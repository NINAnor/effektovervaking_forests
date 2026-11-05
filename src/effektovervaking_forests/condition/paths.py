"""Shared paths and constants for the condition pipeline: a `data/` tree (inputs,
caches) and an `outputs/` tree under one root."""

from __future__ import annotations

from effektovervaking_forests.config import DATA as _FORESTMON_DATA
from effektovervaking_forests.config import _env_path

# CONDITION_ROOT, else `<FORESTMON_DATA_DIR>/condition`. Nothing is created at
# import time; see `ensure_dirs()`.
ROOT = _env_path("CONDITION_ROOT", _FORESTMON_DATA / "condition")

DATA = ROOT / "data"
CACHE = DATA / "cache"
OUT = ROOT / "outputs"
OUT_DATA = OUT / "data"

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
# The mapped stand polygons for both forest types (six layers, see LAYER_TYPE).
KARTLEGGING_GPKG = _env_path(
    "FORESTS_KARTLEGGING_GPKG", DATA / "skog_kartlegginger.gpkg"
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

# Per-layer stable feature id column.
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
# One COG block on the ground: 1024 px x 10 m. Both readers address blocks in metres.
BLOCK_M = 1024 * 10.0


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
# Layers overlap, so a pixel belongs to a SET of polygons: embeddings are stored
# once per (utm_zone, x, y, year) and membership is a separate m:n table.
PIXELS = OUT_DATA / "pixels.parquet"  # pixel_id, x, y, year, A00..A63
PIXEL_POLYGONS = OUT_DATA / "pixel_polygons.parquet"  # pixel_id <-> poly_id (m:n)
POLY_OVERLAP = OUT_DATA / "polygon_overlap.parquet"  # poly_id <-> poly_id, shared area

# ---------------------------------------------------------------------------
# ESA WorldCover -- the "transformed" bad pool (see condition.worldcover)
# ---------------------------------------------------------------------------
# v200 is the 2021 epoch, 10 m, EPSG:4326, in 3x3 degree COG tiles (anonymous HTTPS).
WORLDCOVER_URL = (
    "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
    "ESA_WorldCover_10m_2021_v200_{ns}{lat:02d}{ew}{lon:03d}_Map.tif"
)
WORLDCOVER_YEAR = 2021
# Built-up and cropland only; grassland (30) and bare/sparse (60) are mostly
# natural fjell, pasture and rock in Norway.
TRANSFORMED_CLASSES = (40, 50)
# The bad pool is drawn from a ring around each stand (inner radius clears the
# stand's own edge).
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
# Independent (Landsat) disturbance reference: calibrates the trajectory slope
# gate and supplies the recall figures in 05_validate.py. A sensitivity floor,
# not ground truth (blind to selective removal).
HANSEN_PIXELS = OUT_DATA / "hansen_pixels.parquet"

# ---------------------------------------------------------------------------
# Rasters
# ---------------------------------------------------------------------------
OUT_TIF = OUT / "tif"


def ensure_dirs() -> None:
    """Create the output tree. Every step entry point calls this first."""
    for d in (CACHE, OUT_DATA, OUT_TIF):
        d.mkdir(parents=True, exist_ok=True)
