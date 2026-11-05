"""Read AlphaEarth annual embeddings from Source Cooperative (no Earth Engine).

The community mirror at `s3://us-west-2.opendata.source.coop/tge-labs/aef` holds
the same annual 64-band, 10 m embeddings as `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`,
as 8192x8192 int8 COGs -- one directory per year and UTM zone -- plus a spatial
index (`aef_index.parquet`) with one row per (tile, year).

Three properties of the mirror drive the whole design of this module. They were
measured, not assumed; `scripts/09_bench_reads.py` reproduces the numbers.

1. **The COGs are BAND-interleaved with 1024x1024 blocks.** Reading a 32x32
   window is therefore not cheap: GDAL must fetch one 1024x1024 block *per band*,
   i.e. 64 compressed blocks totalling ~40 MB, to answer it. Measured from here,
   a 32x32x64 window took >90 s while a full 1024x1024x64 block took **6-10 s**.
   The window size is almost free; the block count is everything.

   So this module never reads per-polygon windows. It block-aligns: it works out
   which 1024x1024 blocks the polygons touch, reads each such block **once**, in
   full, and cuts every polygon out of the block in memory. That turns a
   per-polygon read pattern into a handful of large reads and is the difference
   between a run of days and a run of tens of minutes.

   `GDAL_HTTP_MULTIRANGE=YES` matters just as much: it lets the 64 per-band
   range requests go out concurrently instead of serially.

2. **The `.tiff` is bottom-up.** Its geotransform has a POSITIVE y step with the
   origin at the tile's SOUTH edge, so row 0 is the southernmost row. Every tile
   ships a sibling `.vrt` that presents it north-up -- but that `.vrt` is a
   *warped* VRT which re-blocks to 128x512 and defeats the block alignment above.
   So we read the `.tiff` natively and flip, which `_read_block` does, returning
   a normal north-up affine. `tests/test_aef_reader.py` checks the flipped output
   against the `.vrt` pixel for pixel.

3. **Values are int8, nodata -128,** the unit-normalised float embedding
   quantised by one global scale factor. `AEF_INT8_SCALE` (1/127) is applied on
   read for interpretability, but nothing downstream depends on its exact value:
   the trajectory axis and the DoR score are both cosine-based, and cosine
   distance is invariant to a global scale.

Anonymous access needs `AWS_NO_SIGN_REQUEST=YES` for `/vsis3`; the default here
is `/vsicurl` against `data.source.coop`, which needs no credentials at all and
measured faster from Europe. `gdal_env()` applies both plus the HTTP tuning.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize
from rasterio.transform import Affine, xy

from effektovervaking_forests.condition.paths import (
    AEF_DEQUANT,
    AEF_INDEX,
    AEF_INDEX_URL,
    AEF_INT8_SCALE,
    AEF_NODATA,
    BANDS,
)

__all__ = [
    "gdal_env",
    "dequantise",
    "open_tile",
    "ensure_index",
    "tiles_for_geometries",
    "tile_uri",
    "blocks_for_geometries",
    "extract_from_tile",
    "extract_polygon_pixels",
    "extract_points",
]

BLOCK = 1024  # the COGs' internal block size, in pixels
TILE_SIZE = 8192  # tile width/height, in pixels

_GDAL_ENV = {
    # /vsis3 fallback; harmless when reading over /vsicurl.
    "AWS_NO_SIGN_REQUEST": "YES",
    "AWS_DEFAULT_REGION": "us-west-2",
    # Never list a prefix just to open one object.
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tiff,.vrt",
    # The single most important setting here: issue the 64 per-band block ranges
    # concurrently. Without it a block read is 64 serial round trips.
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
    # rasterio.Env passes these to CPLSetConfigOption by type: the size options
    # must stay ints, not strings, or GDAL raises "an integer is required".
    "GDAL_CACHEMAX": 2048,
    "VSI_CACHE": True,
    "VSI_CACHE_SIZE": 64 * 1024 * 1024,
    "GDAL_NUM_THREADS": "ALL_CPUS",
    # data.source.coop sits behind Cloudflare, which returns 520/522/525 under
    # concurrency often enough to fail whole tiles. GDAL's own retry does not
    # cover those codes by default -- they are Cloudflare-specific, not RFC
    # statuses -- so they are listed explicitly alongside the usual 5xx.
    "GDAL_HTTP_MAX_RETRY": 5,
    "GDAL_HTTP_RETRY_DELAY": 2,
    "GDAL_HTTP_RETRY_CODES": "429,500,502,503,504,520,521,522,523,524,525",
}


def gdal_env(**overrides) -> rasterio.Env:
    """A `rasterio.Env` configured for anonymous Source Cooperative reads."""
    cfg = dict(_GDAL_ENV)
    cfg.update(overrides)
    # Also export to os.environ: the AWS options are read when the /vsis3 handler
    # is first created rather than per-Env, so an Env alone can be too late.
    for k, v in cfg.items():
        os.environ.setdefault(k, str(v))
    return rasterio.Env(**cfg)


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
    GeoDataFrame. Returns the index columns plus `uri`, the GDAL path to open.
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
    df["uri"] = df["path"].map(tile_uri)
    df["tile_id"] = (
        df["path"].str.rsplit("/", n=1).str[-1].str.replace(".tiff", "", regex=False)
    )
    return df


def tile_uri(path: str, via: str = "curl") -> str:
    """s3:// path from the index -> a GDAL URI for the bottom-up `.tiff`.

    `via="curl"` (default) goes through the public HTTPS mirror and needs no
    credentials; `via="s3"` uses /vsis3 with AWS_NO_SIGN_REQUEST.
    """
    if via == "s3":
        return path.replace("s3://", "/vsis3/")
    rest = path.replace("s3://us-west-2.opendata.source.coop/", "")
    return f"/vsicurl/https://data.source.coop/{rest}"


# ---------------------------------------------------------------------------
# Block-aligned reading
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Block:
    """One 1024x1024 block of a tile, addressed in the tile's own (bottom-up) grid."""

    brow: int  # block row, counted from the tile's SOUTH edge
    bcol: int  # block column, counted from the tile's WEST edge

    def bounds(self, tile: dict) -> tuple[float, float, float, float]:
        w = tile["utm_west"] + self.bcol * BLOCK * 10.0
        s = tile["utm_south"] + self.brow * BLOCK * 10.0
        return (w, s, w + BLOCK * 10.0, s + BLOCK * 10.0)


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
    """Stored int8 (nodata -128) -> float32 embedding, by `paths.AEF_DEQUANT`.

    The two rules are NOT a scale apart, which is the whole point of having this
    in one place:

        linear         v = raw / 127
        signed_square  v = (raw / 127.5)^2 * sign(raw)

    `scripts/09_check_gee_parity.py` compares both against
    `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL` at the same coordinates. Over 400
    pixels of this AOI in 2024, `signed_square` reproduces Earth Engine exactly
    -- cosine 1.000000, implied scale 1.0000, max |difference| 0.011, which is
    the int8 granularity -- while `linear` gives cosine 0.9617 and an implied
    scale of 0.380. AlphaEarth embeddings are unit-norm; `signed_square` returns
    ||v|| = 1.000 and `linear` returns 2.531.

    So the mirror is quantised with a signed square root and the linear read is
    wrong. It is wrong in *direction*, not in scale, and the argument in
    `docs/METHODS.md` and `condition/axis.py` that "nothing downstream depends on
    AEF_INT8_SCALE because cosine distance is invariant to a global factor" is
    sound reasoning from a false premise: the map is not a global factor. Two
    pixels that Earth Engine puts at cosine distance d are put at a different
    distance by the linear read.

    `linear` remains the default so that the shipped outputs and the report stay
    reproducible from the shipped code. Setting `AEF_DEQUANT=signed_square` and
    re-running from step 2 moves everything onto the Earth Engine embedding.
    """
    raw = np.asarray(raw)
    if AEF_DEQUANT == "linear":
        arr = raw.astype(np.float32)
        arr[arr == AEF_NODATA] = np.nan
        arr *= AEF_INT8_SCALE
        return arr
    if AEF_DEQUANT == "signed_square":
        # A 256-entry lookup: every int8 maps to
        # one of 256 outcomes, so the table is built once and gathered, and the
        # nodata step folds into the same gather instead of a separate mask.
        return _DEQUANT_LUT[raw.astype(np.int16) + 128]
    raise ValueError(
        f"AEF_DEQUANT must be 'linear' or 'signed_square', got {AEF_DEQUANT!r}"
    )


def _build_dequant_lut() -> np.ndarray:
    idx = np.arange(-128, 128, dtype=np.float32)
    lut = (idx / 127.5) ** 2 * np.sign(idx)
    lut[AEF_NODATA + 128] = np.nan
    return lut


_DEQUANT_LUT = _build_dequant_lut()


def open_tile(tile: dict):
    """A tile open for block reads.

    Gives `.read_block(blk) -> (float32 (64, 1024, 1024) north-up, Affine)` and is
    a context manager.
    """
    return _GdalTile(tile)


class _GdalTile:
    """rasterio over /vsicurl -- the block reader."""

    def __init__(self, tile: dict):
        self.tile, self._env, self._ds = tile, None, None

    def __enter__(self) -> _GdalTile:
        self._env = gdal_env()
        self._env.__enter__()
        self._ds = rasterio.open(self.tile["uri"])
        return self

    def __exit__(self, *exc) -> None:
        if self._ds is not None:
            self._ds.close()
        if self._env is not None:
            self._env.__exit__(*exc)
        self._ds = self._env = None

    def read_block(self, blk: Block):
        return _read_block(self._ds, blk)


def _read_block(ds, blk: Block) -> tuple[np.ndarray, Affine]:
    """Read one whole block and return it north-up, with a north-up affine.

    The block is addressed geographically -- `brow` counts north from the tile's
    SOUTH edge -- and the file rows are then derived from the dataset's own
    transform, so this is correct whether the file is bottom-up (the `.tiff`, y
    step +10, origin at the south edge) or north-up (the `.vrt`). Getting this
    backwards reads the block mirrored about the tile's centre line, which is a
    silent wrong answer rather than an error: the array is valid, just from
    somewhere else. `tests/test_aef_reader.py` pins it against the `.vrt`.
    """
    west, south, north = _tile_origin(ds)
    step = abs(ds.transform.e)
    bw = west + blk.bcol * BLOCK * ds.transform.a
    bs = south + blk.brow * BLOCK * step
    bn = bs + BLOCK * step

    inv = ~ds.transform
    c0, r0 = inv * (bw, bn)
    c1, r1 = inv * (bw, bs)
    row_off, col_off = int(round(min(r0, r1))), int(round(c0))
    if (
        row_off < 0
        or col_off < 0
        or row_off + BLOCK > ds.height
        or col_off + BLOCK > ds.width
    ):
        raise IndexError(f"block {blk} outside {ds.width}x{ds.height} tile")

    arr = ds.read(window=((row_off, row_off + BLOCK), (col_off, col_off + BLOCK)))
    if ds.transform.e > 0:  # bottom-up: rows run S -> N
        arr = arr[:, ::-1, :]
    return dequantise(arr), Affine(ds.transform.a, 0.0, bw, 0.0, -step, bn)


def _tile_origin(ds) -> tuple[float, float, float]:
    """(west, south, north) of a dataset, for either row orientation."""
    y0 = ds.transform.f
    y1 = ds.transform.f + ds.height * ds.transform.e
    return ds.transform.c, min(y0, y1), max(y0, y1)


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
    `id_col, year, utm_zone, x, y, A00..A63`, with x/y the pixel centre in the
    tile's UTM CRS -- so a pixel keeps a stable identity across years, since the
    annual tiles of one zone share a grid.

    Never raises for "nothing here": a caller sharding by tile can treat an empty
    frame as done.
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
    # The polygon bounds are needed once per block, so they are taken out of the
    # GeoSeries once rather than through a Python call per polygon per block.
    bounds = sub.geometry.bounds.to_numpy()
    with open_tile(tile) as src:
        blocks = blocks_for_geometries(sub, tile)
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

        # Stands smaller than a pixel rasterize to nothing; fall back to the
        # pixel under the centroid so a 0.01 ha kalklindeskog stand is scored
        # rather than silently dropped. This runs AFTER every block, not inside
        # the loop: a stand spanning two blocks may rasterize in the second one,
        # and adding its centroid while processing the first would give it a
        # spurious extra pixel.
        missing = sub[~sub[id_col].isin(seen)]
        cent = missing.geometry.centroid if len(missing) else None
        for blk in blocks_for_geometries(missing, tile) if len(missing) else []:
            bb = blk.bounds(tile)
            # Strict on both sides, exactly as `_overlaps` was on the degenerate
            # bounds of a point: a centroid on a block edge belongs to neither.
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
