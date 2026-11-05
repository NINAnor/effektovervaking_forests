# Deploying the S-CCD monitor on a server

Once a day, for each area of interest (AOI), the monitor:

1. finds new Sentinel-2 L2A scenes;
2. reads the AOI's pixels from them;
3. advances a per-pixel S-CCD change model;
4. records new breaks as alerts;
5. writes map files for the web viewer and GIS.

The single command `forestmon run <aoi>` does steps 1–4; it is idempotent and does
nothing when there is nothing new. `scripts/forestmon_cycle.sh` wraps it for all
AOIs and adds the publish step.

Since 2026, scenes come from the [EOPF Zarr service](https://explorer.eopf.copernicus.eu)
over public HTTPS, so **daily operation needs no credentials**. CDSE S3 keys are
needed only to build the 2017–2025 history for a *new* AOI (section 3B).

## 1. Server requirements

- **Linux** with `git`, and [uv](https://docs.astral.sh/uv/getting-started/installation/).
  uv installs Python 3.12 itself.
- **Outbound HTTPS** to:
  - `github.com` (install);
  - `stac.core.eopf.eodc.eu` and `data.eodc.eu` (daily scenes);
  - `stac.dataspace.copernicus.eu` (catalogue);
  - `eodata.dataspace.copernicus.eu`, only for a history backfill.
- **GDAL command-line tools ≥ 3.8** with the PMTiles driver, for the publish step.
  Check with `ogr2ogr --formats | grep PMTiles`. Ubuntu 24.04's `gdal-bin` (3.8)
  or a conda/pixi `gdal` both work. Detection runs without it; only publishing
  fails.
- **Local disk**, not a network share (the locks rely on it). About 2 GB for the
  two current AOIs, growing by roughly 0.2 GB a year.
- **Memory**: a daily run on kalklindeskog peaked at 0.6 GB. olivinskog is about
  three times larger and was not measured on its own; see "Memory".

## 2. Install

```bash
git clone https://github.com/NINAnor/effektovervaking_forests.git /opt/forestmon
cd /opt/forestmon
uv sync --frozen --no-dev          # creates /opt/forestmon/.venv from uv.lock
.venv/bin/forestmon doctor
```

What `doctor` should report:

- `OK` for the JP2OpenJPEG and Zarr GDAL drivers and for pyxccd;
- `WARN` for CDSE S3 credentials (expected: daily runs don't need them);
- `OK` for `ogr2ogr` once GDAL is installed.

To update later: `git pull && uv sync --frozen --no-dev`.

## 3. Data

Everything lives under one data directory, `FORESTMON_DATA_DIR`. Keep it outside
the clone, for example `/srv/forestmon/data`:

```text
<data>/aoi/<aoi>.parquet        AOI polygons, one GeoParquet per AOI
                                (the AOI name is the file stem; a "_union" suffix is dropped)
<data>/operational/obs/<aoi>/   observation store (the expensive part)
<data>/operational/state/       S-CCD model state
<data>/operational/runs/        ledger of processed scenes
<data>/operational/alerts/      alerts, including reviewers' verdicts
<data>/operational/catalog/     scene index (rebuildable)
<data>/operational/grid/        pixel grid (rebuildable)
<data>/logs/  <data>/web/  <data>/published/   logs and outputs
```

### 3A. Take over the existing AOIs (recommended)

kalklindeskog and olivinskog are already running elsewhere. Get from the current
operator:

- these folders of `operational/` (about 1.8 GB), copied to `<data>/operational/`:
  `obs/`, `state/`, `runs/`, `alerts/`, `grid/` and `catalog/`. Do **not** copy
  `locks/` (a copied lease would block the first run), nor `parity/`,
  `*_bench.json` or `*_gdal_*.csv`, which are left over from removed
  benchmarking tools;
- the AOI polygons `kalklindeskog_union.parquet` and `olivinskog_union.parquet`,
  copied to `<data>/aoi/`.

Copy `grid/` rather than rebuilding it: the state records a fingerprint of the
grid and refuses a different one.

Take the copy after the old machine's nightly cycle has finished. **Have the old
machine's cron job removed before the new server's first run.** Two machines
must never update the same AOI: each would apply scenes the other has not seen,
and the alerts (which hold review verdicts) would diverge. From the handover on,
the server's copy is the only one.

No credentials are needed. Check the copy:

```bash
.venv/bin/forestmon health kalklindeskog     # expect HEALTHY
.venv/bin/forestmon run kalklindeskog        # expect exit 0
```

This was tested on a copy of the production kalklindeskog data, in an
environment with no CDSE keys: `health` reported HEALTHY, `run` and the full
cycle exited 0.

### 3B. Start a new AOI

A new AOI needs its 2017–2025 history, which only CDSE holds, so set up CDSE S3
keys first (section 7). Put the polygons in `<data>/aoi/<aoi>.parquet`, then run
these steps. Each is resumable:

```bash
forestmon grid build <aoi>
forestmon catalog sync <aoi>
forestmon grid build <aoi>             # multi-zone AOIs only: re-run after sync, BEFORE assign-tiles
forestmon grid assign-tiles <aoi>      # after sync
forestmon obs backfill <aoi>           # hours; batched, safe to re-run after an interruption
forestmon obs compact <aoi>
forestmon sccd bootstrap <aoi>         # the memory-heavy step, see "Memory"
forestmon run <aoi>                    # from here on, the schedule takes over
```

Then add the AOI to `FORESTMON_AOIS`.

## 4. Configure

Copy `.env.example` to `.env` in the clone (it is git-ignored) and set:

```bash
FORESTMON_DATA_DIR=/srv/forestmon/data
FORESTMON_AOIS=kalklindeskog olivinskog
FORESTMON_BIN=/opt/forestmon/.venv/bin/forestmon
FORESTMON_PUSH=0
# FORESTMON_GDAL_BIN=/path/to/dir/containing/ogr2ogr   # only if ogr2ogr is not on PATH
```

- **`FORESTMON_BIN`**: cron runs with a minimal PATH. Without this setting the
  script looks for `uv` on PATH or in `~/.local/bin`, and stops with
  `FATAL uv not found` if it isn't there.
- **`FORESTMON_PUSH=0`**: the script does not commit or push the web files. With
  `1`, it commits `<data>/web` and pushes it to the git repository that directory
  belongs to, so the web map redeploys. This is off today; enable it only once
  that repository and the server's push access are set up.
- **Other settings**: the remaining `FORESTMON_*` settings (logs, health
  thresholds, `FORESTMON_S2_SOURCE`, `FORESTMON_EOPF_START`) are listed with
  their defaults in `.env.example`. The defaults are right for daily operation.

## 5. Schedule

Run one cycle by hand first, then install the cron job as the same user:

```bash
scripts/forestmon_cycle.sh                  # one cycle: detect + publish for every AOI
scripts/forestmon_cycle.sh --check          # last 40 lines of <data>/logs/cycle.log
scripts/forestmon_cycle.sh --install-cron   # daily at 04:17, with FORESTMON_PUSH=0
```

A cycle runs `forestmon run` and then `forestmon publish site` for each AOI. If
detection fails it still publishes what is on disk. It exits non-zero if any
step failed, so set `MAILTO=` in the crontab to receive failures by email.
Cycles cannot overlap: a `flock` guards the script, and a per-AOI lease guards
each run.

The cron route was chosen over `systemd --user` timers because user units stop
at logout unless lingering is enabled. A **system** systemd timer works as well;
see the example units in the appendix.

## 6. Check it is working

```bash
forestmon health <aoi>                 # HEALTHY / DEGRADED / UNHEALTHY with the reasons
forestmon health <aoi> --json          # for a monitoring system
forestmon logs <aoi> --failures        # what went wrong in recent runs
forestmon alerts summary <aoi>
```

`health` exits 0 when healthy and 2 when unhealthy; DEGRADED exits 1 only with
`--strict`. It reads the files on disk, so it also works after a run that died.

Signs of trouble:

- the state age grows past a few days (`FORESTMON_STALE_STATE_DAYS`, default 14);
- the backlog keeps growing;
- the error rate climbs.

EOPF publishes a day or two after CDSE does, so the newest scene is usually one
to three days old. That is normal.

## 7. CDSE credentials (history backfill only)

Only needed for 3B. These are *not* the account password:

1. Register at <https://dataspace.copernicus.eu/>.
2. Generate S3 keys at <https://eodata-s3keysmanager.dataspace.copernicus.eu/>.
3. Store them in `~/.config/cdse/credentials` (mode 600, on a local disk), or in
   the service's environment:

```bash
CDSE_S3_ACCESS_KEY=...
CDSE_S3_SECRET_KEY=...
```

Never put them in the repository, an image, or a network share.

## Backup

Back up `<data>/operational/` while no cycle is running (the store, ledger and
state are committed in sequence):

| What | Path | Notes |
|---|---|---|
| Observation store | `obs/` | Years of scene reads; slow to rebuild and needs CDSE keys |
| S-CCD state | `state/<aoi>_sccd_state.pkl` and `.json` | Back up as a pair |
| Run ledger | `runs/<aoi>_ledger.parquet` | Which scenes are processed |
| Alerts | `alerts/<aoi>_alerts.parquet` | Holds review verdicts; cannot be reproduced |
| AOI polygons | `<data>/aoi/` | Inputs |

`grid/`, `catalog/`, logs and the published outputs can be rebuilt.

## Memory

- **Daily `run`**: small. kalklindeskog (38 k pixels) peaked at 0.6 GB.
- **`sccd bootstrap`** (new AOIs only): the memory-heavy step. It fits the model
  in chunks of `--chunk-pixels` pixels (default 12,000), because loading the
  whole store for olivinskog was OOM-killed at 40 GB.
- **`obs backfill`**: batched (`FORESTMON_INGEST_BATCH`, default 100 scenes).
  Killing it mid-batch is safe, but that batch is redone.

## One run per AOI (the lease)

`run` and `obs backfill` take a per-AOI lease at
`<data>/operational/locks/<aoi>.lock`:

- A second `run` exits 0 without waiting; a second `obs backfill` exits 1.
- A lease is taken over automatically if its holder is dead on the same host, or
  if it is older than 24 h.
- `--force-lock` clears a stale lease, but only use it when you are sure nothing
  is running.

## Appendix: systemd (untested example)

`/etc/systemd/system/forestmon.service`:

```ini
[Unit]
Description=forestmon monitoring cycle

[Service]
Type=oneshot
User=forestmon
WorkingDirectory=/opt/forestmon
ExecStart=/opt/forestmon/scripts/forestmon_cycle.sh
```

`/etc/systemd/system/forestmon.timer`:

```ini
[Unit]
Description=Daily forestmon cycle

[Timer]
OnCalendar=*-*-* 04:17:00
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it with `sudo systemctl enable --now forestmon.timer`. The script reads
`/opt/forestmon/.env`, so no `Environment=` lines are needed.

## Appendix: Docker (untested)

The `Dockerfile` and `docker-compose.yml` build an image whose entrypoint is
`forestmon`, running as uid 10001 with `/data` as the data volume:

```bash
docker run --rm --env-file .env -v /srv/forestmon/data:/data forestmon run <aoi>
```

The image has no `ogr2ogr`, so `publish site` needs a derived image.

## What has and has not been verified

Verified on 2026-11-05:

- install from a fresh clone;
- `doctor`, `health`, `run` and the cycle script (detect and publish) on a copy
  of the production kalklindeskog data, with no CDSE keys;
- ingesting new scenes from EOPF without keys, on a small test AOI;
- EOPF and CDSE returning identical values for the same scenes.

Not verified:

- the Docker build and the systemd units;
- olivinskog's memory peak on a new server;
- `FORESTMON_PUSH=1` from a new server.
