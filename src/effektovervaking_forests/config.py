"""Paths, constants and the processing recipe shared by every operational step.

The band/masking recipe mirrors the Earth Engine inspector so a pixel processed
operationally matches what the inspector shows; the one known difference is in
CLOUD_MASK_NOTE below.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


# ── environment ──────────────────────────────────────────────────────────────
def read_env_file(path: Path, skip: frozenset[str] = frozenset()) -> None:
    """Load KEY=VALUE lines into os.environ without overriding what is set.

    Blank values are ignored; a ` #` after the value starts a comment.
    scripts/forestmon_cycle.sh parses the same way.
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = (part.strip() for part in line.split("=", 1))
        v = re.split(r"\s+#", v, maxsplit=1)[0].strip().strip('"').strip("'")
        if v and k not in skip and not os.environ.get(k):
            os.environ[k] = v


# Read the working-directory .env at import. CDSE keys are left to `cdse_auth`
# (home credentials file takes precedence).
read_env_file(
    Path.cwd() / ".env",
    skip=frozenset(
        {"CDSE_S3_ACCESS_KEY", "CDSE_S3_SECRET_KEY"}
    ),  # pragma: allowlist secret
)


# ── layout ───────────────────────────────────────────────────────────────────
# Every artefact lives under FORESTMON_DATA_DIR (default ./data); directories
# are created by `ensure_dirs()`, never on import.
def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name) or default).expanduser()


DATA = _env_path("FORESTMON_DATA_DIR", Path.cwd() / "data")
OPS = DATA / "operational"  # everything this package writes

# AOI polygons: one GeoParquet per AOI, `<aoi>.parquet` (or legacy
# `<aoi>_union.parquet`); the file stem is the AOI name.
AOI_DIR = _env_path("FORESTMON_AOI_DIR", DATA / "aoi")


def _discover_aois(aoi_dir: Path) -> dict[str, Path]:
    if not aoi_dir.is_dir():
        return {}
    return {p.stem.removesuffix("_union"): p for p in sorted(aoi_dir.glob("*.parquet"))}


AOI_PARQUET = _discover_aois(AOI_DIR)

GRID_DIR = OPS / "grid"  # per-AOI pixel grids
CATALOG_DIR = OPS / "catalog"  # per-AOI STAC scene indexes
STATE_DIR = OPS / "state"  # per-AOI S-CCD state
OBS_DIR = OPS / "obs"  # per-AOI observation store
ALERTS_DIR = OPS / "alerts"  # per-AOI break/alert tables
RUNS_DIR = OPS / "runs"  # run ledger


def ensure_dirs() -> None:
    """Create the output tree (reads module globals at call time)."""
    for d in (
        OPS,
        GRID_DIR,
        CATALOG_DIR,
        STATE_DIR,
        OBS_DIR,
        ALERTS_DIR,
        RUNS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


def aoi_path(aoi_name: str) -> Path:
    """The polygon file for an AOI; the error says where it looked."""
    try:
        return AOI_PARQUET[aoi_name]
    except KeyError:
        known = ", ".join(AOI_PARQUET) or "none"
        raise KeyError(
            f"unknown AOI {aoi_name!r}: no {aoi_name}.parquet in {AOI_DIR} "
            f"(known: {known}; set FORESTMON_AOI_DIR)"
        ) from None


# Earth Engine is used only by condition step 02e; unset falls back to the
# project in the user's credentials.
EE_PROJECT = os.environ.get("FORESTMON_EE_PROJECT") or None

# Event logs (JSON Lines, one file per AOI) and the published products.
LOG_DIR = _env_path("FORESTMON_LOG_DIR", DATA / "logs")
WEB_OUT = _env_path("FORESTMON_WEB_OUT", DATA / "web")
GIS_OUT = _env_path("FORESTMON_GIS_OUT", DATA / "published")
# OPERA DIST-ALERT points for the confidence agreement term (optional).
OPERA_PARQUET = _env_path("FORESTMON_OPERA_PARQUET", DATA / "opera_dist_alerts.parquet")

# ── Sentinel-2 sources ───────────────────────────────────────────────────────
# History comes from CDSE (STAC search is public; band reads need S3 keys).
# Scenes from EOPF_START on come from the EOPF Zarr service
# (https://explorer.eopf.copernicus.eu): public HTTPS, no keys, same product ids,
# but it only holds recent data (about April 2025 onward).
# FORESTMON_S2_SOURCE=cdse uses CDSE throughout.
STAC_URL = "https://stac.dataspace.copernicus.eu/v1"
EOPF_STAC_URL = "https://stac.core.eopf.eodc.eu"
S2_L2A_COLLECTION = "sentinel-2-l2a"  # same id on both
S2_SOURCE = os.environ.get("FORESTMON_S2_SOURCE") or "eopf"
if S2_SOURCE not in ("eopf", "cdse"):
    raise ValueError(f"FORESTMON_S2_SOURCE must be eopf or cdse, not {S2_SOURCE!r}")

# CDSE serves bands from its own S3 store; keys are generated per user at
# https://eodata-s3keysmanager.dataspace.copernicus.eu/ (not the account password).
S3_ENDPOINT = (
    os.environ.get("CDSE_S3_ENDPOINT") or "https://eodata.dataspace.copernicus.eu"
)
S3_KEY_ENV = "CDSE_S3_ACCESS_KEY"
S3_SECRET_ENV = "CDSE_S3_SECRET_KEY"  # pragma: allowlist secret

# GDAL/CURL settings for JP2 and Zarr byte-range reads (GDAL_NUM_THREADS=2 measured best
# at the pipeline's two-scene concurrency).
GDAL_OPTS = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "26214400",  # 25 MB compressed byte-range cache per file
    "GDAL_NUM_THREADS": "2",
}


# ── the processing recipe ────────────────────────────────────────────────────
# S-CCD band order: blue, green, red, nir, swir1, swir2.
BANDS = ["B02", "B03", "B04", "B08", "B11", "B12"]

# CDSE assets are per band and resolution. B11/B12 are natively 20 m; reading
# the 20 m asset at the containing cell matches Earth Engine's nearest-neighbour
# value at scale 10, with no resampling of our own.
ASSET_FOR_BAND = {
    "B02": "B02_10m",
    "B03": "B03_10m",
    "B04": "B04_10m",
    "B08": "B08_10m",
    "B11": "B11_20m",
    "B12": "B12_20m",
}
SCL_ASSET = "SCL_20m"

GRID_RES = 10  # the grid we carry state on

# SCL values rejected (as on the GEE side): 3 cloud shadow, 8 cloud medium,
# 9 cloud high, 10 thin cirrus, 11 snow/ice.
SCL_REJECT = (3, 8, 9, 10, 11)

# The Earth Engine recipe also masks s2cloudless probability >= 40, which the
# CDSE archive lacks: the operational mask is SCL-only and so looser, and may
# keep some cloud-edge observations. BOA_ADD_OFFSET (baseline 04.00+) is read
# per product and applied, reproducing GEE's *_HARMONIZED scale.
NODATA_DN = 0  # L2A JP2 nodata

# pyxccd expects 0-10000 reflectance, which offset-corrected L2A DNs already are.
PYX_VALID_RANGE = (0, 10000)

# S-CCD parameters, carried over from the label-tuned inspector config.
SCCD_P_CG = 0.90
SCCD_CONSE = 6
PYX_LAM = 20

# Archive start (S2A+S2B reach useful density over Norway from 2017).
HISTORY_START = "2017-01-01"

# Breaks dated before this are recorded as "historical", not new alerts.
MONITORING_START = os.environ.get("FORESTMON_MONITORING_START") or "2026-01-01"

# First acquisition date read from EOPF (with S2_SOURCE=eopf).
EOPF_START = os.environ.get("FORESTMON_EOPF_START") or MONITORING_START

# Also store rejected (cloud/shadow/snow) observations. Off by default: ~7x
# larger store and no early-out on fully clouded scenes. Only useful if set
# before a backfill.
STORE_ALL_OBSERVATIONS = (os.environ.get("FORESTMON_STORE_ALL") or "0") == "1"

SCENE_WORKERS = 2  # 2 x 7 band threads = 14

# Scenes per commit during ingest (bounds work lost on interruption).
INGEST_BATCH = int(os.environ.get("FORESTMON_INGEST_BATCH") or "100")

# ── provenance ───────────────────────────────────────────────────────────────
# Bump when the recipe changes in a way that invalidates fitted state; stamped
# into state files and alerts.
RECIPE_VERSION = "1"
