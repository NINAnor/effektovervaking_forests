# Deploying the S-CCD monitor

Three supported ways to run an AOI unattended. All end in the same command,
`forestmon run <aoi>`, which is idempotent and does nothing when there is
nothing new. Configuration is described in the README ("Configure") and
`.env.example`.

Nothing here has been run end to end on a machine other than the development one; the
Dockerfile and unit files are untested examples (see "Not verified" at the end).

## Credentials

CDSE S3 keys are read from the environment first, then `~/.config/cdse/credentials`
(`cdse_auth.py`). Never put them in an image, a unit file in git, or on a network share.

| Method | Injection |
|---|---|
| cron | `~/.config/cdse/credentials`, mode 600 on a local disk (network mounts often ignore chmod) |
| docker | `--env-file` / `-e CDSE_S3_ACCESS_KEY -e CDSE_S3_SECRET_KEY`, or compose `env_file` |
| systemd | `EnvironmentFile=/etc/forestmon/cdse.env`, mode 600, root-owned |

## Image

```bash
docker build -t forestmon .
docker run --rm forestmon doctor      # shows credentials missing until injected
```

Python 3.12-slim, multi-stage, dependencies installed from `uv.lock`
(`--frozen --no-dev`), non-root user (uid 10001). `ENTRYPOINT` is `forestmon`,
default `CMD` is `--help`. Defaults: `FORESTMON_DATA_DIR=/data`,
`FORESTMON_AOI_DIR=/data/aoi`, `FORESTMON_LOG_DIR=/data/logs`; mount a volume at
`/data` that uid 10001 can write.

**`publish site` is not available in the stock image**: it needs the `ogr2ogr`
CLI (GDAL >= 3.8 with the PMTiles driver), which is not installed. Build a
derived image that adds it, or set `FORESTMON_GDAL_BIN` to a directory containing
it. `run`, `health`, `alerts` and everything else work without it.

## (a) cron

The existing path: `scripts/forestmon_cycle.sh` runs detect, publish and push
under `flock`.

```bash
scripts/forestmon_cycle.sh --install-cron          # detect + publish
scripts/forestmon_cycle.sh --install-cron push     # also commit/push the web payload
```

Credentials: `~/.config/cdse/credentials`. AOIs, paths and `FORESTMON_PUSH` are
set in `.env` or on the crontab line (see the script header).

## (b) docker run / compose

```bash
# data/aoi/<aoi>.parquet is the AOI polygon; .env holds the CDSE keys (git-ignored)
docker run --rm --env-file .env -v "$PWD/data:/data" forestmon run kalklindeskog
```

[`docker-compose.yml`](../docker-compose.yml) is an example of the same thing:
one service, bind-mounted `/data`, `env_file`. Schedule it from the host:

```cron
17 5 * * *  cd /srv/forestmon && docker compose run --rm forestmon run kalklindeskog
```

## (c) systemd service + timer

`/etc/systemd/system/forestmon@.service` (instance name = AOI):

```ini
[Unit]
Description=forestmon run %i

[Service]
Type=oneshot
User=forestmon
EnvironmentFile=/etc/forestmon/cdse.env
Environment=FORESTMON_DATA_DIR=/srv/forestmon/data
# Native install; for docker use: ExecStart=/usr/bin/docker run --rm --env-file /etc/forestmon/cdse.env -v /srv/forestmon/data:/data forestmon run %i
ExecStart=/opt/forestmon/bin/forestmon run %i
```

`/etc/systemd/system/forestmon@.timer`:

```ini
[Unit]
Description=Daily forestmon run %i

[Timer]
OnCalendar=*-*-* 05:17:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now forestmon@kalklindeskog.timer
```

Unlike `systemd --user` units (which need lingering enabled), system units run
regardless of login.

## Onboarding a new AOI

Put the polygons in `<FORESTMON_AOI_DIR>/<aoi>.parquet` (name = file stem), then,
each step per AOI and resumable:

```bash
forestmon grid build <aoi>
forestmon catalog sync <aoi>
forestmon grid build <aoi>             # multi-zone AOIs only: re-run after sync, BEFORE assign-tiles
forestmon grid assign-tiles <aoi>      # after sync
forestmon obs backfill <aoi>           # long; batched, safe to re-run after an interruption
forestmon obs compact <aoi>
forestmon sccd bootstrap <aoi>
forestmon run <aoi>                    # now schedule this
```

With docker, prefix each line with
`docker run --rm --env-file .env -v "$PWD/data:/data" forestmon`.

## Monitoring

```bash
forestmon health <aoi>           # human summary
forestmon health <aoi> --json    # for a monitoring system
```

Exit code 0 healthy, 2 unhealthy; degraded is 1 only with `--strict`. It reads
the artefacts on disk, not the log, so it works after a run that died early.
Thresholds: `FORESTMON_STALE_STATE_DAYS`, `FORESTMON_STALE_RUN_HOURS`,
`FORESTMON_MAX_BACKLOG`, `FORESTMON_MAX_ERROR_RATE`. Gate on it from a second
timer with an `OnFailure=` unit, or a cron line that mails on
non-zero. `forestmon logs <aoi> --failures` shows what went wrong; the event log
is JSON Lines in `<FORESTMON_LOG_DIR>/<aoi>.jsonl`.

## Backup

Everything is under `<FORESTMON_DATA_DIR>/operational/`.

| Back up | Path | Notes |
|---|---|---|
| Observation store | `obs/` | The expensive part: months of CDSE reads |
| S-CCD state | `state/<aoi>_sccd_state.pkl` and `.json` | Back up as a pair; the json is the meta |
| Run ledger | `runs/<aoi>_ledger.parquet` | Which scenes are processed |
| Alerts | `alerts/<aoi>_alerts.parquet` | Holds human verdicts; not reproducible |
| AOI polygons | `<FORESTMON_AOI_DIR>/` | Inputs |

Derived, rebuildable: `grid/` (`grid build`), `catalog/` (`catalog sync`),
published output (`publish site`), logs, `locks/`. Take the backup while no `run`
holds the AOI lease, because the store, ledger and state are committed in
sequence (see `docs/operational.md`).

## Memory sizing

A multi-zone AOI such as olivinskog has about 117 M observation rows and a
173 MB state pickle. `sccd bootstrap` was OOM-killed at 40 GB when it loaded the
whole store at once; it now fits contiguous pixel chunks (`--chunk-pixels`,
default 12,000). `obs backfill` is batched by scene (`FORESTMON_INGEST_BATCH`,
default 100) and resumable. Do not set a container or unit memory limit tighter than your own
measurement of a backfill and a `run` on that AOI; for the small AOI
(kalklindeskog, 38 k pixels) the footprint is far smaller. Container limits that
kill the process mid-batch are safe to retry (the commit boundaries are built for
that) but waste the batch.

## One run per AOI (the lease)

`run` and `obs backfill` take a per-AOI lease at
`<data>/operational/locks/<aoi>.lock`. A second `run` reports the lease
holder and exits 0 rather than waiting, so overlapping timers are harmless and do
not fail the unit; a second `obs backfill` exits 1. A lease is taken over
automatically if its holder is dead on the same host or it is older than 24 h.
A holder on another host is assumed alive, and inside containers the hostname is
the container id, so after a hard-killed container the lease can persist up to
24 h; clear it with `--force-lock` once you are sure nothing is running. Never
run two containers/hosts on the same data volume for one AOI, and keep the
volume on a local (non-network) filesystem.

## Not verified

* `docker build` was not run (no access to the Docker daemon where this was
  written), nor was the image linted with hadolint. Treat the Dockerfile as
  untested until built once.
* The systemd units and compose file are examples, not exercised.
