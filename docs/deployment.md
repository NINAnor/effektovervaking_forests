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

The data lives in an **S3 bucket**; the server's disk is only a working copy.
Each cycle pulls the data from S3, runs, and pushes the data and the results back
(section 5). The server can therefore be replaced by installing on a new one and
pointing it at the same bucket.

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
- **[rclone](https://rclone.org/install/)** and access to the S3 bucket (an
  access key with read and write rights on it).
- **Local disk**, not a network share (the locks rely on it), for the working
  copy: about 2 GB for the two current AOIs, growing by roughly 0.2 GB a year.
  The bucket holds the same amount.
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

The bucket (the rclone path in `FORESTMON_S3_REMOTE`) holds:

```text
<remote>/aoi/<aoi>.parquet          AOI polygons, one GeoParquet per AOI
                                    (the AOI name is the file stem; a "_union" suffix is dropped)
<remote>/operational/obs/<aoi>/     observation store (the expensive part)
<remote>/operational/state/         S-CCD model state
<remote>/operational/runs/          ledger of processed scenes
<remote>/operational/alerts/        alerts, including reviewers' verdicts
<remote>/operational/grid/          pixel grid
<remote>/operational/catalog/       scene index
<remote>/web/  <remote>/published/  outputs, written by each cycle
<remote>/logs/                      logs, copied after each cycle
```

The server keeps a working copy with the same layout under `FORESTMON_DATA_DIR`
(for example `/srv/forestmon/data`), plus `operational/locks/`, which stays
local. All of `operational/` is needed, including `obs/`. Daily runs read the
observation history when ESA reprocesses an old scene, and it is the only way to
rebuild the model without CDSE keys and hours of downloads.

### 3A. Take over the existing AOIs (recommended)

kalklindeskog and olivinskog are already running on another machine. The
current operator uploads their data to the bucket once, from a clone of this
repository on that machine, with rclone configured for the bucket (section 4):

```bash
# a folder holding only the AOI polygons, under the names the AOIs should have
mkdir -p /tmp/forestmon_aoi
cp <old data>/processed/kalklindeskog_union.parquet <old data>/processed/olivinskog_union.parquet /tmp/forestmon_aoi/

FORESTMON_DATA_DIR=<old data> FORESTMON_AOI_DIR=/tmp/forestmon_aoi \
FORESTMON_S3_REMOTE=<remote> scripts/forestmon_cycle.sh --s3-push
```

`--s3-push` uploads only what the monitor needs: `aoi/` and the six folders of
`operational/` (about 1.8 GB). It leaves out `locks/`, `parity/`, `*_bench.json`
and `*_gdal_*.csv`; the last three are leftovers of removed benchmarking tools.
`grid/` must come along rather than be rebuilt: the state records a fingerprint
of the grid and refuses a different one.

Upload after the old machine's nightly cycle has finished, and **remove the old
machine's cron job before the server's first cycle.** Two machines must never
update the same AOI: each would apply scenes the other has not seen, and the
alerts (which hold review verdicts) would diverge. From the handover on, the
bucket is the only copy that matters.

On the server, after sections 4 and 5, run one cycle by hand and check it:

```bash
scripts/forestmon_cycle.sh                   # pulls from S3, runs, pushes back
.venv/bin/forestmon health kalklindeskog     # expect HEALTHY
```

This was tested with a local folder standing in for the bucket, on a copy of the
production kalklindeskog data and with no CDSE keys: the upload, a first cycle
on an empty server, a failed push and its recovery all behaved as described in
section 5. `health` reported HEALTHY on the same data.

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

Then add the AOI to `FORESTMON_AOIS`, and add its polygon to the bucket's
`aoi/`: a cycle pulls `aoi/` from the bucket, which replaces the local folder.

## 4. Configure

Copy `.env.example` to `.env` in the clone (it is git-ignored) and set:

```bash
FORESTMON_S3_REMOTE=nina:forestmon      # rclone remote and bucket (path); see below
FORESTMON_DATA_DIR=/srv/forestmon/data
FORESTMON_AOIS=kalklindeskog olivinskog
FORESTMON_BIN=/opt/forestmon/.venv/bin/forestmon
FORESTMON_PUSH=0
# FORESTMON_GDAL_BIN=/path/to/dir/containing/ogr2ogr   # only if ogr2ogr is not on PATH
```

- **`FORESTMON_S3_REMOTE`**: an rclone path, `<remote name>:<bucket>[/<prefix>]`.
  Create the remote once with `rclone config` as the user that runs the cron job
  (it is stored in `~/.config/rclone/rclone.conf`, keep it mode 600). For a
  generic S3 service the remote looks like this:

  ```ini
  [nina]
  type = s3
  provider = Other
  endpoint = https://<s3 endpoint>
  access_key_id = ...
  secret_access_key = ...
  ```

  Check it with `rclone lsf nina:forestmon/operational`. Unset, the script works
  on the local data directory only, as before.

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
scripts/forestmon_cycle.sh                  # one cycle: S3 pull, detect + publish per AOI, S3 push
scripts/forestmon_cycle.sh --check          # last 40 lines of <data>/logs/cycle.log
scripts/forestmon_cycle.sh --install-cron   # daily at 04:17, with FORESTMON_PUSH=0
scripts/forestmon_cycle.sh --s3-pull        # just the pull (or --s3-push), e.g. to inspect
```

A cycle:

1. pulls `aoi/` and `operational/` from the bucket into the working copy;
2. runs `forestmon run` and then `forestmon publish site` for each AOI (if
   detection fails it still publishes what is on disk);
3. pushes `operational/`, `aoi/`, `web/` and `published/` back to the bucket,
   and copies the logs to `logs/`.

It exits non-zero if any step failed, so set `MAILTO=` in the crontab to receive
failures by email. Cycles cannot overlap: a `flock` guards the script, and a
per-AOI lease guards each run.

What happens when S3 misbehaves:

- **Pull fails** (bucket unreachable, wrong credentials, or never uploaded): the
  cycle stops before running anything. Running on stale data and pushing it back
  could overwrite newer data in the bucket.
- **Push fails**: the cycle exits non-zero. The run's results stay on the local
  disk, marked by `<data>/.s3_push_pending`. While that file exists, the next
  cycle skips the pull (the local copy is newer than the bucket) and pushes
  again. Do not delete the marker by hand unless you mean to discard those
  results.
- **Push interrupted halfway**: the folders are pushed in the order the pipeline
  writes them (observations, ledger, state, alerts), so the bucket is left in a
  state the next run repairs, the same as after a crash.

**Only one server may use a bucket.** The per-AOI lease is local to the server,
so two servers on the same bucket would each overwrite the other's results.

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

The bucket is the primary copy, but each push mirrors the server, so a bad run
or a mistake on the server reaches the bucket at the next push. Turn on
**versioning** for the bucket (or copy it elsewhere regularly) so that an
earlier state can be restored. The parts that matter, under `operational/`:

| What | Path | Notes |
|---|---|---|
| Observation store | `obs/` | Years of scene reads; slow to rebuild and needs CDSE keys |
| S-CCD state | `state/<aoi>_sccd_state.pkl` and `.json` | Back up as a pair |
| Run ledger | `runs/<aoi>_ledger.parquet` | Which scenes are processed |
| Alerts | `alerts/<aoi>_alerts.parquet` | Holds review verdicts; cannot be reproduced |
| AOI polygons | `aoi/` | Inputs |

`grid/`, `catalog/`, logs and the published outputs can be rebuilt. A
replacement server needs nothing but sections 2, 4 and 5: its first cycle
pulls everything from the bucket.

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

The image has no `ogr2ogr` and no `rclone`, so `publish site` and the S3 sync
need a derived image, or run the cycle script on the host.

## What has and has not been verified

Verified on 2026-11-05:

- install from a fresh clone;
- `doctor`, `health`, `run` and the cycle script (detect and publish) on a copy
  of the production kalklindeskog data, with no CDSE keys;
- the S3 sync with a local folder standing in for the bucket: the first upload,
  a cycle on an empty server, a failed push and its recovery, and an unreachable
  or never-uploaded bucket;
- ingesting new scenes from EOPF without keys, on a small test AOI;
- EOPF and CDSE returning identical values for the same scenes.

Not verified:

- the S3 sync against a real S3 bucket (only against a local folder; the rclone
  commands are the same);
- the Docker build and the systemd units;
- olivinskog's memory peak on a new server;
- `FORESTMON_PUSH=1` from a new server.
