# The condition pipeline (`forestmon condition`)

Stand condition and trajectory for **olivinskog** and **kalklindeskog** from
AlphaEarth annual embeddings (2017-2025). The library is the subpackage
`src/effektovervaking_forests/condition/`; the numbered steps are modules in
`src/effektovervaking_forests/condition/steps/` (`sNN_*.py`, each with
`main(argv)`). The method and the evidence behind each default are in
[METHODS.md](METHODS.md).

| | |
|---|---|
| **Question** | Where has each stand arrived, and which way is it moving? |
| **Input** | 737 mapped stands (`skog_kartlegginger.gpkg`) + AlphaEarth 2017-2025 |
| **Output** | a condition score and a trajectory class per 10 m cell and per stand |
| **Cost** | about 30 min wall clock, about 50 GB read; Earth Engine for step 02e |
| **Scale** | 168,832 cells, 2.0 M pixel-year scores, 5 UTM zones |
| **Verified** | stand AUC 0.836 against the field labels (`local`); trajectory recall on Hansen clearing 0.33 at 2 % FPR (cell grain) |

Embeddings are downloaded from the Source Cooperative mirror with
[`aef_loader_plus`](https://github.com/Geethen/aef_loader_plus) (no account
needed).

## Setup

Python >= 3.12 and the repo's uv environment (numba is a regular dependency).
The Earth Engine client is an extra, needed only for step 02e:

```bash
uv sync                 # base install
uv sync --extra gee     # + earthengine-api, for step 02e
uv run forestmon condition doctor
```

`doctor` checks the stand GeoPackage, the GDAL GeoTIFF driver and
`earthengine-api`, and shows where the pipeline will write.

### Inputs

| what | where | notes |
|---|---|---|
| mapped stands | `skog_kartlegginger.gpkg`, six layers | `FORESTS_KARTLEGGING_GPKG` |
| AlphaEarth 2017-2025 | [Source Cooperative `tge-labs/aef`](https://source.coop/tge-labs/aef) | anonymous HTTPS |
| ESA WorldCover v200 | AWS `esa-worldcover` | anonymous HTTPS, 2021 epoch |
| Hansen GFC (required by step 04) | Earth Engine `UMD/hansen/global_forest_change_2025_v1_13` | needs an Earth Engine project |

### Configuration

Environment variables, or a `.env` in the working directory (real variables
win; see `.env.example`).

| variable | default | meaning |
|---|---|---|
| `CONDITION_ROOT` | `<FORESTMON_DATA_DIR>/condition` | data root: `data/cache/` and `outputs/` |
| `FORESTS_KARTLEGGING_GPKG` | `<CONDITION_ROOT>/data/skog_kartlegginger.gpkg` | stand polygons |
| `FORESTMON_EE_PROJECT` | none | Earth Engine project; default for `condition run --ee-project` |
| `FORESTMON_LOG_DIR` | `<FORESTMON_DATA_DIR>/logs` | `condition run` logs to `<FORESTMON_LOG_DIR>/condition/` |

Directories are created when a step starts, never at import. Allow about 50 GB
of tile cache; the products are about 12 MB of GeoTIFF and a 71 MB GeoPackage.

## Running

```bash
forestmon condition doctor                  # check inputs and the data root
forestmon condition steps                   # list steps in run order
forestmon condition run                     # all steps, fail-fast
forestmon condition run --from 04 --to 07   # a slice
forestmon condition run --dry-run           # print the plan and exit
forestmon condition step 05                 # one step
forestmon condition step 03 -- --help       # that step's own arguments
```

`run` stops at the first failing step and tees each step to
`<FORESTMON_LOG_DIR>/condition/<stamp>_<id>_<name>.log`. Without `--ee-project`
(or `FORESTMON_EE_PROJECT`) step 02e is skipped; `run` then refuses to start
step 04 unless `outputs/data/hansen_pixels.parquet` already exists. Arguments
after `--` go to the step.

## Steps

Run order is 00, 02, 02b, 02c, 02d, 02e, 04, 03, 05, 06, 07. Step 03 reads the
yearly scores that step 04 writes.

| step | module | does | writes |
|---|---|---|---|
| `00` | `s00_build_polygons` | polygons, roles, survey years, 5 km spatial blocks | `polygons.parquet`, `polygons_geo.parquet` |
| `02` | `s02_extract_embeddings` | block-aligned download of every stand pixel, 9 years (about 31 GB) | `polygon_pixels.parquet` |
| `02b` | `s02b_membership` | overlap-safe pixel-to-stand membership model | `pixels`, `pixel_polygons`, `pixel_roles`, `pixel_sites`, `polygon_overlap` |
| `02c` | `s02c_plan_bad_pool` | set cover: fewest AlphaEarth blocks giving every stand a transformed ring | `bad_pool_plan`, `bad_candidates` |
| `02d` | `s02d_extract_bad_pool` | transformed-pool download (about 20 GB) | `bad_pixels`, `bad_membership`, `bad_sites` |
| `02e` | `s02e_hansen_reference` | Hansen loss year per pixel (needs Earth Engine, `--extra gee`) | `hansen_pixels.parquet` |
| `04` | `s04_condition` | kNN DoR per pixel-year at both scopes, stand median and interval, thresholds | `pixel_condition`, `condition`, `threshold.json`, `whitener.json` |
| `03` | `s03_trajectory` | z-score, Theil-Sen, Mann-Kendall, calibrated gates | `trajectory`, `trajectory_pixels`, `trajectory_gate.json` |
| `05` | `s05_validate` | five checks against the field labels; read this output | `condition_combined.csv` + printed report |
| `06` | `s06_rasterize` | 20-band GeoTIFF per spatial block, on the AlphaEarth grid | `outputs/tif/*.tif`, `*.vrt`, `index.csv` |
| `07` | `s07_package` | one GeoPackage for QGIS | `outputs/condition_results.gpkg` |

The downloads (02, 02d) and 04 are the long steps; the others take seconds to
minutes. The embedding reader fetches whole 1024x1024 band-interleaved blocks
once and cuts polygons out in memory (single-pixel windows cost the same as a
block). Downloads are cached per (tile, year) under `data/cache/`, so a re-run
only fetches what is missing.

## Outputs

Paths are relative to `CONDITION_ROOT`.

| file | one row per | key columns |
|---|---|---|
| `outputs/condition_results.gpkg` | 10 m cell (168,832) and stand (737) | the deliverable: 20 measurements per cell, the stand verdict, a `readme` layer, QGIS styles, EPSG:25833 |
| `outputs/tif/*.tif` | raster cell | 66 files (one per spatial block), 20 float32 bands, plus one `.vrt` per UTM zone |
| `pixel_condition.parquet` | pixel x year | `dor_local`, `dor_global`, `m_good_*`, `m_bad`, `label_age` |
| `condition.parquet` | stand x year | `dor_*` (median of cells), `ci_*`, `condition_*`, `dor_*_frac_poor` |
| `trajectory.parquet` (+ `_pixels`) | stand / pixel | `trajectory`, `sen_slope`, `mk_z`, `mk_p`, `c_2017..c_2025` |
| `condition_combined.csv` | stand | both results at the latest year |
| `threshold.json`, `whitener.json`, `trajectory_gate.json` | - | every fitted quantity, for audit |

Rules for reading the output:

1. Compare `dor_global` with `threshold_global` **per stand**, not with one AOI
   cutoff. The threshold is fitted without the stand's own spatial block, so it
   varies by block on purpose.
2. Read `m_good_*` beside `dor_*`; at the `global` scope part of the score is
   distance from converted land. Step 05 warns when this holds.
3. Check `edge_m`: below 20 m a 10 m cell mixes the stand with its neighbour,
   and 428 of 737 stands have no pixel above that line.

Unscoreable stands are `NaN`, with the reason recoverable from
`n_good`/`n_bad`/`n_valid`; nothing is silently dropped.

## Running against an existing data root

To reuse a data root that already holds `data/cache/` and `outputs/` (and avoid
re-downloading about 50 GB), point `CONDITION_ROOT` at it:

```bash
export CONDITION_ROOT=/path/to/condition
export FORESTS_KARTLEGGING_GPKG=/path/to/skog_kartlegginger.gpkg   # if not under $CONDITION_ROOT/data/
forestmon condition doctor
```

The steps then **overwrite** that root's `outputs/` and add to its cache; copy
`outputs/` first if you want to compare.

## When the data changes

* **A new AlphaEarth year.** Add it to `AEF_YEARS` in
  `src/effektovervaking_forests/condition/paths.py` and run `forestmon condition
  run --from 02`; only the new year downloads.
* **A new field survey or edited stand polygons.** Re-run from `00` (labels,
  survey years, blocks, thresholds and the bad-pool set cover all depend on them).
* **A different AOI or forest type.** Edit `LAYER_TYPE` and `ID_COLUMN` in
  `paths.py`, then re-run everything. The method needs a good pool of the same
  habitat type, which `tilstand` supplies here.

## Tests

```bash
uv run pytest tests/condition                # offline
uv run pytest tests/condition -m network     # the few that hit the mirror
```

## Limitations to carry into any report

* The bad pool is converted land, not degraded forest; report `m_good` too.
* Only 36 degraded labels (in six localities) bound every accuracy figure;
  olivinskog is the weaker AOI (fewer donors, 12 degraded stands).
* Hansen sees canopy removal only; trajectory `declining` is a screening flag.
* `local` covers 44 fewer stands than `global`; say which scope a figure uses.

See [METHODS.md](METHODS.md) section 11 for the full list.
