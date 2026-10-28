# forestmon — near-real-time monitoring from CDSE

This package (`effektovervaking_forests`, CLI `forestmon`) is the deployable
counterpart to the interactive inspector in the research repo
(`Geethen_forests/scripts/c2c_ts_server.py`). Where that inspector
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
forestmon parity check kalklindeskog        # CDSE == Earth Engine? (gate)
forestmon obs backfill kalklindeskog        # history -> store; batched, resumable
forestmon obs compact kalklindeskog         # parts -> one sorted parquet per year
forestmon sccd bootstrap kalklindeskog      # fit initial per-pixel state
forestmon run kalklindeskog                 # the operational cycle
forestmon catalog reconcile kalklindeskog   # weekly: late / reprocessed products
forestmon alerts list kalklindeskog
```

Concurrency is one budget, not two knobs: band threads × scene workers is the
number of simultaneous connections to CDSE, and measured throughput peaks at ~14
and degrades past it. Defaults are 2 scene workers × 7 band threads
(`config.SCENE_WORKERS`, `config.MAX_CDSE_CONNECTIONS`).

Tests: `uv run pytest` (`tests/test_operational.py`) — offline failure-injection
over the commit boundaries (interruption, duplicate invocation, product
supersession, failed alert write), plus drift guards pinning the S-CCD
parameters to the inspector's and the loss gate to its label-tuned argmax. The
two guards that compare against the research repo run only when
`FORESTMON_INSPECTOR_SRC` (path to `Geethen_forests/scripts/c2c_ts_server.py`)
and `FORESTMON_LABEL_TUNING_CSV` (`Geethen_forests/results/label_tuning_results.csv`)
are set; otherwise the pinned values in `tests/test_operational.py` are the
contract.

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

**CDSE S3 credentials** (required). These are *not* the account password:
generate keys at <https://eodata-s3keysmanager.dataspace.copernicus.eu/> and put
them in `~/.config/cdse/credentials`:

```
CDSE_S3_ACCESS_KEY=...
CDSE_S3_SECRET_KEY=...
```

Not on the project drive. `/data/P-Prosjekter2/...` is a network mount that
forces mode 755 — `chmod 600` silently does nothing there, so a key file would
be readable by everyone in `nina_users`. `$HOME` is ext4 and honours 600.
Environment variables take precedence over the file, so a systemd unit or
container can inject credentials without touching disk at all.

**JP2 driver.** Sentinel-2 L2A ships as JPEG 2000. The PyPI `rasterio` wheel
bundles a GDAL with the JP2OpenJPEG driver, so nothing extra needs installing
(`forestmon doctor` checks this).

**`ogr2ogr`** (GDAL >= 3.8 CLI with the PMTiles driver) is a system tool needed
only by `publish site`. It is found on `PATH` or via `FORESTMON_GDAL_BIN`.

**Earth Engine** is needed only by `parity check`: install the `gee` extra
(`uv sync --extra gee`) and optionally set `FORESTMON_EE_PROJECT`.

**Locations.** Nothing is tied to the code location and nothing is created on
import. Everything lives under `FORESTMON_DATA_DIR` (default `./data`; outputs in
`<data>/operational`); AOIs are read from `FORESTMON_AOI_DIR` (default
`<data>/aoi`, one GeoParquet per AOI, AOI name = file stem, legacy
`<name>_union.parquet` accepted). See `.env.example` for the full list.

## The parity gate

`sccd bootstrap` refuses to run until `parity check` has passed. The reason is
specific: the labels, tuned gates and the inspector all come from Earth Engine's
`COPERNICUS/S2_SR_HARMONIZED`, while the operational stream comes from CDSE's
L2A archive. If the two disagree, a step change is injected at the handover
date — and a step change in every pixel on the same day is exactly what S-CCD
exists to report.

Parity is measured on **raw per-band DN** at a specific pixel on a specific
acquisition, joined on `(sensing time, tile)`. L2A DNs are integers, so a
correct pipeline matches *exactly*; the verdict is PASS only at ≥99.9% exact
agreement on every band. Three things decide whether it does, all handled
explicitly in `reader.py`: the baseline-04.00 `BOA_ADD_OFFSET`, nearest-cell
sampling of the 20 m SWIR bands, and never warping a scene onto our grid.

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

An AOI may span several UTM zones (olivinskog: 32, 33, 34, 35). Each polygon is
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
  SCL-only. Parity therefore compares unmasked reflectance and reports mask
  disagreement separately (`config.CLOUD_MASK_NOTE`).
* **Winter.** At 58–61°N the optical record effectively stops from November to
  February (low sun, snow — SCL 11 is rejected). S-CCD must not read the gap as
  change, and alerts raised on the first spring observation deserve their own
  confidence class. Not yet implemented.
* **The gate is under-calibrated.** `alerts.LOSS_GATE = 0.075` was tuned on 15
  loss positives. Re-tuning against a larger, *dated* NRT label set is the next
  calibration job — NRT accuracy is a function of time-to-detection, which the
  current annual labels cannot score at all.
* **No confirmation logic yet.** v1 alerts on a single confirmed break above the
  gate. Multi-observation confirmation and S1/S2 agreement are what will make
  the precision acceptable at this base rate.
