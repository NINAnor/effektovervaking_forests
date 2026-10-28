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
boundaries, parity gate and known gaps.

## Install
Requires Python >= 3.11 and [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
# On the NINA project drive (CIFS, no symlinks) keep the venv on local disk:
export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/effektovervaking_forests
uv sync --dev
uv run forestmon doctor      # checks dependencies, credentials, AOIs
```

Console scripts `forestmon` and `effektovervaking-forests` are the same app.
Extras: `--extra gee` (earthengine-api, only for `parity check`) and
`--extra bench` (experimental read benchmarks). `publish site` also needs the
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

To take over the existing production deployment in place, without moving
state, logs or the website payload, put this in `.env` (`G` = the
`Geethen_forests` directory; write it out in full):

```bash
FORESTMON_DATA_DIR=G/data
FORESTMON_AOI_DIR=G/data/processed
FORESTMON_LOG_DIR=G/logs/forestmon
FORESTMON_WEB_OUT=G/web/public/data
FORESTMON_GDAL_BIN=~/.pixi/envs/geo/bin
FORESTMON_EE_PROJECT=ee-gsingh
```

Setting only the first two would start a fresh log history (so `health` would
report no previous run) and publish outside the website repository. Remove the
old `forestmon_cycle.sh` crontab entry before installing this one, so that the
two checkouts do not both run.

## Use
Each step is per-AOI and idempotent (AOI name = parquet file stem).

```bash
uv run forestmon grid build <aoi>          # fixed 10 m pixel grid on the S2 UTM lattice
uv run forestmon catalog sync <aoi>        # index CDSE scenes
uv run forestmon grid assign-tiles <aoi>   # pin pixels to MGRS tiles (after sync)
uv run forestmon parity check <aoi>        # CDSE == Earth Engine? (gate, needs gee extra)
uv run forestmon obs backfill <aoi>        # history -> store; batched, resumable
uv run forestmon obs compact <aoi>
uv run forestmon sccd bootstrap <aoi>      # refuses until parity has passed
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
uv run forestmon condition doctor            # stand gpkg, GDAL, numba/ee, data root
uv run forestmon condition steps             # the 14 steps
uv run forestmon condition run               # all but 09; fail-fast, logged; --from/--to/--dry-run
uv run forestmon condition step 04 -- --help # one step; args after -- are the step's own
```

Config: `CONDITION_ROOT` (data + outputs, default `<FORESTMON_DATA_DIR>/condition`),
`FORESTS_KARTLEGGING_GPKG` (stand polygons). `uv sync --extra fast` (numba,
threadpoolctl) speeds up step 04; `--extra gee` is needed for 02e/09 only.
Details, outputs and the production-takeover recipe:
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

Two drift guards compare against the research repo and run only when
`FORESTMON_INSPECTOR_SRC` and `FORESTMON_LABEL_TUNING_CSV` are set; otherwise
the pinned values in `tests/test_operational.py` are the contract.

### Update from template
To update your project with the latest changes from the template, run:
```bash
uvx --with copier-template-extensions copier update --trust
```

You can keep your previous answers by using:
```bash
uvx --with copier-template-extensions copier update --trust --defaults
```

### (Optional) prek
prek is a fast, Rust-based tool for managing git hooks (100% compatible with pre-commit). It helps ensure code quality by running checks every time you make a commit.

First, install prek:
```bash
uv tool install prek
```

If you have installed the git hooks with `pre-commit` (template version 0.2.6 and older), remove them before installing the ones provided by prek:

```
pre-commit uninstall
```

Then install git hooks:
```bash
prek install
```

To run prek on all files:
```bash
prek run --all-files
```

### How to install a package
Run `uv add <package-name>` to install a package. For example:
```bash
uv add requests
```

#### Visual studio code
If you are using visual studio code install the recommended extensions


### Tools installed
- uv
- prek (optional)

#### What is an environment variable? and why should I use them?
Environment variables are variables that are not populated in your code but rather in the environment
that you are running your code. This is extremely useful mainly for two reasons:
- security, you can share your code without sharing your passwords/credentials
- portability, you can avoid using hard-coded values like file-system paths or folder names

you can place your environment variables in a file called `.env`, the `main.py` will read from it. Remember to:
- NEVER commit your `.env`
- Keep a `.env.example` file updated with the variables that the software expects
