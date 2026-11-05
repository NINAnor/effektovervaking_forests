# Effektovervaking Forests (forestmon)

Forest monitoring for the NINA effect-monitoring programme. This repo hosts two
pipelines: the near-real-time S-CCD loss monitor (below) and the annual
AlphaEarth stand-condition pipeline (`forestmon condition`, see
[Condition pipeline](#condition-pipeline-annual)).

The loss monitor reads Sentinel-2 L2A directly from the Copernicus Data Space Ecosystem
(CDSE), keeps a per-pixel S-CCD model up to date for each AOI, and raises
alerts when a pixel breaks. S-CCD is the one validated detector that is
architecturally near-real-time, not the most accurate: cross-validated loss F1
is only 0.185 against project labels. Alerts are review triggers, not verdicts.
See [docs/architecture.md](docs/architecture.md) for the ingestion-to-Parquet
diagram and [docs/operational.md](docs/operational.md) for the pipeline, commit
boundaries and known gaps.

## Install
Requires Python >= 3.12 and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
# On a network share without symlink support, keep the venv on local disk:
export UV_PROJECT_ENVIRONMENT=/path/to/local/venv
uv sync --dev
uv run forestmon doctor      # checks dependencies, credentials, AOIs
```

Console scripts `forestmon` and `effektovervaking-forests` are the same app.
Extras: `--extra gee` (earthengine-api, only for condition step 02e).
`publish site` also needs the
`ogr2ogr` CLI (GDAL >= 3.8, PMTiles driver) on `PATH` or via `FORESTMON_GDAL_BIN`.
The JP2 driver ships with the rasterio wheel.

## Configure
Everything comes from environment variables or a `.env` in the working
directory (real env vars win); see `.env.example`.

- `FORESTMON_DATA_DIR` data root (default `./data`, outputs in `<data>/operational`)
- `FORESTMON_AOI_DIR` one GeoParquet per AOI, name = file stem (default `<data>/aoi`)
- `FORESTMON_LOG_DIR`, `FORESTMON_WEB_OUT`, `FORESTMON_GIS_OUT` (defaults under `<data>`)
- `FORESTMON_EE_PROJECT` Earth Engine project (optional), `FORESTMON_GDAL_BIN`

**CDSE S3 keys** go in `~/.config/cdse/credentials` (mode 600), not in a `.env`
on the project drive:

```
CDSE_S3_ACCESS_KEY=...
CDSE_S3_SECRET_KEY=...
```

Generate them at <https://eodata-s3keysmanager.dataspace.copernicus.eu/>.

To run against an existing deployment's data in place, point the variables
above at it (`FORESTMON_DATA_DIR`, `FORESTMON_AOI_DIR`, `FORESTMON_LOG_DIR`,
`FORESTMON_WEB_OUT`, ...) instead of copying it; set all of them, or `health`
starts a fresh log history and `publish` writes somewhere new. Only one checkout
should have the cron job installed.

## Use
Each step is per-AOI and idempotent (AOI name = parquet file stem).

```bash
uv run forestmon grid build <aoi>          # fixed 10 m pixel grid on the S2 UTM lattice
uv run forestmon catalog sync <aoi>        # index CDSE scenes
uv run forestmon grid assign-tiles <aoi>   # pin pixels to MGRS tiles (after sync)
uv run forestmon obs backfill <aoi>        # history -> store; batched, resumable
uv run forestmon obs compact <aoi>
uv run forestmon sccd bootstrap <aoi>      # fit initial per-pixel state
uv run forestmon run <aoi>                 # the operational cycle (safe to repeat)
uv run forestmon health <aoi>              # backlog, staleness, errors
uv run forestmon logs <aoi> --failures
uv run forestmon alerts list <aoi>
uv run forestmon publish site <aoi>        # PMTiles + COG for the web map / GIS
```

`forestmon --help` lists all commands.

## Condition pipeline (annual)
Condition score and trajectory class per 10 m cell and per stand for
olivinskog and kalklindeskog, from AlphaEarth 2017–2025 embeddings. Separate
from the loss monitor; it shares only the configuration. ~30 min, ~50 GB read.

```bash
uv run forestmon condition doctor            # stand gpkg, GDAL, ee, data root
uv run forestmon condition steps             # the 11 steps
uv run forestmon condition run               # fail-fast, logged; --from/--to/--dry-run
uv run forestmon condition step 04 -- --help # one step; args after -- are the step's own
```

Config: `CONDITION_ROOT` (data + outputs, default `<FORESTMON_DATA_DIR>/condition`),
`FORESTS_KARTLEGGING_GPKG` (stand polygons). `--extra gee` is needed for step
02e only.
Details and outputs:
[docs/condition/README.md](docs/condition/README.md); method and evidence:
[docs/condition/METHODS.md](docs/condition/METHODS.md).

## Deploy
`scripts/forestmon_cycle.sh` runs one cycle under `flock`; install it with
`scripts/forestmon_cycle.sh --install-cron` and configure it via environment
variables (see the script header). `forestmon health` exits 0 healthy, 2
unhealthy, and 1 for degraded only with `--strict`, so it can gate or page.
Docker, compose and systemd recipes, new-AOI onboarding and backups:
[docs/deployment.md](docs/deployment.md).

## Test
```bash
uv run pytest        # offline
```
`tests/condition` has network tests (hit the AlphaEarth mirror), deselected by
default; run them with `uv run pytest tests/condition -m network`.

Two drift guards compare against the inspector script and the label-tuning
results and run only when `FORESTMON_INSPECTOR_SRC` and
`FORESTMON_LABEL_TUNING_CSV` are set; otherwise the pinned values in
`tests/test_operational.py` are the contract.

Git hooks (ruff, formatting, secret scanning) run with
[prek](https://github.com/j178/prek): `uv tool install prek && prek install`.
