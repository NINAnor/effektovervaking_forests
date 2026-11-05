"""Read per-pixel Sentinel-2 L2A reflectance out of CDSE's S3 archive.

The design constraint is that our pixels are a needle in a haystack: ~38k
scattered 10 m pixels inside a 110 x 110 km tile, i.e. about 0.3% of it. So we
never read a band, we read the JP2's internal blocks that our pixels fall in —
typically a few dozen per tile — through GDAL's /vsis3 byte-range support.

Three details decide whether the numbers match Earth Engine, and all three are
handled here rather than being assumed:

* **BOA_ADD_OFFSET.** Processing baseline 04.00 (2022-01-25 onward) shifted L2A
  DNs by +1000. Earth Engine's *_HARMONIZED collections undo it. We read the
  actual offset from each product's metadata instead of hard-coding the
  changeover date, because reprocessed old scenes carry new baselines.
* **20 m bands.** B11/B12 are natively 20 m. Earth Engine's getRegion at
  scale=10 returns the 20 m value of the containing cell, so we read the 20 m
  asset and locate the containing cell — reproducing it exactly, with no
  resampling of our own.
* **No warping.** A scene whose CRS differs from the AOI grid is skipped, not
  reprojected. See operational.aoi for why.
"""

from __future__ import annotations

import functools
import os
import re
import warnings
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import rowcol
from rasterio.windows import Window

from . import cdse_auth, config

# A scene means seven files (six bands + SCL), each a separate network open.
# That fixed cost — not the bytes — is what dominates: reading them concurrently
# instead of in sequence measured 4.9-6.4x faster (13-25 s -> 2.6-5.1 s per
# scene, 17-20k pixels). Rasterio's Env is thread-local, so each worker enters
# its own; the S3 credentials are process-wide environment variables and need no
# such care.
#
# Note this multiplies with the scene-level workers in pipeline.ingest: 4 scene
# workers x 7 band threads is 28 concurrent connections to CDSE, and measured
# throughput peaks at ~14 and degrades past it. The pair is therefore expressed
# as one budget, config.MAX_CDSE_CONNECTIONS; raise it there, not here.
BAND_WORKERS = 7


def _install_s3_env() -> None:
    """Put CDSE's S3 settings into the process environment for GDAL.

    They cannot go through `rasterio.Env(...)`: rasterio 1.5 rejects AWS_* config
    options there ("AWS credentials are handled exclusively by boto3") and
    expects a `rasterio.session.AWSSession`, which pulls in boto3 purely to hand
    back keys we already have. GDAL reads these names from the environment
    directly, so setting them here gets the same result with one less
    dependency.

    AWS_VIRTUAL_HOSTING=FALSE is required — CDSE uses path-style addressing.
    """
    key, secret = cdse_auth.s3_credentials()
    os.environ.update(
        {
            "AWS_ACCESS_KEY_ID": key,
            "AWS_SECRET_ACCESS_KEY": secret,
            "AWS_S3_ENDPOINT": config.S3_ENDPOINT.replace("https://", "").replace(
                "http://", ""
            ),
            "AWS_VIRTUAL_HOSTING": "FALSE",
            "AWS_HTTPS": "YES",
            # CDSE's S3 is not AWS and issues no region; an unset region makes GDAL's
            # signer fall back to a default that CDSE rejects on some endpoints.
            "AWS_DEFAULT_REGION": (os.environ.get("AWS_DEFAULT_REGION") or "default"),
        }
    )


# Remaining GDAL settings are plain config options and are safe in Env().
# DISABLE_READDIR_ON_OPEN stops GDAL listing a whole SAFE directory to open one
# file, which over S3 is a directory listing per band.
def _gdal_env() -> rasterio.Env:
    """The GDAL config for a read (`config.GDAL_OPTS`)."""
    _install_s3_env()
    return rasterio.Env(**config.GDAL_OPTS)


def _vsis3(href: str) -> str:
    return href.replace("s3://", "/vsis3/")


@functools.lru_cache(maxsize=4096)
def _boa_offsets_from_xml(xml_href: str) -> tuple[int, ...]:
    """BOA_ADD_OFFSET per band index, from a product's MTD_MSIL2A.xml.

    Returns a 13-long tuple indexed by S2 band number order (B01..B12 as ESA
    lists them). Pre-04.00 products have no offset element at all, which
    correctly yields zeros.
    """
    fs = cdse_auth.s3_filesystem()
    path = xml_href.replace("s3://", "")
    with fs.open(path, "rb") as fh:
        xml = fh.read().decode("utf-8", errors="replace")
    offs = {}
    for m in re.finditer(
        r'<BOA_ADD_OFFSET\s+band_id="(\d+)">(-?\d+)</BOA_ADD_OFFSET>', xml
    ):
        offs[int(m.group(1))] = int(m.group(2))
    if not offs:
        return tuple([0] * 13)
    return tuple(offs.get(i, 0) for i in range(13))


# ESA band_id ordering inside MTD_MSIL2A.xml: 0=B01, 1=B02, ... 8=B08, 9=B8A,
# 10=B09, 11=B11, 12=B12. Note there is no B10 in L2A, so the tail is shifted.
_BAND_ID = {"B02": 1, "B03": 2, "B04": 3, "B08": 7, "B11": 11, "B12": 12}


def boa_offsets_for_scene(scene: pd.Series) -> dict[str, int]:
    """Every band's offset for one scene, fetched ONCE.

    Deliberately called before the band threads start rather than from inside
    them. `_boa_offsets_from_xml` is `lru_cache`d, but an lru_cache does not
    coalesce concurrent misses: six threads hitting a cold entry simultaneously
    issue six identical reads of the same product XML, so the "seven network
    opens per scene" the design is tuned around was really up to thirteen on
    every uncached scene.
    """
    return {b: boa_offset_for(scene, b) for b in config.BANDS}


def boa_offset_for(scene: pd.Series, band: str) -> int:
    """The offset to ADD to a raw DN to put it on the harmonized scale.

    ESA stores BOA_ADD_OFFSET as -1000 for baseline >= 04.00, meaning the
    already-shifted DN needs it added back. Falling back to the baseline rule if
    metadata is unavailable keeps an unattended run alive, but is logged by the
    caller because a silently wrong offset is a 0.1-reflectance step change —
    exactly the kind of thing S-CCD would report as a disturbance.
    """
    xml = scene["assets"].get("product_metadata")
    if xml:
        try:
            return _boa_offsets_from_xml(xml)[_BAND_ID[band]]
        except Exception as e:  # noqa: BLE001
            warnings.warn(
                f"BOA offset metadata unreadable for {scene['id']}: {e}", stacklevel=2
            )
    baseline = str(scene.get("baseline") or "")
    try:
        return -1000 if float(baseline) >= 4.0 else 0
    except ValueError:
        return 0


def _read_at(
    path: str, xs: np.ndarray, ys: np.ndarray, expect_crs: str
) -> tuple[np.ndarray, np.ndarray]:
    """Values at the given map coordinates, read block-by-block.

    Returns (values, valid) where `valid` marks points that fell inside the
    raster. Reading whole internal blocks rather than per-point windows is the
    difference between a few dozen range requests and tens of thousands.
    """
    with rasterio.open(path) as ds:
        if ds.crs is None or ds.crs.to_string() != expect_crs:
            raise ValueError(f"CRS mismatch: {ds.crs} != {expect_crs}")

        rows, cols = rowcol(ds.transform, xs, ys)  # floor -> containing cell
        rows = np.asarray(rows, dtype="int64")
        cols = np.asarray(cols, dtype="int64")
        valid = (rows >= 0) & (rows < ds.height) & (cols >= 0) & (cols < ds.width)

        out = np.zeros(len(xs), dtype="int32")
        if not valid.any():
            return out, valid

        bh, bw = ds.block_shapes[0]
        br = np.where(valid, rows // bh, -1)
        bc = np.where(valid, cols // bw, -1)

        for r_blk, c_blk in {t for t in zip(br[valid], bc[valid])}:
            sel = valid & (br == r_blk) & (bc == c_blk)
            row_off, col_off = r_blk * bh, c_blk * bw
            win = Window(
                col_off,
                row_off,
                min(bw, ds.width - col_off),
                min(bh, ds.height - row_off),
            )
            block = ds.read(1, window=win)
            out[sel] = block[rows[sel] - row_off, cols[sel] - col_off]
        return out, valid


def read_scene(
    scene: pd.Series,
    pixels: pd.DataFrame,
    apply_offset: bool = True,
    scl_first: bool = True,
) -> pd.DataFrame:
    """Per-pixel reflectance + SCL for one scene.

    `pixels` needs `pix_id`, `utm_x`, `utm_y` and `utm_crs`. Only pixels that
    actually fall inside this scene's raster come back, so a tile that clips the
    AOI simply contributes fewer rows. Values are raw DN on the harmonized scale
    (0-10000), which is what pyxccd wants; no reflectance division happens here.

    `scl_first` reads the single 20 m SCL band before the six reflectance bands
    and returns early when it yields nothing usable — a granule's bbox can
    contain our pixels while its actual data footprint (swaths are not square)
    does not, and the bbox prefilter cannot tell the difference.

    Measured, this is a SMALL win, not the big one it looks like: on a 2025
    sample it took empty scenes from ~1.7 s to ~0.3 s, but those were already
    cheap — `_read_at` returns before fetching any block when no point is inside
    the raster — and total backfill time is dominated by the scenes that DO have
    data (40-60 s each). Net effect on that sample was ~1.0x. It is kept because
    it is free (the probe is reused below, not re-read) and it makes the empty
    case cheap in the operational path, where one scene is the whole run.

    Turn it off for anything that needs the full pixel set regardless of
    quality (e.g. comparing raw reflectance on cloudy pixels).
    """
    crss = pixels["utm_crs"].unique()
    if len(crss) != 1:
        # A multi-zone AOI must be split before reading; `pixels_for_scene` does.
        raise ValueError(f"{scene['id']}: pixels span {len(crss)} CRSs {list(crss)}")
    crs = crss[0]
    xs = pixels["utm_x"].to_numpy("float64")
    ys = pixels["utm_y"].to_numpy("float64")

    cols: dict[str, np.ndarray] = {}
    valid_all = np.ones(len(pixels), dtype=bool)
    raw_nodata_any = np.zeros(len(pixels), dtype=bool)

    scl_href = scene["assets"].get(config.SCL_ASSET)
    if not scl_href:
        raise KeyError(f"{scene['id']}: missing asset {config.SCL_ASSET}")

    scl = valid_scl = None
    if scl_first:
        with _gdal_env():
            scl, valid_scl = _read_at(_vsis3(scl_href), xs, ys, crs)
        # SCL 0 is "no data" — outside the granule footprint.
        usable = valid_scl & (scl > 0) & (~np.isin(scl, config.SCL_REJECT))
        if not usable.any():
            return pd.DataFrame(
                columns=[
                    "pix_id",
                    *config.BANDS,
                    "scl",
                    "scene_id",
                    "datetime",
                    "nodata",
                    "clear",
                ]
            )

    # Read the product metadata once, on this thread, before fanning out.
    offsets = boa_offsets_for_scene(scene) if apply_offset else {}

    def _one_band(band: str):
        asset = config.ASSET_FOR_BAND[band]
        href = scene["assets"].get(asset)
        if not href:
            raise KeyError(f"{scene['id']}: missing asset {asset}")
        with _gdal_env():  # Env is thread-local
            vals, valid = _read_at(_vsis3(href), xs, ys, crs)
        raw_nodata = vals == config.NODATA_DN
        if apply_offset:
            off = offsets[band]
            # Only shift real observations; DN 0 is nodata and must stay 0.
            # Clamp at 0 as the HARMONIZED collection does (max(DN - 1000, 0)):
            # a dark pixel with DN < 1000 would otherwise go negative where
            # Earth Engine reports 0 (0.6-0.9% of rows; olivinskog parity failed
            # on exactly these). Nodata is judged on the RAW DN, so a clamped
            # dark pixel stays a real observation.
            vals = np.where(raw_nodata, 0, np.maximum(vals + off, 0))
        return band, vals, valid, raw_nodata

    def _read_scl():
        with _gdal_env():
            return _read_at(_vsis3(scl_href), xs, ys, crs)

    with ThreadPoolExecutor(max_workers=BAND_WORKERS) as pool:
        futures = [pool.submit(_one_band, b) for b in config.BANDS]
        # When scl_first already fetched SCL, reuse it rather than paying twice.
        scl_fut = None if scl is not None else pool.submit(_read_scl)
        for f in futures:
            band, vals, valid, raw0 = f.result()
            cols[band] = vals
            valid_all &= valid
            raw_nodata_any |= raw0
        if scl_fut is not None:
            scl, valid_scl = scl_fut.result()

    valid_all &= valid_scl

    df = pd.DataFrame({"pix_id": pixels["pix_id"].to_numpy()})
    for b, v in cols.items():
        df[b] = v.astype("int16")
    df["scl"] = scl.astype("uint8")
    df["scene_id"] = scene["id"]
    df["datetime"] = pd.Timestamp(scene["datetime"])
    df = df[valid_all].reset_index(drop=True)

    # Nodata in any band means the pixel is outside the granule's data footprint
    # (tiles are square, swaths are not), which is different from being cloudy.
    df["nodata"] = raw_nodata_any[valid_all]
    df["clear"] = (~df["nodata"]) & (~df["scl"].isin(config.SCL_REJECT))
    return df


def tile_utm_crs(tile: str) -> str | None:
    """'32VNM' -> 'EPSG:32632'. Sentinel-2 L2A granules are always in the
    northern/southern UTM zone their MGRS code names; this tool is northern."""
    m = re.match(r"^(\d{1,2})[C-X]", tile)
    return f"EPSG:{32600 + int(m.group(1))}" if m else None


def pixels_for_scene(pixels: pd.DataFrame, scene: pd.Series) -> pd.DataFrame:
    """Cheap pre-filter so we do not open a tile that holds none of our pixels.

    Uses the granule's WGS84 bbox from the catalogue. This is a bounding box,
    not the true footprint, so it over-selects slightly near swath edges; the
    read itself is the exact test, dropping anything outside the raster. The
    point is only to avoid the network round trip for the 7 tiles' worth of
    scenes that miss our polygons entirely.
    """
    # A pixel is assigned exactly one home tile (see aoi.assign_home_tiles), so
    # a scene only ever contributes the pixels that belong to its tile. This is
    # both the exact spatial filter and the guard against a seam pixel getting
    # two differently-corrected observations for one acquisition.
    if "home_tile" in pixels.columns and scene.get("tile"):
        pixels = pixels[pixels["home_tile"] == scene["tile"]]
        if pixels.empty:
            return pixels
    # A tile's UTM zone is its leading digits (32VNM -> 32). Only pixels gridded
    # in that zone can be read from it without warping; without home tiles this
    # is what keeps a multi-zone AOI from offering another zone's pixels.
    zone = tile_utm_crs(scene.get("tile") or "")
    if zone and "utm_crs" in pixels.columns:
        pixels = pixels[pixels["utm_crs"] == zone]
        if pixels.empty:
            return pixels

    bbox = scene.get("bbox")
    if bbox is None or len(bbox) < 4:
        return pixels
    w, s_, e, n = bbox[0], bbox[1], bbox[2], bbox[3]
    m = (
        (pixels["lon"] >= w)
        & (pixels["lon"] <= e)
        & (pixels["lat"] >= s_)
        & (pixels["lat"] <= n)
    )
    return pixels[m]
