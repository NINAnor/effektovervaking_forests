"""Read AlphaEarth annual embeddings from Source Cooperative.

The mirror holds 64-band, 10 m int8 COGs (8192x8192, one directory per year and
UTM zone) plus a tile index (`aef_index.parquet`). Design constraints:

1. COGs are band-interleaved with 1024x1024 blocks, so small windows are
   expensive. This module block-aligns: it finds the blocks the polygons touch,
   reads each once in full, and cuts polygons out in memory.
2. The `.tiff` is bottom-up (origin at the SOUTH edge); blocks are flipped
   to north-up after reading.
3. Values are int8, nodata -128, quantised with a signed square root;
   `dequantise` inverts it.

Reads go through `aef_loader_plus` (no credentials). Earth Engine
(`GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`) is the backup; see `_Tile`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from rasterio.features import rasterize
from rasterio.transform import Affine, xy

from effektovervaking_forests import config
from effektovervaking_forests.condition.paths import (
    AEF_INDEX,
    AEF_INDEX_URL,
    AEF_NODATA,
    BANDS,
    CACHE,
)

log = logging.getLogger(__name__)

__all__ = [
    "dequantise",
    "open_tile",
    "ee_available",
    "ensure_index",
    "tiles_for_geometries",
    "blocks_for_geometries",
    "extract_from_tile",
    "extract_polygon_pixels",
    "extract_points",
]

BLOCK = 1024  # the COGs' internal block size, in pixels
TILE_SIZE = 8192  # tile width/height, in pixels
PIXEL_M = 10.0
BATCH_BLOCKS = 8  # blocks fetched concurrently; 8 x 67 MB of int8
MANIFEST_CACHE = CACHE / "aef_manifests"  # parsed COG headers, reused across runs
# "source_coop" (default; Earth Engine backs up failed blocks) or "ee".
AEF_SOURCE = os.environ.get("AEF_SOURCE") or "source_coop"
if AEF_SOURCE not in ("source_coop", "ee"):
    raise ValueError(f"AEF_SOURCE must be source_coop or ee, not {AEF_SOURCE!r}")
EE_COLLECTION = "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL"
EE_HIGHVOLUME_URL = "https://earthengine-highvolume.googleapis.com"
EE_CHUNK = 256  # 256 x 256 x 64 float32 = 16.8 MB per request (limit 48 MB)
EE_WORKERS = 8
EE_NODATA = -2.0  # outside [-1, 1], the range of the embeddings


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------
def ensure_index(path: Path = AEF_INDEX) -> Path:
    """Download the ~78 MB tile index once; return its local path."""
    if path.exists() and path.stat().st_size > 1_000_000:
        return path
    import requests

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    with requests.get(AEF_INDEX_URL, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    tmp.replace(path)
    return path


def tiles_for_geometries(
    gpkg_or_gdf, years: Sequence[int], index: Path = AEF_INDEX
) -> pd.DataFrame:
    """Index rows for every (tile, year) intersecting the given geometries.

    `gpkg_or_gdf` is a path to any OGR/GeoParquet-readable file or a
    GeoDataFrame. Returns the index columns plus `tile_id`.
    """
    import duckdb
    import geopandas as gpd

    ensure_index(index)
    if isinstance(gpkg_or_gdf, (str, Path)):
        p = Path(gpkg_or_gdf)
        aoi = gpd.read_parquet(p) if p.suffix == ".parquet" else gpd.read_file(p)
    else:
        aoi = gpkg_or_gdf
    aoi = aoi.to_crs(4326)[["geometry"]]

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.register("aoi_df", pd.DataFrame({"wkt": aoi.geometry.to_wkt()}))
    yrs = ", ".join(str(int(y)) for y in years)
    df = con.execute(
        f"""
        SELECT DISTINCT i.path, i.crs, i.year, i.utm_zone,
               i.utm_west, i.utm_south, i.utm_east, i.utm_north
        FROM read_parquet('{index}') i
        WHERE i.year IN ({yrs})
          AND EXISTS (SELECT 1 FROM aoi_df a
                      WHERE ST_Intersects(ST_GeomFromText(a.wkt), i.geom))
        ORDER BY i.year, i.utm_zone, i.path
        """
    ).df()
    df["tile_id"] = (
        df["path"].str.rsplit("/", n=1).str[-1].str.replace(".tiff", "", regex=False)
    )
    return df


# ---------------------------------------------------------------------------
# Block-aligned reading
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Block:
    """One 1024x1024 block of a tile, addressed in the tile's own (bottom-up) grid."""

    brow: int  # block row, counted from the tile's SOUTH edge
    bcol: int  # block column, counted from the tile's WEST edge

    def bounds(self, tile: dict) -> tuple[float, float, float, float]:
        w = float(tile["utm_west"]) + self.bcol * BLOCK * PIXEL_M
        s = float(tile["utm_south"]) + self.brow * BLOCK * PIXEL_M
        return (w, s, w + BLOCK * PIXEL_M, s + BLOCK * PIXEL_M)


def blocks_for_geometries(gdf_utm, tile: dict) -> list[Block]:
    """Which 1024x1024 blocks of `tile` the geometries touch.

    `gdf_utm` must already be in the tile's CRS. Deduplicated, so a block holding
    fifty stands is read once.
    """
    out: set[Block] = set()
    for minx, miny, maxx, maxy in gdf_utm.geometry.bounds.to_numpy():
        c0 = int((minx - tile["utm_west"]) // (BLOCK * 10))
        c1 = int((maxx - tile["utm_west"]) // (BLOCK * 10))
        r0 = int((miny - tile["utm_south"]) // (BLOCK * 10))
        r1 = int((maxy - tile["utm_south"]) // (BLOCK * 10))
        n = TILE_SIZE // BLOCK
        for br in range(max(r0, 0), min(r1, n - 1) + 1):
            for bc in range(max(c0, 0), min(c1, n - 1) + 1):
                out.add(Block(br, bc))
    return sorted(out, key=lambda b: (b.brow, b.bcol))


def dequantise(raw: np.ndarray) -> np.ndarray:
    """Stored int8 (nodata -128) -> float32 embedding: v = (raw / 127.5)^2 * sign(raw).

    The mirror is quantised with a signed square root (not linear). A 256-entry
    lookup table; nodata -> NaN.
    """
    return _DEQUANT_LUT[np.asarray(raw).astype(np.int16) + 128]


def _build_dequant_lut() -> np.ndarray:
    idx = np.arange(-128, 128, dtype=np.float32)
    lut = (idx / 127.5) ** 2 * np.sign(idx)
    lut[AEF_NODATA + 128] = np.nan
    return lut


_DEQUANT_LUT = _build_dequant_lut()


def open_tile(tile: dict) -> _Tile:
    """A tile open for block reads: `.read_block(blk) -> (float32 (64, 1024,
    1024) north-up, Affine)`. A context manager."""
    return _Tile(tile)


class _Tile:
    """Source Cooperative through `aef_loader_plus`, Earth Engine as backup.

    A block that cannot be read from Source Cooperative is read from Earth
    Engine when `FORESTMON_EE_PROJECT` is set; `AEF_SOURCE=ee` reads every block
    there. Both return the same embeddings (EE float32 vs dequantised int8
    differ by float32 rounding).
    """

    def __init__(self, tile: dict):
        self.tile = tile
        self._da = None
        self._plan: list[Block] = []
        self._cache: dict[Block, np.ndarray] = {}

    def __enter__(self) -> _Tile:
        return self

    def __exit__(self, *exc) -> None:
        self._da, self._cache = None, {}

    def prefetch(self, blocks: Sequence[Block]) -> None:
        """Blocks to fetch concurrently, `BATCH_BLOCKS` at a time, on demand."""
        self._plan = list(blocks)

    def read_block(self, blk: Block) -> tuple[np.ndarray, Affine]:
        west, south, _, north = blk.bounds(self.tile)
        transform = Affine(PIXEL_M, 0.0, west, 0.0, -PIXEL_M, north)
        if AEF_SOURCE != "ee":
            try:
                if blk not in self._cache:
                    self._fetch(blk)
                raw = self._cache.pop(blk)
                return dequantise(raw[:, ::-1, :]), transform  # bottom-up file
            except Exception as e:
                if not ee_available():
                    raise
                log.warning("Source Cooperative read failed (%s); using EE", e)
        return _ee_block(self.tile, blk), transform

    def _open(self):
        from aef_loader import AEFTileInfo, DataSource, VirtualTiffReader

        t = self.tile
        west, south = float(t["utm_west"]), float(t["utm_south"])
        span = TILE_SIZE * PIXEL_M
        info = AEFTileInfo(
            id=str(t.get("tile_id", t["path"])),
            path=t["path"],
            year=int(t["year"]),
            bbox=_wgs84_bounds(t["crs"], west, south, west + span, south + span),
            crs_epsg=int(str(t["crs"]).split(":")[-1]),
            utm_zone=t["utm_zone"],
            utm_bounds=(west, south, west + span, south + span),
            source=DataSource.SOURCE_COOP,
        )
        MANIFEST_CACHE.mkdir(parents=True, exist_ok=True)

        async def _go():
            async with VirtualTiffReader(manifest_cache_dir=MANIFEST_CACHE) as r:
                # One chunk per stored block with all 64 bands: the per-band
                # byte ranges of a block are then fetched together.
                return await r.open_tiles_by_zone([info], chunks="all-bands")

        return asyncio.run(_go())[t["utm_zone"]].ds["embeddings"]

    def _fetch(self, blk: Block) -> None:
        """Fetch `blk` with up to BATCH_BLOCKS - 1 planned blocks alongside it.

        If the batch fails, `blk` is retried alone, so a failing neighbour
        cannot send a readable block to the backup.
        """
        import dask

        if self._da is None:
            self._da = self._open()
        others = [b for b in self._plan if b != blk and b not in self._cache]
        batch = [blk, *others[: BATCH_BLOCKS - 1]]

        def window(b: Block):
            # (time, band, y, x) with y ascending: row 0 is the SOUTH edge.
            r, c = b.brow * BLOCK, b.bcol * BLOCK
            return self._da.data[0, :, r : r + BLOCK, c : c + BLOCK]

        try:
            got = dask.compute(*map(window, batch), scheduler="threads")
        except Exception:
            if len(batch) == 1:
                raise
            batch, got = [blk], dask.compute(window(blk), scheduler="threads")
        for b, arr in zip(batch, got):
            if arr.shape != (len(BANDS), BLOCK, BLOCK):
                raise ValueError(f"block {b} read as {arr.shape}")
            self._cache[b] = np.asarray(arr)
        self._plan = [b for b in self._plan if b not in self._cache]


def _wgs84_bounds(crs, w, s, e, n) -> tuple[float, float, float, float]:
    from pyproj import Transformer

    tr = Transformer.from_crs(crs, 4326, always_xy=True)
    xs, ys = tr.transform([w, e, w, e], [s, s, n, n])
    return min(xs), min(ys), max(xs), max(ys)


def ee_available() -> bool:
    return config.EE_PROJECT is not None and importlib.util.find_spec("ee") is not None


_EE_LOCK = threading.Lock()


def _ee_block(tile: dict, blk: Block) -> np.ndarray:
    """One block from Earth Engine, (64, 1024, 1024) float32 north-up, NaN nodata.

    Fetched as `EE_CHUNK`-pixel squares to stay under the computePixels payload
    limit; only images in the tile's own CRS are used, so pixels are not
    resampled from a neighbouring zone.
    """
    import ee

    with _EE_LOCK:
        if not ee.data.is_initialized():
            ee.Initialize(project=config.EE_PROJECT, opt_url=EE_HIGHVOLUME_URL)
    crs, year = str(tile["crs"]), int(tile["year"])
    west, south, east, north = blk.bounds(tile)
    img = (
        ee.ImageCollection(EE_COLLECTION)
        .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
        .filterBounds(ee.Geometry.Rectangle([west, south, east, north], crs, False))
        .map(lambda i: i.set("crs", i.select(0).projection().crs()))
        .filter(ee.Filter.eq("crs", crs))
        .mosaic()
        .select(list(BANDS))
        .unmask(EE_NODATA)
    )
    out = np.empty((len(BANDS), BLOCK, BLOCK), dtype=np.float32)

    def chunk(rc: tuple[int, int]) -> None:
        r, c = rc
        grid = {
            "dimensions": {"width": EE_CHUNK, "height": EE_CHUNK},
            "affineTransform": {
                "scaleX": PIXEL_M,
                "shearX": 0,
                "translateX": west + c * PIXEL_M,
                "shearY": 0,
                "scaleY": -PIXEL_M,
                "translateY": north - r * PIXEL_M,
            },
            "crsCode": crs,
        }
        a = ee.data.computePixels(
            {"expression": img, "fileFormat": "NUMPY_NDARRAY", "grid": grid}
        )
        out[:, r : r + EE_CHUNK, c : c + EE_CHUNK] = np.stack([a[b] for b in BANDS])

    starts = [
        (r, c) for r in range(0, BLOCK, EE_CHUNK) for c in range(0, BLOCK, EE_CHUNK)
    ]
    with ThreadPoolExecutor(max_workers=EE_WORKERS) as pool:
        list(pool.map(chunk, starts))
    out[out == EE_NODATA] = np.nan
    return out


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def extract_from_tile(
    tile,
    gdf,
    id_col: str = "poly_id",
    all_touched: bool = False,
    geometry: bool = True,
) -> pd.DataFrame:
    """All pixels of `gdf` that fall on one (tile, year) -- the unit of work and resume.

    `tile` is a row of `tiles_for_geometries`. Returns long-format rows
    `id_col, year, utm_zone, x, y, A00..A63`, x/y being the pixel centre in the
    tile's UTM CRS (stable across years: annual tiles of a zone share a grid).
    An empty frame, never an exception, means nothing here.
    """
    tile = tile._asdict() if hasattr(tile, "_asdict") else dict(tile)
    empty = pd.DataFrame(columns=[id_col, "year", "utm_zone", "x", "y", *BANDS])

    local = gdf.to_crs(tile["crs"])
    tb = (tile["utm_west"], tile["utm_south"], tile["utm_east"], tile["utm_north"])
    sub = local[local.geometry.apply(lambda g: _overlaps(g.bounds, tb))]
    if sub.empty:
        return empty

    if not geometry:
        return _sample_points(tile, sub, id_col, empty)

    # Integer codes, because rasterize() writes a numeric label raster.
    ids = sub[id_col].to_numpy()
    code_of = {v: i + 1 for i, v in enumerate(ids)}

    frames: list[pd.DataFrame] = []
    seen: set = set()
    # Polygon bounds taken out of the GeoSeries once, not per polygon per block.
    bounds = sub.geometry.bounds.to_numpy()
    with open_tile(tile) as src:
        blocks = blocks_for_geometries(sub, tile)
        src.prefetch(blocks)
        for blk in blocks:
            bb = blk.bounds(tile)
            keep = (
                (bounds[:, 0] < bb[2])
                & (bounds[:, 2] > bb[0])
                & (bounds[:, 1] < bb[3])
                & (bounds[:, 3] > bb[1])
            )
            here = sub[keep]
            if here.empty:
                continue
            arr, transform = src.read_block(blk)
            labels = rasterize(
                ((g, code_of[i]) for g, i in zip(here.geometry, here[id_col])),
                out_shape=arr.shape[1:],
                transform=transform,
                fill=0,
                all_touched=all_touched,
                dtype="int32",
            )
            rows, cols = np.nonzero(labels)
            if len(rows):
                xs, ys = xy(transform, rows, cols)
                vals = arr[:, rows, cols]
                ok = np.isfinite(vals[0])
                if ok.any():
                    df = pd.DataFrame(vals[:, ok].T, columns=list(BANDS))
                    df.insert(0, "y", np.asarray(ys)[ok])
                    df.insert(0, "x", np.asarray(xs)[ok])
                    df.insert(0, id_col, ids[labels[rows, cols][ok] - 1])
                    frames.append(df)
                    seen.update(df[id_col].unique())

        # Stands smaller than a pixel rasterize to nothing; fall back to the pixel
        # under the centroid. Runs AFTER every block (not inside the loop): a
        # stand spanning two blocks may rasterize in the second, and adding its
        # centroid while processing the first would give it a spurious extra pixel.
        missing = sub[~sub[id_col].isin(seen)]
        cent = missing.geometry.centroid if len(missing) else None
        for blk in blocks_for_geometries(missing, tile) if len(missing) else []:
            bb = blk.bounds(tile)
            # Strict on both sides: a centroid on a block edge belongs to neither.
            cx, cy = cent.x.to_numpy(), cent.y.to_numpy()
            here = missing[(cx > bb[0]) & (cx < bb[2]) & (cy > bb[1]) & (cy < bb[3])]
            if here.empty:
                continue
            arr, transform = src.read_block(blk)
            got = _centroid_pixels(here, arr, transform, id_col)
            if got is not None:
                frames.append(got)

    if not frames:
        return empty
    out = pd.concat(frames, ignore_index=True)
    out.insert(1, "utm_zone", tile["utm_zone"])
    out.insert(1, "year", int(tile["year"]))
    return out.drop_duplicates(subset=[id_col, "x", "y"])


def _centroid_pixels(missing, arr, transform, id_col) -> pd.DataFrame | None:
    inv = ~transform
    recs, keys = [], []
    for row in missing.itertuples():
        c, r = inv * (row.geometry.centroid.x, row.geometry.centroid.y)
        r, c = int(r), int(c)
        if not (0 <= r < arr.shape[1] and 0 <= c < arr.shape[2]):
            continue
        v = arr[:, r, c]
        if not np.isfinite(v[0]):
            continue
        x, y = xy(transform, r, c)
        recs.append(np.concatenate(([x, y], v)))
        keys.append(getattr(row, id_col))
    if not recs:
        return None
    df = pd.DataFrame(recs, columns=["x", "y", *BANDS])
    df.insert(0, id_col, keys)
    return df


def _sample_points(tile, sub, id_col, empty) -> pd.DataFrame:
    """Point sampling, block-aligned for the same reason polygons are."""
    frames = []
    with open_tile(tile) as src:
        blocks = blocks_for_geometries(sub, tile)
        src.prefetch(blocks)
        for blk in blocks:
            bb = blk.bounds(tile)
            here = sub[
                sub.geometry.apply(
                    lambda g: bb[0] <= g.x < bb[2] and bb[1] <= g.y < bb[3]
                )
            ]
            if here.empty:
                continue
            arr, transform = src.read_block(blk)
            inv = ~transform
            cs, rs = inv * (here.geometry.x.to_numpy(), here.geometry.y.to_numpy())
            rs, cs = rs.astype(int), cs.astype(int)
            ok = (rs >= 0) & (rs < arr.shape[1]) & (cs >= 0) & (cs < arr.shape[2])
            if not ok.any():
                continue
            vals = arr[:, rs[ok], cs[ok]]
            good = np.isfinite(vals[0])
            xs, ys = xy(transform, rs[ok][good], cs[ok][good])
            df = pd.DataFrame(vals[:, good].T, columns=list(BANDS))
            df.insert(0, "y", np.asarray(ys))
            df.insert(0, "x", np.asarray(xs))
            df.insert(0, id_col, here[id_col].to_numpy()[ok][good])
            frames.append(df)
    if not frames:
        return empty
    out = pd.concat(frames, ignore_index=True)
    out.insert(1, "utm_zone", tile["utm_zone"])
    out.insert(1, "year", int(tile["year"]))
    return out


def extract_polygon_pixels(
    gdf,
    years: Sequence[int],
    id_col: str = "poly_id",
    index: Path = AEF_INDEX,
    all_touched: bool = False,
    progress: bool = True,
) -> pd.DataFrame:
    """Serial convenience API over `extract_from_tile`.

    For a full run use `scripts/02_extract_embeddings.py`, which shards by tile,
    runs tiles concurrently and resumes -- this path has no checkpoint.
    """
    tiles = tiles_for_geometries(gdf, years, index)
    if tiles.empty:
        raise RuntimeError("no AlphaEarth tiles intersect these geometries")
    rows = list(tiles.itertuples())
    if progress:
        from tqdm.auto import tqdm

        rows = tqdm(rows, desc="tiles", unit="tile")
    frames = [extract_from_tile(t, gdf, id_col, all_touched) for t in rows]
    out = pd.concat(frames, ignore_index=True)
    return out.drop_duplicates(subset=["utm_zone", "x", "y", "year", id_col])


def extract_points(
    gdf,
    years: Sequence[int],
    id_col: str = "ref_id",
    index: Path = AEF_INDEX,
    progress: bool = True,
) -> pd.DataFrame:
    """Embedding at each point, for every year. Same schema as the polygon path."""
    tiles = tiles_for_geometries(gdf, years, index)
    rows = list(tiles.itertuples())
    if progress:
        from tqdm.auto import tqdm

        rows = tqdm(rows, desc="tiles", unit="tile")
    frames = [extract_from_tile(t, gdf, id_col, geometry=False) for t in rows]
    if not frames:
        return pd.DataFrame(columns=[id_col, "year", "utm_zone", "x", "y", *BANDS])
    return pd.concat(frames, ignore_index=True)


def _overlaps(a, b) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])
