"""Paths, constants and the processing recipe shared by every operational step.

The band/masking recipe here deliberately mirrors `scripts/c2c_ts_server.py`
(`_dense_masked_collection`, `_dense_pixel_series`) so that a pixel processed
operationally is the same pixel the inspector shows. Where CDSE cannot
reproduce Earth Engine exactly, the difference is named in ONE place — see
CLOUD_MASK_NOTE below — rather than being spread through the reader.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


# ── environment ──────────────────────────────────────────────────────────────
def read_env_file(path: Path, skip: frozenset[str] = frozenset()) -> None:
    """Load KEY=VALUE lines into os.environ without overriding what is set.

    Blank values are ignored (an empty placeholder is not a setting), so a
    copied .env.example cannot mask a value from a later source. A ` #` after
    the value starts a comment. scripts/forestmon_cycle.sh parses the same way.
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


# Settings from a .env in the working directory, read here so they are in place
# however the package is entered. The CDSE keys are left to `cdse_auth`, which
# keeps ~/.config/cdse/credentials ahead of a .env on the project drive.
read_env_file(
    Path.cwd() / ".env",
    skip=frozenset(
        {"CDSE_S3_ACCESS_KEY", "CDSE_S3_SECRET_KEY"}
    ),  # pragma: allowlist secret
)


# ── layout ───────────────────────────────────────────────────────────────────
# Nothing is written next to the code. Every artefact lives under one data
# directory, chosen per deployment (FORESTMON_DATA_DIR) and defaulting to
# ./data relative to where the CLI is run. Directories are created on demand by
# `ensure_dirs()`, never as a side effect of import.
def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name) or default).expanduser()


DATA = _env_path("FORESTMON_DATA_DIR", Path.cwd() / "data")
OPS = DATA / "operational"  # everything this package writes

# AOI polygons: one GeoParquet per AOI, named `<aoi>.parquet` (the legacy
# `<aoi>_union.parquet` name is also accepted). The AOI name used on the
# command line is the file stem.
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
PARITY_DIR = OPS / "parity"  # CDSE-vs-GEE reports
RUNS_DIR = OPS / "runs"  # run ledger


def ensure_dirs() -> None:
    """Create the output tree. Reads the module globals at call time, so a
    test (or a caller) that repoints them gets its own tree."""
    for d in (
        OPS,
        GRID_DIR,
        CATALOG_DIR,
        STATE_DIR,
        OBS_DIR,
        ALERTS_DIR,
        PARITY_DIR,
        RUNS_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


def aoi_path(aoi_name: str) -> Path:
    """The polygon file for an AOI, with an error that says where we looked."""
    try:
        return AOI_PARQUET[aoi_name]
    except KeyError:
        known = ", ".join(AOI_PARQUET) or "none"
        raise KeyError(
            f"unknown AOI {aoi_name!r}: no {aoi_name}.parquet in {AOI_DIR} "
            f"(known: {known}; set FORESTMON_AOI_DIR)"
        ) from None


# Earth Engine is used only by `parity check`. The Cloud project is per user;
# unset, Earth Engine falls back to the one in the user's credentials.
EE_PROJECT = os.environ.get("FORESTMON_EE_PROJECT") or None

# Event logs (JSON Lines, one file per AOI) and the published products.
LOG_DIR = _env_path("FORESTMON_LOG_DIR", DATA / "logs")
WEB_OUT = _env_path("FORESTMON_WEB_OUT", DATA / "web")
GIS_OUT = _env_path("FORESTMON_GIS_OUT", DATA / "published")
# OPERA DIST-ALERT points, the independent-agreement term in alert confidence.
# Absent, that term is simply never satisfied.
OPERA_PARQUET = _env_path("FORESTMON_OPERA_PARQUET", DATA / "opera_dist_alerts.parquet")

# ── CDSE ─────────────────────────────────────────────────────────────────────
STAC_URL = "https://stac.dataspace.copernicus.eu/v1"
S2_L2A_COLLECTION = "sentinel-2-l2a"

# CDSE serves band assets from its own S3-compatible store. Keys are generated
# per user at https://eodata-s3keysmanager.dataspace.copernicus.eu/ and are NOT
# the same thing as the account password used for the OData/zipper endpoint.
S3_ENDPOINT = (
    os.environ.get("CDSE_S3_ENDPOINT") or "https://eodata.dataspace.copernicus.eu"
)
S3_KEY_ENV = "CDSE_S3_ACCESS_KEY"
S3_SECRET_ENV = "CDSE_S3_SECRET_KEY"  # pragma: allowlist secret

# GDAL/CURL tuning for the JP2 range reads. Kept here, and overridable from the
# environment, so the transport can be swept on a test machine without editing
# code — see `forestmon bench-gdal`.
#
# Measured shape of one scene (kalklindeskog, 17,179 px on T32VNL): B02 is
# 10980^2 in 1024x1024 blocks and our pixels touch 9 of 121 of them; B11 is
# 640x640 blocks, 9 of 81. That is ~19 MB decoded per 10 m band to extract
# 0.034 MB of wanted pixels — ~550x read amplification, and close to
# irreducible, since the polygons are clustered and 9 blocks is about the
# smallest set that covers them. So the cost is (a) 7 opens, (b) ~63 block
# range-GETs, (c) ~90 MB of JPEG 2000 decode. Anything that speeds this up has
# to attack one of those three, which is what the options below are for.
GDAL_OPTS = {
    # Never list a SAFE directory to open one file.
    "GDAL_DISABLE_READDIR_ON_OPEN": (
        os.environ.get("FORESTMON_GDAL_READDIR") or "EMPTY_DIR"
    ),
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".jp2,.xml",
    "GDAL_HTTP_MAX_RETRY": "5",
    "GDAL_HTTP_RETRY_DELAY": "2",
    "VSI_CACHE": "TRUE",
    # 25 MB per opened file. This is compressed VSI byte-range cache, not GDAL's
    # decoded raster block cache; larger values only help if the JP2 driver
    # revisits compressed ranges and multiply by the seven concurrent files.
    "VSI_CACHE_SIZE": (os.environ.get("FORESTMON_VSI_CACHE_SIZE") or "26214400"),
    # Measured 2026-08-19. GDAL already defaults MULTIPLEX and
    # MERGE_CONSECUTIVE_RANGES to YES, but they only act on ReadMultiRange calls
    # (and multiplexing additionally needs HTTP/2). Forcing HTTP/2 produced no
    # reliable gain and was 22% slower in the final controlled head-to-head, so
    # leave HTTP_VERSION unset.
    "GDAL_HTTP_VERSION": (os.environ.get("FORESTMON_HTTP_VERSION") or ""),
    "GDAL_HTTP_MULTIPLEX": (os.environ.get("FORESTMON_HTTP_MULTIPLEX") or ""),
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": (
        os.environ.get("FORESTMON_MERGE_RANGES") or ""
    ),
    # JP2 decode is CPU-bound at ~90 MB/scene. A balanced 18-unique-scene
    # crossover at the real two-scene concurrency found 2 threads won 8/9
    # batches: 1.91x median paired and 3.52x total-throughput speedup, with exact
    # parity. ALL_CPUS caused severe long-tail stalls. Still overridable for a
    # machine with materially different CPU or scene concurrency.
    "GDAL_NUM_THREADS": (os.environ.get("FORESTMON_GDAL_NUM_THREADS") or "2"),
}


def gdal_opts() -> dict:
    """GDAL_OPTS with empty values dropped — an empty string is not 'unset'."""
    return {k: v for k, v in GDAL_OPTS.items() if v != ""}


# ── the processing recipe ────────────────────────────────────────────────────
# S-CCD wants blue,green,red,nir,swir1,swir2. pyxccd is fed in exactly this
# order; PYX_BANDS in c2c_ts_server.py is the same list in Earth Engine naming.
BANDS = ["B02", "B03", "B04", "B08", "B11", "B12"]
GEE_BAND_ALIAS = {
    "B02": "B2",
    "B03": "B3",
    "B04": "B4",
    "B08": "B8",
    "B11": "B11",
    "B12": "B12",
}

# CDSE assets are per-band AND per-resolution. B11/B12 are natively 20 m; Earth
# Engine's getRegion at scale=10 hands back the 20 m value of the containing
# pixel (nearest neighbour), so reading the 20 m asset and locating the
# containing cell reproduces it exactly, with no resampling of our own.
ASSET_FOR_BAND = {
    "B02": "B02_10m",
    "B03": "B03_10m",
    "B04": "B04_10m",
    "B08": "B08_10m",
    "B11": "B11_20m",
    "B12": "B12_20m",
}
SCL_ASSET = "SCL_20m"

NATIVE_RES = {"B02": 10, "B03": 10, "B04": 10, "B08": 10, "B11": 20, "B12": 20}
GRID_RES = 10  # the grid we carry state on

# Scene Classification values we refuse, matching the GEE side exactly:
#   3 cloud shadow, 8 cloud medium prob, 9 cloud high prob, 10 thin cirrus,
#   11 snow/ice. Snow matters here — at 58-61 N it removes most of Nov-Feb.
SCL_REJECT = (3, 8, 9, 10, 11)

# CLOUD_MASK_NOTE ------------------------------------------------------------
# The Earth Engine recipe ALSO requires s2cloudless probability < 40, from
# COPERNICUS/S2_CLOUD_PROBABILITY. That collection is a Sentinel Hub product
# hosted by Google; it is not in the CDSE SAFE archive, so the operational mask
# is SCL-only and is therefore LOOSER than the inspector's. Consequence: the
# operational series may retain some cloud-edge observations the inspector
# drops. Parity is therefore checked on RAW REFLECTANCE per (pixel, scene) —
# a comparison masking policy cannot bias — and the mask difference is reported
# separately by `parity check` rather than being silently averaged in.
S2CLOUDLESS_MAX_PROB = 40  # GEE-side threshold, for reference

# Reflectance handling. Baseline 04.00 (2022-01-25 onward) shifted L2A DNs by
# +1000; GEE's *_HARMONIZED collections undo that so the whole archive is on
# one scale. We read BOA_ADD_OFFSET from each product's metadata and apply it,
# which reproduces HARMONIZED rather than assuming a fixed baseline date.
DN_SCALE = 10000.0  # DN -> reflectance divisor
NODATA_DN = 0  # L2A JP2 nodata

# pyxccd expects Landsat-style 0-10000 reflectance, which is what S2 L2A DNs
# already are after the offset — so no rescaling, same as the GEE path.
PYX_VALID_RANGE = (0, 10000)

# S-CCD parameters, carried over from the label-tuned inspector config
# (SCCD_P_CG / SCCD_CONSE in c2c_ts_server.py). Kept here so the operational
# run and the inspector cannot drift apart unnoticed.
SCCD_P_CG = 0.90
SCCD_CONSE = 6
PYX_LAM = 20

# Archive start. S2A+S2B only reach useful density over Norway from 2017.
HISTORY_START = "2017-01-01"

# When operational alerting starts. Breaks dated before this are recorded with
# status "historical": the bootstrap fit finds every disturbance since 2017, and
# emitting nine years of them as new alerts would bury the ones that happened
# this week under thousands that were never actionable.
MONITORING_START = os.environ.get("FORESTMON_MONITORING_START") or "2026-01-01"

# Keep cloud/shadow/snow-rejected observations in the store as well as clear
# ones. Off by default: it multiplies the store ~7x (0.21 -> 1.53 GB measured)
# AND removes the reader's early-out on fully-clouded scenes, which is what
# makes those scenes cheap to skip. Worth turning ON before a backfill if the
# masking recipe is ever likely to be revisited, because re-reading the rejects
# afterwards costs ~2 TB of CDSE traffic; worthless to turn on afterwards.
STORE_ALL_OBSERVATIONS = (os.environ.get("FORESTMON_STORE_ALL") or "0") == "1"

# Concurrency budget. Band threads x scene workers = simultaneous connections to
# CDSE, and measured throughput PEAKS at ~14 and degrades past it (22.9
# scenes/min at 2 scene workers, 8.5-9.2 at 4-8). These two knobs cannot be
# tuned independently, so the default is expressed as one budget.
MAX_CDSE_CONNECTIONS = int(os.environ.get("FORESTMON_MAX_CONNECTIONS") or "14")
SCENE_WORKERS = 2  # 2 x 7 band threads = 14

# Scenes per commit during ingest. Each batch writes its own observation part
# file and its own ledger rows, so an interrupted backfill loses at most this
# many scenes of work rather than the whole run.
INGEST_BATCH = int(os.environ.get("FORESTMON_INGEST_BATCH") or "100")

# ── provenance ───────────────────────────────────────────────────────────────
# Bumped whenever the recipe above changes in a way that invalidates fitted
# state. Stamped into state files and alerts so an alert is always traceable to
# the rules that produced it.
RECIPE_VERSION = "1"
