"""Read per-pixel Sentinel-2 L2A reflectance from CDSE (JP2 on S3) or EOPF (Zarr).

Our pixels are a tiny scattered fraction of a 110 km tile, so only the
internal blocks they fall in are read, via GDAL byte-range reads: /vsis3 with
CDSE keys, or the Zarr driver over public HTTPS for EOPF.
Three details keep values matching Earth Engine:

* BOA_ADD_OFFSET is read from each product's metadata (not a fixed baseline
  date), reproducing the *_HARMONIZED scale.
* B11/B12 are read from the native 20 m asset at the containing cell.
* No warping: a scene whose CRS differs from the pixel's is skipped.
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

# A scene is seven files (six bands + SCL), each a separate network open, so
# they are read concurrently. Multiplies with the scene workers in
# pipeline.ingest; band threads x scene workers = ~14 concurrent CDSE connections.
BAND_WORKERS = 7


def _install_s3_env() -> None:
    """Put CDSE's S3 settings into the process environment for GDAL.

    rasterio 1.5 rejects AWS_* options in `rasterio.Env(...)` (it wants a boto3
    session), so they are set in the environment. CDSE needs path-style
    addressing (AWS_VIRTUAL_HOSTING=FALSE).
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
            # CDSE has no region; an unset one makes GDAL's signer pick a default
            # that CDSE rejects on some endpoints.
            "AWS_DEFAULT_REGION": (os.environ.get("AWS_DEFAULT_REGION") or "default"),
        }
    )


def _gdal_env(href: str) -> rasterio.Env:
    """The GDAL config for reading `href`; CDSE keys only for s3:// hrefs."""
    if href.startswith("s3://"):
        _install_s3_env()
    return rasterio.Env(**config.GDAL_OPTS)


def gdal_path(href: str) -> str:
    """Asset href -> GDAL path: /vsis3 for CDSE, the Zarr driver for EOPF."""
    if href.startswith("s3://"):
        return href.replace("s3://", "/vsis3/")
    root, sep, array = href.partition(".zarr/")
    if sep:
        return f'ZARR:"/vsicurl/{root}.zarr":/{array}'
    return f"/vsicurl/{href}"


@functools.lru_cache(maxsize=4096)
def _boa_offsets_from_xml(xml_href: str) -> tuple[int, ...]:
    """BOA_ADD_OFFSET per band index, from a product's MTD_MSIL2A.xml.

    Returns a 13-long tuple in ESA band_id order. Pre-04.00 products have no
    offset element, which correctly yields zeros.
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
    """Every band's offset for one scene, fetched once.

    Called before the band threads start: `lru_cache` does not coalesce
    concurrent misses, so threads would each re-read the same XML.
    """
    return {b: boa_offset_for(scene, b) for b in config.BANDS}


def boa_offset_for(scene: pd.Series, band: str) -> int:
    """The offset to ADD to a raw DN to put it on the harmonized scale.

    ESA stores BOA_ADD_OFFSET as -1000 for baseline >= 04.00. If metadata is
    unreadable it falls back to the baseline rule with a warning: a wrong offset
    is a 0.1-reflectance step that S-CCD would report as a disturbance.
    """
    off = scene.get("boa_offset")
    if off is not None and pd.notna(off):
        return int(off)
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

    Returns (values, valid); `valid` marks points inside the raster. Whole
    internal blocks are read, not per-point windows (far fewer range requests).
    """
    with rasterio.open(path) as ds:
        # EOPF Zarr carries no CRS GDAL can see; its zone is the tile's
        # (`pixels_for_scene` already filtered on that).
        if ds.crs is not None and ds.crs.to_string() != expect_crs:
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

    `scl_first` reads SCL before the reflectance bands and returns early when
    nothing is usable (a granule's bbox can contain our pixels while its swath
    does not). Turn it off to get the full pixel set regardless of quality.
    """
    crss = pixels["utm_crs"].unique()
    if len(crss) != 1:
        # Multi-zone pixels must be split first; `pixels_for_scene` does.
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
        with _gdal_env(scl_href):
            scl, valid_scl = _read_at(gdal_path(scl_href), xs, ys, crs)
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
        with _gdal_env(href):  # Env is thread-local
            vals, valid = _read_at(gdal_path(href), xs, ys, crs)
        raw_nodata = vals == config.NODATA_DN
        if apply_offset:
            off = offsets[band]
            # DN 0 is nodata and stays 0. Clamp at 0 like HARMONIZED does (dark
            # pixels would otherwise go negative); nodata is judged on the RAW DN.
            vals = np.where(raw_nodata, 0, np.maximum(vals + off, 0))
        return band, vals, valid, raw_nodata

    def _read_scl():
        with _gdal_env(scl_href):
            return _read_at(gdal_path(scl_href), xs, ys, crs)

    with ThreadPoolExecutor(max_workers=BAND_WORKERS) as pool:
        futures = [pool.submit(_one_band, b) for b in config.BANDS]
        # Reuse SCL if scl_first already fetched it.
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

    # Nodata in any band = outside the swath, which is not the same as cloudy.
    df["nodata"] = raw_nodata_any[valid_all]
    df["clear"] = (~df["nodata"]) & (~df["scl"].isin(config.SCL_REJECT))
    return df


def tile_utm_crs(tile: str) -> str | None:
    """'32VNM' -> 'EPSG:32632' (granules are in the UTM zone their code names)."""
    m = re.match(r"^(\d{1,2})[C-X]", tile)
    return f"EPSG:{32600 + int(m.group(1))}" if m else None


def pixels_for_scene(pixels: pd.DataFrame, scene: pd.Series) -> pd.DataFrame:
    """Cheap pre-filter so we do not open a tile that holds none of our pixels.

    Uses the catalogue's granule bbox, which over-selects near swath edges; the
    read itself is the exact test.
    """
    # A pixel has one home tile (aoi.assign_home_tiles); a scene contributes
    # only its own tile's pixels.
    if "home_tile" in pixels.columns and scene.get("tile"):
        pixels = pixels[pixels["home_tile"] == scene["tile"]]
        if pixels.empty:
            return pixels
    # Only pixels gridded in the tile's UTM zone can be read without warping.
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
