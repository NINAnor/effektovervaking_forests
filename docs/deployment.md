# Deploying the S-CCD monitor on a server

Once a day, for each area of interest (AOI), `scripts/forestmon_cycle.sh`:

1. pulls the monitor's data from the project S3 bucket;
2. runs `forestmon run <aoi>`: reads new Sentinel-2 scenes, advances the
   per-pixel S-CCD model and records new breaks as alerts;
3. runs `forestmon publish site <aoi>`: writes the map files (PMTiles, COG,
   GeoPackage);
4. re-bakes the dashboard (`app/data`), when `FORESTMON_DASHBOARD_SOURCE` and
   `FORESTMON_CONDITION_SOURCE` are set (see `app/README.md`);
5. pushes the data and the results back to the bucket;
6. with `--install-cron push`, commits `app/data` and pushes it to GitHub, and
   Pages redeploys the dashboard. A night with no new alerts commits nothing.

The bucket holds the data and the server keeps a working copy, so a server can
be replaced by installing on a new one. Each step does nothing when there is
nothing new, so running a cycle twice is harmless. Daily scenes come from the
[EOPF Zarr service](https://explorer.eopf.copernicus.eu) over public HTTPS; the
only credential a daily run needs is the bucket key.

## 1. Requirements

- Linux with `git`, [uv](https://docs.astral.sh/uv/getting-started/installation/)
  and [rclone](https://rclone.org/install/). uv installs Python 3.12 if the system
  has none. No GDAL install is needed.
- A key with read and write access to the bucket `miljodata-test` on
  `https://s3-int-1.nina.no`.
- Outbound HTTPS to `github.com`, `stac.dataspace.copernicus.eu`,
  `stac.core.eopf.eodc.eu` and the `eodc.eu` data hosts it links to.
- About 2 GB of **local** disk (not a network share: the locks and SQLite
  writes rely on it) for the two current AOIs, growing by roughly 0.2 GB a year.
- Memory: a daily run on kalklindeskog peaked at 0.6 GB; olivinskog is about
  three times larger.

## 2. Install

```bash
git clone https://github.com/NINAnor/effektovervaking_forests.git /opt/forestmon
cd /opt/forestmon
uv sync --frozen --no-dev
cp .env.s3.example .env.s3 && chmod 600 .env.s3   # fill in the five values
uv run forestmon doctor
```

`.env.s3` for the project bucket:

```ini
S3_ENDPOINT_URL=https://s3-int-1.nina.no
S3_BUCKET=miljodata-test
S3_PREFIX=156004_08_effektovervaking_2026_2030_opsjon_2_fjernmaling
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

`doctor` should show `OK` for the GDAL drivers and pyxccd, and `WARN` for the
CDSE credentials, which only a new AOI's history backfill needs (section 6).
Settings beyond the bucket go in `.env` (see `.env.example`); the defaults keep
everything under `/opt/forestmon/data`, and the AOIs default to
`kalklindeskog olivinskog` (`FORESTMON_AOIS`).

## 3. Run one cycle by hand

```bash
scripts/forestmon_cycle.sh
uv run forestmon health kalklindeskog
uv run forestmon health olivinskog
```

The cycle prints its steps and ends with `=== cycle end (rc=0) ===`. Both
`health` checks should say `HEALTHY`. The first cycle catches up on every scene
since the bucket was last updated (about 9 minutes for 8 days of scenes);
later cycles take a few minutes.

## 4. Schedule

```bash
scripts/forestmon_cycle.sh --install-cron
```

This adds one crontab line that runs the cycle at 04:17 daily, whether or not
anyone is logged in. `scripts/forestmon_cycle.sh --check` shows the end of the
log (`data/logs/cycle.log`). A second cycle that starts while one is running
exits at once, so overlapping runs are harmless.

## 5. Moving to another machine

Since 2026-11-19 the cycle runs nightly on a NINA workstation, from a clone with
its data in a local working copy, exactly as above; the bucket is kept current
by every cycle. The alerts hold reviewers' verdicts, which cannot be rebuilt,
and **two machines must never run the same AOI**: each would apply scenes the
other has not seen and the alerts would diverge. So, in this order:

1. On the old machine, after its last nightly cycle,
   `scripts/forestmon_cycle.sh --uninstall-cron`. If that cycle did not end with
   `S3 push ok` (`--check`), run `scripts/forestmon_cycle.sh --s3-push` there.
2. On the new machine, sections 2–4, with the old machine's `.env` minus its
   paths (`FORESTMON_DASHBOARD_SOURCE` and `FORESTMON_CONDITION_SOURCE` carry
   over unchanged). Its first cycle pulls what the old one pushed.
3. For the dashboard to update itself, `git push` must work there without a
   prompt (a deploy key with write access, or `gh auth setup-git`); then
   `scripts/forestmon_cycle.sh --install-cron push`.

## 6. Adding a new AOI

Put the polygons in the bucket's `sccd/aoi/<aoi>.parquet` (GeoParquet; the AOI
name is the file stem, a `_union` suffix is dropped). Building the 2017–2025
history reads CDSE, so put CDSE S3 keys
([generate them here](https://eodata-s3keysmanager.dataspace.copernicus.eu/)) in
`~/.config/cdse/credentials` (mode 600):

```ini
CDSE_S3_ACCESS_KEY=...
CDSE_S3_SECRET_KEY=...
```

Then, on the server:

```bash
scripts/forestmon_cycle.sh --s3-pull
uv run forestmon grid build <aoi>
uv run forestmon catalog sync <aoi>
uv run forestmon grid build <aoi>          # AOIs spanning UTM zones: again, after sync
uv run forestmon grid assign-tiles <aoi>
uv run forestmon obs backfill <aoi>        # hours; resumable, safe to re-run
uv run forestmon obs compact <aoi>
uv run forestmon sccd bootstrap <aoi>
scripts/forestmon_cycle.sh --s3-push
```

and add the name to `FORESTMON_AOIS` in `.env`.

## 7. Updating

```bash
git pull
```

The cycle runs `uv run --frozen`, which brings the venv in line with `uv.lock`.

## 8. When something goes wrong

```bash
scripts/forestmon_cycle.sh --check       # end of the cycle log
uv run forestmon health <aoi>            # exit 0 healthy, 2 unhealthy; --json for monitoring
uv run forestmon logs <aoi> --failures   # what failed in the last runs
```

- A failed detect does not stop the publish of what is on disk, and the cycle
  exits non-zero.
- If the bucket cannot be read, the cycle stops before running anything, so it
  never pushes stale data over newer data.
- If a push is interrupted, the next cycle pushes again instead of pulling.
- `run` takes a per-AOI lease in `data/operational/locks/`. A lease left by a
  killed process on the same machine is taken over automatically; otherwise it
  expires after 24 h, or `forestmon run <aoi> --force-lock` clears it once you
  are sure nothing is running.

## Backup

The bucket is the data store. The irreplaceable part is
`sccd/operational/alerts/` (reviewers' verdicts); `obs/` (the observation
history) is the expensive part to rebuild. The key cannot read whether the
bucket is versioned, so ask NINA IT whether it is backed up.

## Alternatives (untested)

The `Dockerfile` and `docker-compose.yml` build an image that runs `forestmon`
on a mounted `/data`, but the image has no rclone, so it cannot run the S3 cycle,
and it has never been built. A systemd timer can replace cron, with
`ExecStart=/opt/forestmon/scripts/forestmon_cycle.sh` in a `Type=oneshot` system
service (not a `--user` unit, which stops at logout).

## What has been verified

On 2026-11-13, in a fresh clone with an empty home directory, against a copy of
the bucket's `sccd/` data: install, `doctor` and the tests; a cycle that caught
up 8 days of scenes for both AOIs (0 errors; the new alerts matched the
current machine's own nightly runs, 159/159 and 427/431), published and pushed;
a second cycle that found nothing to do; `health`; a cycle skipped because
another was running; a cycle aborted because the bucket was unreachable; and
`--install-cron`. On 2026-11-19, the takeover from the old research checkout: its data uploaded
with `--s3-push` (`rclone check`: 0 differences), pulled into a local working
copy, and a cycle with the dashboard bake run against the real bucket. Not
verified: Docker and systemd.
