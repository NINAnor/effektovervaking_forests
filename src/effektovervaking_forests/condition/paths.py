"""Shared paths and constants for the condition pipeline.

Layout mirrors DegreeofRecovery/paper_release: a `data/` tree that holds inputs
and caches, an `outputs/` tree that holds everything the steps generate, both
under one root.
"""

from __future__ import annotations

import os
from pathlib import Path

from effektovervaking_forests.config import DATA as _FORESTMON_DATA
from effektovervaking_forests.config import _env_path

# Where the run keeps its `data/` and `outputs/` trees: CONDITION_ROOT, else
# `<FORESTMON_DATA_DIR>/condition`. Point CONDITION_ROOT at an existing
# condition/ directory to run against its cache and result set without copying
# tens of GB (see docs/condition/README.md, "Configure"). Like the rest of the
# package, nothing is created at import time -- see `ensure_dirs()`.
ROOT = _env_path("CONDITION_ROOT", _FORESTMON_DATA / "condition")

DATA = ROOT / "data"
CACHE = DATA / "cache"
OUT = ROOT / "outputs"
OUT_DATA = OUT / "data"
OUT_FIG = OUT / "figures"
OUT_REPORT = OUT / "report"

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
# The mapped polygons for both forest types. Overridable so the pipeline can be
# pointed at a copy without editing code.
# Site default: the NINA project share this pipeline was deployed on. It is the
# only absolute path in the package, and it is a default rather than a
# requirement -- set FORESTS_KARTLEGGING_GPKG to run anywhere else.
DEFAULT_KARTLEGGING_GPKG = Path(
    "/data/P-Prosjekter2/156004_08_effektovervaking_2026_2030_opsjon_2_fjernmaling"
    "/Data/Olivinskog_kalklindeskog/skog_kartlegginger.gpkg"
)
KARTLEGGING_GPKG = Path(
    os.environ.get("FORESTS_KARTLEGGING_GPKG", DEFAULT_KARTLEGGING_GPKG)
)

# The six polygon layers, and which forest type each belongs to.
LAYER_TYPE = {
    "MI_olivinskog": "olivinskog",
    "NiN5k_olivinskog": "olivinskog",
    "HB13_olivinskog": "olivinskog",
    "MI_kalklindeskog": "kalklindeskog",
    "NiN5k_kalklindeskog": "kalklindeskog",
    "HB13_kalklindeskog": "kalklindeskog",
}
FOREST_TYPES = ("olivinskog", "kalklindeskog")

# Per-layer stable feature id column (see extract_alphaearth_embeddings.py).
ID_COLUMN = {
    "MI_olivinskog": "identifikasjon_lokalId",
    "MI_kalklindeskog": "identifikasjon_lokalId",
    "HB13_olivinskog": "identifikasjon_lokalId",
    "HB13_kalklindeskog": "identifikasjon_lokalId",
    "NiN5k_olivinskog": "Kartleggin",
    "NiN5k_kalklindeskog": "Kartleggin",
}

# ---------------------------------------------------------------------------
# AlphaEarth on Source Cooperative
# ---------------------------------------------------------------------------
AEF_INDEX_URL = "https://data.source.coop/tge-labs/aef/v1/annual/aef_index.parquet"
AEF_INDEX = CACHE / "aef_index.parquet"
AEF_YEARS = tuple(range(2017, 2026))  # 2017-2025 inclusive
BANDS = tuple(f"A{i:02d}" for i in range(64))
AEF_NODATA = -128
# The COGs store the unit-normalised float embedding quantised to int8. A single
# global scale factor cancels out of cosine distance (the metric both the
# trajectory axis and the DoR score use), so nothing downstream depends on the
# exact value -- see scripts/09_check_gee_parity.py.
AEF_INT8_SCALE = 1.0 / 127.0

# One COG block on the ground: 1024 px x 10 m. Both readers address blocks in
# metres, so this is the constant that ties them to the same grid.
BLOCK_M = 1024 * 10.0

# How the stored int8 becomes a float embedding. THIS IS NOT A FREE CHOICE and
# the two options are not a scale apart -- see `condition.aef.dequantise`.
#
#   "linear"         v = raw / 127         what this pipeline has always applied
#   "signed_square"  v = (raw/127.5)^2 * sign(raw)    what Earth Engine returns
#
# `scripts/09_check_gee_parity.py` measured both against
# GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL over 400 pixels of this AOI:
#
#              cosine to GEE   implied scale   ||v||     max |GEE - ours|
#   linear         0.9617          0.380       2.531          0.2598
#   signed_square  1.0000          1.000       1.000          0.0111
#
# AlphaEarth embeddings are unit-norm by construction, and only the second
# recovers that. So "linear" is wrong, and wrong in DIRECTION rather than in
# scale, which is the one kind of error a cosine-based score cannot absorb.
#
# The default is "signed_square" as of the 2026-09-09 re-run: the whole pipeline
# was re-extracted and re-scored on it, and everything in outputs/ and docs/ now
# comes from it. "linear" is kept only to reproduce the pre-2026-09-09 results,
# which were computed on an embedding that is not the one Earth Engine serves.
AEF_DEQUANT = os.environ.get("AEF_DEQUANT", "signed_square")

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
POLYGONS = OUT_DATA / "polygons.parquet"
POLY_PIXELS = OUT_DATA / "polygon_pixels.parquet"
TRAJECTORY = OUT_DATA / "trajectory.parquet"
CONDITION = OUT_DATA / "condition.parquet"
COMBINED = OUT_DATA / "condition_combined.csv"

# ---------------------------------------------------------------------------
# Overlap-safe pixel model (see condition.membership)
# ---------------------------------------------------------------------------
# The mapped layers overlap: 2,237 ha of polygon area sits on 1,694 ha of
# ground. A pixel therefore belongs to a SET of polygons, not to one, so the
# embeddings are stored once per (utm_zone, x, y, year) and the polygon
# membership is a separate many-to-many table.
PIXELS = OUT_DATA / "pixels.parquet"  # pixel_id, x, y, year, A00..A63
PIXEL_POLYGONS = OUT_DATA / "pixel_polygons.parquet"  # pixel_id <-> poly_id (m:n)
POLY_OVERLAP = OUT_DATA / "polygon_overlap.parquet"  # poly_id <-> poly_id, shared area

# ---------------------------------------------------------------------------
# ESA WorldCover -- the "transformed" bad pool (see condition.worldcover)
# ---------------------------------------------------------------------------
# v200 is the 2021 epoch, 10 m, EPSG:4326, in 3x3 degree COG tiles served
# anonymously over plain HTTPS.
WORLDCOVER_URL = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_{ns}{lat:02d}{ew}{lon:03d}_Map.tif"
)
WORLDCOVER_YEAR = 2021
# Built-up and cropland only: the classes that are unambiguously transformed
# land in Norway. Grassland (30) is excluded because it is mostly natural fjell
# and pasture here, and bare/sparse (60) because Norwegian bare ground is
# overwhelmingly rock and scree rather than anything a forest was turned into.
TRANSFORMED_CLASSES = (40, 50)
# The bad pool is drawn from a ring around each stand, as in the
# DegreeofRecovery paper: far enough out not to be the stand's own edge, close
# enough to share its region, terrain and acquisition history.
ANNULUS_INNER_M = 3_000.0
ANNULUS_OUTER_M = 8_000.0

BAD_POOL_PLAN = OUT_DATA / "bad_pool_plan.parquet"  # which blocks to read, and why
BAD_PIXELS = OUT_DATA / "bad_pixels.parquet"  # the transformed reference pixels
BAD_MEMBERSHIP = (
    OUT_DATA / "bad_membership.parquet"
)  # bad pixel <-> the stands it serves
PIXEL_CONDITION = OUT_DATA / "pixel_condition.parquet"  # the per-pixel scores

# ---------------------------------------------------------------------------
# Hansen Global Forest Change -- the independent disturbance reference
# ---------------------------------------------------------------------------
# Landsat-based, annual, produced by someone else, so it is the one label in
# this pipeline that is not derived from AlphaEarth. It calibrates the
# trajectory step's slope gate (see condition.trend) and supplies the recall
# figures in 05_validate.py. It is a sensitivity floor, not a ground truth: it
# sees canopy removal and is blind to the selective removal and understorey
# change these habitat types are mostly lost to.
HANSEN_PIXELS = OUT_DATA / "hansen_pixels.parquet"

# ---------------------------------------------------------------------------
# Rasters
# ---------------------------------------------------------------------------
OUT_TIF = OUT / "tif"


def ensure_dirs() -> None:
    """Create the output tree. Every step entry point calls this first."""
    for d in (CACHE, OUT_DATA, OUT_FIG, OUT_REPORT, OUT_TIF):
        d.mkdir(parents=True, exist_ok=True)
