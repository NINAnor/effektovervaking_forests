# forestmon — near-real-time monitoring of Sentinel-2

This package (`effektovervaking_forests`, CLI `forestmon`) is the deployable
counterpart to an interactive inspector script from the research work. Where that inspector
answers *"what happened at this pixel?"* interactively off Earth Engine, this
package answers *"what changed since the last run?"* unattended, off the
Copernicus Data Space Ecosystem.

Stage one is **S-CCD only**. COLD, CCDC, LandTrendr, VeRDET and the RADD stream
come later behind the same catalogue, observation and state layers.

## Why S-CCD first

Of the detectors validated in this project, S-CCD is the only one that is
*architecturally* near-real-time: it carries a Kalman-filtered model of each
pixel forward and tests each new observation against it (`sccd_update`), rather
than refitting a whole history (`sccd_detect`). The rest need a completed annual
composite or a full batch refit.

That is its qualification, not its accuracy. Against this project's human labels
S-CCD scored *worst* of the six segmenters — cross-validated loss F1 **0.185**
for the deployed configuration (p_cg 0.90, conse 6, gate 0.075), vs 0.79 for
VeRDET. (0.074, quoted in earlier drafts, is the score of the grid-searched
config that tuning rejected as overfitted — not what runs here.) Treat its breaks as review triggers; the precision has to come
from the confirmation logic layered on top, which is where `alerts.py` will grow.

## Pipeline

```
  AOI polygons ──► aoi.py       38,327 fixed 10 m pixels (kalklindeskog)
                    │           on the real S2 UTM lattice — never resampled
                    ▼
 CDSE STAC ────► catalog.py    16,022 scenes indexed; 7 MGRS tiles;
                    │           acquisition schedule + publication lag
                    ▼
 s3://eodata ──► reader.py     block-aligned windowed JP2 reads
                    │           (~0.3% of a tile, so we read blocks not bands)
                    ▼
                 obs.py        observations as immutable parquet parts,
                    │           compacted per year; keyed (pix_id, acquisition)
                    ▼
                 sccd.py ◄──► state.py     per-pixel SccdOutput, persisted
                    │
                    ▼
                 alerts.py     magnitude-gated, append-only, human verdicts win
```

`runs.py` is the ledger of which scenes have been processed — deliberately
separate from the observation store, because a fully clouded scene *is*
processed but leaves no observations, and would otherwise be retried forever. It
records *how far* each scene got, not just that it was seen: a scene at
`observations_committed` is on disk but not yet in the model, and the next run
folds it in from disk rather than re-reading it from CDSE.

`store.py` holds the durability primitives — atomic replace, and a per-AOI
lease so a scheduler that fires twice gets one worker rather than two racing on
the same ledger.

### Commit boundaries

The observation store, the ledger and the state pickle cannot be updated
atomically together, so the write order is chosen so a crash always leaves work
to redo rather than work silently lost:

```
1. observations -> new part file                  (idempotent by key)
2. ledger       -> observations_committed         (won't be re-read)
3. state        -> pickle replaced atomically
4. ledger       -> state_applied                  (fully done)
5. alerts       -> written, meta.alerts_dirty cleared
```

The invariant that matters: an observation is never applied to the recursive
model twice. Everything else is recoverable, and `pipeline.apply_pending_observations`
is what recovers it.

## Usage

```bash
forestmon doctor                            # dependencies + credentials
forestmon grid build kalklindeskog
forestmon grid assign-tiles kalklindeskog   # after catalog sync
forestmon catalog sync kalklindeskog
forestmon catalog schedule kalklindeskog    # when is the next scene readable?
forestmon obs backfill kalklindeskog        # history -> store; batched, resumable
forestmon obs compact kalklindeskog         # parts -> one sorted parquet per year
forestmon sccd bootstrap kalklindeskog      # fit initial per-pixel state
forestmon run kalklindeskog                 # the operational cycle
forestmon catalog reconcile kalklindeskog   # weekly: late / reprocessed products
forestmon alerts list kalklindeskog
```

Concurrency is one budget, not two knobs: band threads × scene workers is the
number of simultaneous connections to CDSE; throughput peaks at about 14 and
degrades past it. Defaults are 2 scene workers × 7 band threads
(`config.SCENE_WORKERS`, `config.MAX_CDSE_CONNECTIONS`).

Tests: `uv run pytest` (`tests/test_operational.py`) — offline failure-injection
over the commit boundaries (interruption, duplicate invocation, product
supersession, failed alert write), plus drift guards pinning the S-CCD
parameters to the inspector's and the loss gate to its label-tuned argmax. The
two guards that compare against the research inspector run only when
`FORESTMON_INSPECTOR_SRC` (the inspector script) and `FORESTMON_LABEL_TUNING_CSV`
(the label-tuning results CSV) are set; otherwise the pinned values in
`tests/test_operational.py` are the contract.

## Watching it run

```bash
forestmon health kalklindeskog          # how far behind are we, is anything stuck
forestmon health kalklindeskog --json   # same, for a monitoring system
forestmon logs   kalklindeskog --failures
```

`health` is computed from the artefacts on disk — ledger, store, state meta,
catalogue — not from the log, because a run that dies before writing anything
still has to be diagnosable. It reports backlog size and age, catalogue age,
model age, read error rate and the top recent errors, scene latency, the
store-to-model gap, alerts awaiting review, and who holds the lease. Exit code
0 = healthy, 2 = unhealthy; degraded is 1 only with `--strict`, otherwise 0.
A timer can page on it.

The event log is JSON Lines under `<FORESTMON_LOG_DIR>/<aoi>.jsonl` (default `<data>/logs`), one line per
event with a `run_id` tying an invocation together — greppable at 3am and
loadable into pandas for "what was our p95 scene latency last month", with no
service to run.

Everything is per-AOI and idempotent. `run` is the one a timer calls; with
nothing new it does nothing, which is what makes a frequent schedule cheap.

## Setup

**Sources.** Scenes from `FORESTMON_EOPF_START` (default 2026-01-01) on are read
from the [EOPF Zarr service](https://explorer.eopf.copernicus.eu): public HTTPS,
no credentials, same product ids as CDSE, read with GDAL's Zarr driver. EOPF only
holds data from about April 2025, so the history before that comes from CDSE.
Both returned identical values for two scenes x 2000 pixels (2026-11-05).
`FORESTMON_S2_SOURCE=cdse` reads everything from CDSE.

**CDSE S3 credentials** (needed for the history backfill). These are *not* the account password:
generate keys at <https://eodata-s3keysmanager.dataspace.copernicus.eu/> and put
them in `~/.config/cdse/credentials`:

```
CDSE_S3_ACCESS_KEY=...
CDSE_S3_SECRET_KEY=...
```

Keep the file on a local disk, not a network mount: network mounts typically
force a fixed mode, so `chmod 600` silently does nothing and the key file would
be readable by other users. Environment variables take precedence over the file, so a systemd unit or
container can inject credentials without touching disk at all.

**JP2 driver.** Sentinel-2 L2A ships as JPEG 2000. The PyPI `rasterio` wheel
bundles a GDAL with the JP2OpenJPEG driver, so nothing extra needs installing
(`forestmon doctor` checks this).

**`ogr2ogr`** (GDAL >= 3.8 CLI with the PMTiles driver) is a system tool needed
only by `publish site`. It is found on `PATH` or via `FORESTMON_GDAL_BIN`.

**Earth Engine** is needed only by condition step 02e (Hansen loss year): install
the `gee` extra (`uv sync --extra gee`) and set `FORESTMON_EE_PROJECT`. The
S-CCD monitor itself does not use it.

**Locations.** Nothing is tied to the code location and nothing is created on
import. Everything lives under `FORESTMON_DATA_DIR` (default `./data`; outputs in
`<data>/operational`); AOIs are read from `FORESTMON_AOI_DIR` (default
`<data>/aoi`, one GeoParquet per AOI, AOI name = file stem, legacy
`<name>_union.parquet` accepted). See `.env.example` for the full list.

## Tile overlap

MGRS tiles overlap by ~9.8 km, so 788 of kalklindeskog's 38,327 pixels (2.1%)
are imaged by two tiles on the same acquisition. **The two granules do not
agree**: Sen2Cor runs its atmospheric correction per tile, so on the
32VNL/32VNM seam ~99% of band values differ, by up to 2041 DN -- while SCL
agrees exactly, confirming it is radiometry and not geolocation.

In NBR the difference largely cancels (max 0.0185; none above the 0.075 loss
gate), so it does not fire alerts on its own. It is fixed anyway because the
bias is *tile-dependent*, not random: a pixel fed mostly by one tile and later
mostly by the other sees a systematic step, which is exactly what S-CCD is built
to report.

`grid assign-tiles` (in `aoi.py`) pins every pixel to the tile in which it sits furthest from
the tile edge -- deterministic, and the most interior view is also the better
observation. `reader.pixels_for_scene` then filters on it, which doubles as the
exact spatial prefilter (17k/20k pixels offered per scene instead of all 38k).

## UTM zones

An AOI may span several UTM zones (e.g. olivinskog, below: 32, 33, 34, 35). Each polygon is
gridded in the zone Sentinel-2 tiles it in — MGRS, *with* the Norway exception
that widens 32V to 3–12°E, so the Bergen-area stands are zone 32, not 31 — and,
once the catalogue exists, in the zone of a tile that actually contains it
(north of 64°N at ~12.7°E only zone-32 tiles reach; re-run `grid build` after
`catalog sync`). The
grid carries one sub-grid per zone side by side; `pix_id` embeds the EPSG code.
A scene is only ever offered pixels in its own CRS, so nothing is warped, and
publishing writes one COG per zone.

## Known gaps

* **Cloud masking is looser than Earth Engine's.** The GEE recipe also requires
  s2cloudless probability < 40, and that collection is a Sentinel Hub product
  Google hosts — it is not in the CDSE SAFE archive. The operational mask is
  SCL-only. CDSE and Earth Engine L2A reflectance were verified identical once
  (2026-08, raw DN, exact match); the mask difference is noted in `config.py`.
* **The gate is under-calibrated.** `alerts.LOSS_GATE = 0.075` was tuned on 15
  loss positives. Re-tuning against a larger, *dated* NRT label set is the next
  calibration job — NRT accuracy is a function of time-to-detection, which the
  current annual labels cannot score at all.
* **No confirmation logic yet.** v1 alerts on a single confirmed break above the
  gate. Multi-observation confirmation and S1/S2 agreement are what will make
  the precision acceptable at this base rate.

## Example AOI: olivinskog (multi-zone)

`olivinskog` runs through the same CLI as `kalklindeskog`; the AOI name is the
GeoParquet file stem under `FORESTMON_AOI_DIR` (`olivinskog.parquet`; the legacy
`olivinskog_union.parquet` is also accepted). Differences from a single-zone AOI:

* **Size.** 104 polygons, 130,539 fixed 10 m pixels (126,610 in EPSG:32632,
  2,628 in 32634, 1,225 in 32633, 76 in 32635), 20 home MGRS tiles, 61,276
  catalogue scenes (2017-2026), about 117 M observation rows (1.2 GB of yearly
  parquet, against 360 MB for kalklindeskog) and a 173 MB state pickle.
* **One polygon, one CRS, no warping.** Each polygon is gridded whole in the zone
  Sentinel-2 tiles it in (`aoi.polygon_crs`), so a stand is never split across
  coordinate systems. Zone 32V is widened to 3-12 E (MGRS), so western Norway at
  about 5 E is zone 32, not 31. North of 64 N around 12.7 E only zone-32 tiles
  reach, so run `grid build` again after `catalog sync` (the second build assigns
  zones from the tiles that actually contain each polygon).
* **`pix_id` embeds the EPSG code** (`32632_<gx>_<gy>`), which keeps ids unique
  across zones.
* **Per-zone COGs.** `publish site` writes `alerts_olivinskog_<epsg>.tif`, one
  sparse COG per zone (only occupied 512x512 blocks are allocated; a dense array
  over zone 32 would need about 17 GB). Vector outputs are reprojected to WGS84.
* **Backfill.** `obs backfill` writes immutable part files per batch
  (`FORESTMON_INGEST_BATCH`, default 100 scenes) and is restartable;
  `obs compact` then merges them into one sorted, deduplicated file per year.
* **Winter.** At 58-69 N the optical record stops from November to February.
  `confidence.py` caps the confidence of breaks in that window, or after a gap
  of more than 60 days, at `SEASON_CAP` (0.30) and records the cause in
  `season_flag`.

Onboarding order:

```bash
forestmon grid build olivinskog
forestmon catalog sync olivinskog
forestmon grid build olivinskog             # again: zones from covering tiles
forestmon grid assign-tiles olivinskog
forestmon obs backfill olivinskog
forestmon obs compact olivinskog
forestmon sccd bootstrap olivinskog
forestmon run olivinskog
```
