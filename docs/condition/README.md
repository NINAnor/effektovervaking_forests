# The condition pipeline (`forestmon condition`)

Everything needed to produce the condition and trajectory of **olivinskog** and
**kalklindeskog** stands from AlphaEarth embeddings, and nothing else. This
is the shipped pipeline, ported from `Geethen_forests/condition/deploy/` into
this repo as the subpackage `src/effektovervaking_forests/condition/` (library)
and `src/effektovervaking_forests/condition/steps/sNN_*.py` (the numbered steps,
now modules with `main(argv)`): no experiments, no alternative configurations
under test, no dead ends. It is what you run, and it is what the numbers in this
README were produced by.

The development tree it was cut from is `Geethen_forests/condition/`, and the
experiments behind every default are in its `research/` directory — not part of
this repo; the absolute path is
`/data/P-Prosjekter2/156004_08_effektovervaking_2026_2030_opsjon_2_fjernmaling/Geethen_forests/condition/research/`.
Read those if you want to know *why*; read this if you want to *run it* or
*rebuild it*.

Module names such as `condition.paths` or `condition.score` below are modules of
the subpackage `effektovervaking_forests.condition`.

| | |
|---|---|
| **Question** | Where has each stand arrived, and which way is it moving? |
| **Input** | 737 mapped stands (`skog_kartlegginger.gpkg`) + AlphaEarth 2017–2025 |
| **Output** | a condition score and a trajectory class per 10 m cell and per stand |
| **Cost** | ~30 min wall clock, ~50 GB read, no Earth Engine (one optional step) |
| **Scale** | 168,832 cells, 2.0 M pixel-year scores, 5 UTM zones |
| **Verified** | stand AUC 0.836 against the field labels (`local`, median stand score); trajectory recall on Hansen clearing 33 % at 2.0 % FPR at cell grain |
| **Parity** | see docs/condition/PORT_PARITY.md |

---

## 1. What the method is, in one page

Two steps that read one number per cell and year, the **AE score** (below).
Step 2 computes it; step 1 reads its yearly series. So step 2 runs first.

**Step 1 — trajectory** (RECOVER's recipe). Take each cell's yearly AE score at
the nature type (`global`) scope, and the stand's median cell score per year.
Z-score the nine-year series within forest type, fit a Theil–Sen slope, test it
with Mann–Kendall. Classify on the *magnitude* of the slope against a gate
calibrated to a 2% false-positive rate on units Hansen says were never cleared —
not on the p-value, which lacks power on nine points. `fluctuating` is a series
sd at or above the 81.4th percentile of those clean units (0.50 z stand, 0.55 z
pixel). Until October 2026 the input was a projection onto a fixed "naturalness
axis" (the direction from the transformed cloud to the good cloud, fitted once
per forest type). `forestmon condition step 03 -- --input axis` restores it. On
the Hansen check the axis finds clearing better (see §6), so the AE-score
trajectory is a screening flag.

**Step 2 — condition** (DegreeofRecovery's Degree of Recovery, the "AE score").
For each pixel, in each year:

```
        mean cosine distance to the k nearest TRANSFORMED reference pixels
dor = ─────────────────────────────────────────────────────────────────────
       mean cos-dist to k nearest GOOD  +  mean cos-dist to k nearest TRANSFORMED
```

with k = 25, distances taken in a shrunk-whitened metric, and the two reference
pools built as:

* **good** = pixels of `tilstand = god` stands *of the same forest type*, each
  contributing only its own **survey-year** pixels, never a stand within
  **3 km** of the probe, and never ground Hansen records as felled at or before
  that donor's survey year. Two scopes are computed for every pixel — `local`
  (the **32** nearest such stands within 50 km) and `global` (all of them);
* **transformed** = ESA WorldCover cropland (40) and built-up (50) from the
  **3–8 km annuli** of every stand of that forest type, **pooled per type** and
  taken in the same year as the probe — one end-member per type, not one per
  stand.

A stand's verdict is the **median of its cell scores** with a bootstrap interval
over *donor stands* (one draw shared by all cells of the stand-year, every cell
rescored under it), compared against a threshold fitted on labelled stands **outside the
stand's own 5 km spatial block**. An interval that straddles the threshold
returns `indistinguishable`, not a class.

The full argument for each choice, with the numbers, is in
[`METHODS.md`](METHODS.md).

---

## 2. Before you run

**Environment.** The repo's uv environment (Python ≥ 3.11; numpy, pandas, scipy,
pyarrow, duckdb, geopandas, shapely, rasterio, requests, tqdm come with the base
install — see [`pyproject.toml`](../../pyproject.toml)). On the NINA project
drive (CIFS, no symlinks) keep the venv on local disk:

```bash
export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/effektovervaking_forests
uv sync --extra fast          # + numba and threadpoolctl, for step 04 (below)
uv sync --extra fast --extra gee   # + earthengine-api, for steps 02e and 09
uv run forestmon condition doctor
```

`--extra fast` is optional (step 04 falls back to numpy), `--extra gee` is
needed only for 02e (Hansen) and 09 (GEE parity). This is the repo's venv, not
the `geo` pixi environment the pipeline was developed in.

GDAL must have **ZSTD** and **/vsicurl** — the mirror's COGs are ZSTD-compressed
and read over HTTPS. The `rasterio` wheel has both. `forestmon condition doctor`
checks the stand GeoPackage, the GDAL GTiff driver, numba / threadpoolctl / ee,
`aef_loader_plus` (optional), and shows the data root. `forestmon condition
steps` lists the steps.

**Inputs.**

| what | where | notes |
|---|---|---|
| mapped stands | `skog_kartlegginger.gpkg`, six layers | override the location with `FORESTS_KARTLEGGING_GPKG` |
| AlphaEarth 2017–2025 | [Source Cooperative `tge-labs/aef`](https://source.coop/tge-labs/aef) | anonymous HTTPS, no account |
| ESA WorldCover v200 | AWS `esa-worldcover` bucket | anonymous HTTPS, 2021 epoch |
| Hansen GFC (optional) | Earth Engine `UMD/hansen/global_forest_change_2025_v1_13` | the only step needing an EE account |

**Where it writes.** `data/` (caches, `data/cache/`) and `outputs/` (results:
`outputs/{data,tif,report,figures}`), under the **data root**: `CONDITION_ROOT`,
default `<FORESTMON_DATA_DIR>/condition` — no longer the code directory. The
directories are created when a step starts, never at import. All file paths in
this README that are not under `src/`, `tests/` or `docs/` are relative to the
data root.

**Disk.** ~50 GB of tile cache during the run; the products are 12 MB of
GeoTIFF and a 71 MB GeoPackage.

**Check it works before committing to a download:**

```bash
uv run pytest tests/condition                # offline, 154 tests, ~8 s
uv run pytest tests/condition -m network     # the 7 that hit the mirror
```

### Configure

Environment variables, or a `.env` in the working directory (real env vars win;
see `.env.example`):

| variable | default | |
|---|---|---|
| `CONDITION_ROOT` | `<FORESTMON_DATA_DIR>/condition` | data root: `data/cache/` and `outputs/{data,tif,report,figures}` |
| `FORESTS_KARTLEGGING_GPKG` | `.../Data/Olivinskog_kalklindeskog/skog_kartlegginger.gpkg` on the NINA project share | the stand polygons |
| `AEF_DEQUANT` | `signed_square` | `linear` only to reproduce results from before 2026-09-09 (stage B) |
| `AEF_LOADER_PATH` | the project-share `aef_loader_plus` checkout | optional faster download backend; see stage B |
| `FORESTMON_LOG_DIR` | `<FORESTMON_DATA_DIR>/logs` | `condition run` logs to `<FORESTMON_LOG_DIR>/condition/` |
| `FORESTMON_EE_PROJECT` | — | Earth Engine project; default for `condition run --ee-project` |

**Taking over the existing production result set in place.** To run against the
dev tree that holds `data/cache/` and `outputs/` from the 2026-10-23 production
run, without copying 50 GB, put this in `.env`:

```bash
CONDITION_ROOT=/data/P-Prosjekter2/156004_08_effektovervaking_2026_2030_opsjon_2_fjernmaling/Geethen_forests/condition
```

> **Warning.** The steps then **overwrite** the outputs of that run
> (`outputs/` — the GeoPackage, GeoTIFFs, parquet tables and JSON fits) and add
> to its tile cache. Keep a copy of `outputs/` first if you want to compare.

### Running

```bash
forestmon condition run                       # all steps, fail-fast
forestmon condition run --from 04 --to 08     # a slice
forestmon condition run --dry-run             # print the plan and exit
forestmon condition step 05                   # one step
forestmon condition step 04 -- --help         # that step's own arguments
```

`run` stops at the first failing step and tees each step to
`<FORESTMON_LOG_DIR>/condition/<stamp>_<id>_<name>.log`. `--ee-project`
(default `FORESTMON_EE_PROJECT`) enables 02e (Hansen); without it 02e is skipped
and the skip is announced, and 03 falls back to the p-value rule (stage C).
Step 09 (GEE parity) is never in `run`; invoke it explicitly. Arguments after
`--` go to the step's own argparse. `python -m
effektovervaking_forests.condition.steps <id> [args]` also works.

---

## 3. The stages

Five stages, thirteen steps (plus the optional 09). `forestmon condition run`
runs the thirteen in order, fails fast, and tees each step to
`<FORESTMON_LOG_DIR>/condition/`. The table is the map; the sections below say
what each step is actually doing. Invoke a step as `forestmon condition step
<id>`.

| stage | step | does | ≈ time | writes |
|---|---|---|---|---|
| **A** inputs | `00` `build_polygons` | polygons → `poly_id`, forest type, `tilstand`, role, survey year, spatial block | 10 s | `polygons.parquet`, `polygons_geo.parquet` |
| | `01` `plan_reads` | which AlphaEarth blocks the AOI needs, and what they cost | 20 s | `read_plan.parquet` |
| **B** embeddings | `02` `extract_embeddings` | the download: block-aligned reads of every stand pixel, 9 years | **6 min**, 31 GB | `polygon_pixels.parquet` |
| | `02b` `membership` | overlap-safe pixel model: one embedding per piece of ground | 30 s | `pixels`, `pixel_polygons`, `pixel_roles`, `pixel_sites`, `polygon_overlap` |
| | `02c` `plan_bad_pool` | set-cover: the 54 blocks the transformed pool needs, not the 329 it touches | 4 min | `bad_pool_plan`, `bad_candidates` |
| | `02d` `extract_bad_pool` | the transformed-pool download | **4 min**, 20 GB | `bad_pixels`, `bad_membership`, `bad_sites` |
| **C** reference | `02e` `hansen_reference` | Hansen loss year at every pixel centre — the one label not derived from AlphaEarth | 3 min | `hansen_pixels.parquet` |
| **D** science | `04` `condition` | **STEP 2** (runs first): whitened kNN DoR per pixel-year, both scopes, median stand score and interval, thresholds | **8 min** (2.7 min with the fast flags) | `pixel_condition`, `condition`, `threshold.json`, `whitener.json` |
| | `03` `trajectory` | **STEP 1** (runs second): yearly AE score, z-score, Theil–Sen, Mann–Kendall, calibrated gates | ~4 s | `trajectory`, `trajectory_pixels`, `trajectory_gate.json` (`naturalness_axes.json` only with `--input axis`) |
| **E** products | `05` `validate` | the five checks against the field labels — read this output, do not skip it | 40 s | `condition_combined.csv` + a printed report |
| | `06` `rasterize` | 20-band GeoTIFF per spatial block, on the untouched AlphaEarth grid | 1 min | `outputs/tif/*.tif`, `*.vrt`, `index.csv` |
| | `07` `package` | one GeoPackage for QGIS: 168,832 cells + 737 stands + a `readme` layer | 3 min | `outputs/condition_results.gpkg` |
| | `08` `report_data` | every number the report shows, read back out of the shipped outputs | 30 s | `outputs/report/report_data.json`, `condition_report.html` |

Times are measured for the two long steps (`02` at 6.2 min on 2026-09-03, `04`
at 8.4 min on 2026-09-09; 2.7 min wall with the fast flags on 2026-10-23, scoring
alone 2.4 min; `03` 3.7 s) and estimated for the rest. The two download steps are
network-bound and will vary.

`04` is 8 minutes again after the 2026-09-09 optimisation. The research changes
that shipped in it -- pooling the transformed pool per forest type (E38, ~1,050
localities in the bootstrap instead of a per-stand annulus) and a 32-stand local
pool (E44) -- had quietly taken it to **48.7 min**, because the donor-clustered
interval materialised an `(n_boot, n_clusters x k)` array (2,000 x 26,150
float64, 365 MB) per stand-year and per scope just to read 25 numbers out of
each row. That was 94% of the step. `condition.score._boot_knn_mean` now sorts
the candidate distances once and walks a short prefix of them instead, which is
the same estimator on the same RNG draw: `pixel_condition.parquet`,
`threshold.json` and `whitener.json` come back byte-identical and every
classification in `condition.parquet` is unchanged, with the four bootstrap CI
columns agreeing to 3.3e-16 (float summation order). See
`tests/condition/test_score.py::test_boot_knn_mean_*`.

Optional: `forestmon condition step 09 -- --project <ee-project>` confirms the
mirror's int8 COGs agree with `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL` (cosine ≈
1.0; needs `--extra gee`). Worth running once on a new deployment, never again.

### Stage A — what a polygon is, and when its label was true

The six mapped layers overlap heavily: **2,237 ha of polygon on 1,694 ha of
ground.** Step 00 does not resolve that — the stands stay as mapped — but it
records the two attributes without which the labels cannot be used honestly:

* `survey_year`, because `tilstand` describes the day the surveyor stood there
  and these labels span 2015–2025. A `god` from 2017 says nothing about 2025;
* `block`, the 5 km single-linkage cluster of stand centroids. 66 of them.
  Blocks are used for **the threshold and every confidence interval** — not for
  building pools (see §5).

Roles: `god` → good, `dårlig`/`sværtRedusert` → bad, `moderat` → **holdout**
(scored, never a reference — its position between the anchors is the
out-of-sample check), no label → unlabelled.

### Stage B — the download, and why it is minutes rather than days

The mirror's COGs are **band-interleaved, 1024×1024 blocks, 8192², 3.25 GB per
tile**. A 32×32 window costs the same 64 block fetches as the whole block —
measured, >90 s versus 6–10 s. So the reader is **block-aligned**: work out
which blocks the polygons touch, read each once, cut the polygons out in memory.
783 block reads for nine years instead of thousands of per-polygon ones.
`GDAL_HTTP_MULTIRANGE=YES` is essential; Cloudflare sheds load above ~8
concurrent tiles, so 6 workers and whole-tile retries.

**Two readers, same bytes.** The block fetch goes through
[`aef_loader_plus`](/data/P-Prosjekter2/154001_nyvest/landcover_Geethen/aef_loader_plus)
(VirtualiZarr + virtual-tiff over obstore) when it imports, and falls back to
rasterio otherwise — `--backend auto` (default) / `gdal` / `loader` on
step `02` (`extract_embeddings`) and step `02d` (`extract_bad_pool`).

Two things it does that the rasterio path cannot. It caches each tile's parsed
COG header under `data/cache/aef_manifests/` — 2.3 s cold, 0.07 s warm, and
there are 270 (tile, year) shards here. And it fetches a tile's blocks
concurrently rather than one after the next, in batches of 8 so a 16-block tile
does not hold 16 × 67 MB of int8 at once.

The chunk profile is the whole game. This pipeline reads **whole blocks with
every band**, so the reader asks for `chunks="all-bands"`: one Dask task per
1024×1024 block carrying all 64 bands, which issues the block's 64 per-band byte
ranges together — the same coalescing `GDAL_HTTP_MULTIRANGE` buys the rasterio
path. Under the default `native` profile each block is 64 separate tasks (4,096
per tile) and the loader is *slower* than GDAL. Measured on one 16-block 32N
tile in a single session:

| | tile Dask chunks | 16 blocks |
|---|---:|---:|
| GDAL, serial | — | 32.6 s |
| loader, `chunks="native"` | 4,096 | 35.9 s |
| loader, `chunks="all-bands"` | 64 | **4.9 s** |

End to end on four real 2024 shards: 110.4 s through GDAL against 23.0 s, so
**4.8×** — though the GDAL half of that varies a lot run to run (58.2 s for the
same four shards on another attempt), because it is serial and the mirror is
not. The loader's own numbers are steady, and switching profile took those four
shards from 46.5 s to 23.0 s on its own.

Everything is verified byte-identical: block for block against GDAL, `native`
against `all-bands`, and all four shards against the shipped cache
(`tests/condition/test_aef_backend.py`).

Note that in the `geo` pixi env the pipeline was developed in, the
`aef_loader_plus.pth` pointed at an **older** checkout under `$HOME`;
`condition.aef_backend` puts the project-share copy in front of whatever is
installed. Override with `AEF_LOADER_PATH`. `forestmon condition doctor` reports
whether the loader imports.

The repo's uv venv does **not** carry the loader's own dependencies, so out of
the box `doctor` warns and the downloads take the GDAL path (same bytes,
slower). To use the loader, install them into the venv, e.g.
`uv pip install dask obstore odc-geo virtual-tiff virtualizarr xarray "zarr>=3"`
(the third-party imports of `aef_loader/`; its README has the details). `loader_available()` checks
for `dask` as well, so a half-installed loader falls back to GDAL instead of
failing mid-download.

> **The dequantisation was wrong, and it is fixed as of 2026-09-09.** The
> mirror stores the embedding with a signed square root, so the float is
> `(raw/127.5)²·sign(raw)`, not `raw/127`. Step `09` (GEE parity) — run for the
> first time on 2026-09-09 — put the old linear read at cosine **0.9617** against
> `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL` and the signed square at **1.000000**;
> AlphaEarth vectors are unit-norm and the linear read gave ‖v‖ = 2.53. Every
> score here is cosine-based, so this moved every number: 30% of stands changed
> their local condition class. `condition.paths.AEF_DEQUANT` now defaults to
> `signed_square` and the whole pipeline was re-extracted and re-scored on it.
> Set `AEF_DEQUANT=linear` only to reproduce the superseded results. See
> `condition.aef.dequantise` and [`METHODS.md`](METHODS.md) §0.

`02b` exists because the first extractor rasterised all polygons into **one**
integer label raster, so overlapping stands took each other's pixels — last
drawn wins. 152 of 737 stands kept under half of their pixels. The repair needs
no re-download: the collision lost *memberships*, not *pixels*, so a
point-in-polygon join on the deduplicated centres recovers it in seconds. After
02b a pixel belongs to a **set** of stands (30.6% belong to more than one), and
every exclusion downstream is by `pixel_id`, never by `poly_id`.

`02c` is a set-cover problem, and solving it is the difference between ~118 GB
and 2.2 GB per year: choose the fewest AlphaEarth blocks such that every stand
has ≥150 transformed pixels inside its own annulus. Greedy by marginal deficit;
stands still short get their outer radius widened 8 → 16 → 32 → 64 km, recorded
per stand rather than hidden (an outer-coast olivinskog can have no cropland
within 8 km at all).

### Stage C — the only step that needs Earth Engine

Hansen's annual loss year is the one label here produced by a different sensor,
method and institution. It calibrates step 1's slope gate and sd cut and supplies the
recall figure in step 5. Skip it and step `03` (trajectory) falls back to the
p-value rule and says so — everything still runs, but the trajectory classes
then have no stated false-positive rate. (`forestmon condition run` skips `02e`
and announces it unless an Earth Engine project is given with `--ee-project` or
`FORESTMON_EE_PROJECT`.)

It is a *coarse* truth and is used as one: 30 m against 10 m, canopy removal
only, blind to the selective removal and understorey change these habitats are
mostly lost to. **Absence of Hansen loss is not evidence of no degradation.**

### Stage D — the two scientific steps

Both are described in §1 and in full in [`METHODS.md`](METHODS.md). Run step `04`
(condition) first: step `03` (trajectory) reads the yearly scores it writes and stops with an
error if they are missing. What matters operationally: step `04` is the long one (~4 min, 2.0 M pixel-year
scores) and it is a dense `1 - X̂P̂ᵀ` matmul plus a top-k, so it wants
RAM rather than cores -- unless it is run across stands in parallel:

```bash
uv sync --extra fast     # numba + threadpoolctl; optional, numpy fallback
forestmon condition step 04 -- --boot-rng per-stand --batch-small 200 \
    --workers 8 --parallel process
```

With the flags above, scoring takes 2.4 min on this AOI and the whole step 2.7
min wall (8 processes). Numba sums the top-k in float64, so pixel scores differ
from the plain-numpy path by at most ~2e-7. `forestmon condition step 04 --
--help` lists the step's arguments.

The flags seed each stand's bootstrap from (seed, stand), so stands can run in
worker processes that memory-map the embeddings (`outputs/data/_04_mmap/`,
removed afterwards), and they score stands of <= 200 pixels in batches. Cell
scores and thresholds do not depend on them. Interval bounds move by bootstrap
noise, and a few stand-years move between a decided class and
"indistinguishable". `--boot-draw thinned` is optional and applies only to the
mean-embedding probe interval, not to the median stand interval. See
[`METHODS.md`](METHODS.md), "The interval resamples donor stands".

Step `03` then takes about 4 s, because it builds no reference pools.

### Stage E — validate before you believe anything

Step `05` (`forestmon condition step 05`) prints five checks. They are not
decoration; the first run of this pipeline failed the fourth.

1. **Ordering** — median DoR must fall god > moderat > dårlig > sværtRedusert,
   with the held-out `moderat` between the anchors.
2. **Separation** — AUC and Youden J, at stand *and* pixel grain, per forest
   type, with a bootstrap over whole spatial blocks.
3. **Where the score comes from** — `m_good` and `m_bad` separately. The ratio
   can rise because a pixel is close to good *or* because it is far from
   converted land, and those are different claims.
4. **Controls** — the same AUC within log-area strata and within localities,
   and on pixels ≥20 m inside their own stand. Stand area alone separates the
   labels at 0.652; a signal that only exists between stands of different sizes
   is not a condition signal.
5. **Coherence** — declining stands should score lower than improving ones.
   The trajectory now reads the same AE score, so agreement is partly by
   construction; the useful output is the disagreement.

---

## 4. What comes out

Paths are relative to the data root (`CONDITION_ROOT`, see §2).

| file | one row per | key columns |
|---|---|---|
| `outputs/condition_results.gpkg` | 10 m cell (168,832), and stand (737) | **the deliverable** — 20 measurements per cell, the stand verdict, a self-describing `readme` layer, QGIS styles, EPSG:25833 |
| `outputs/tif/*.tif` | raster cell | 66 files, one per spatial block, 20 float32 bands on the untouched AlphaEarth grid + one `.vrt` per UTM zone |
| `pixel_condition.parquet` | pixel × year | `dor_local`, `dor_global`, `m_good_*`, `m_bad`, `label_age` |
| `condition.parquet` | polygon × year | `dor_*` (median of the stand's cells; `dor_*_probe` is the old mean-embedding score), `ci_*`, `condition_*`, `dor_*_median`, `dor_*_frac_poor` |
| `trajectory.parquet` (+`_pixels`) | polygon / pixel | `trajectory`, `sen_slope`, `mk_z`, `mk_p`, `c_2017..c_2025` (yearly AE score at the nature type scope) |
| `condition_combined.csv` | polygon | both steps joined at the latest year |
| `threshold.json` / `whitener.json` / `naturalness_axes.json` / `trajectory_gate.json` | — | every fitted quantity, so a score can be recomputed or audited |
| `outputs/report/condition_report.html` | — | the report, rendered from `report_data.json` — no number in it is hand-copied |

**Three rules for reading the output.** Each one is a way the map has already
been misread:

1. **Compare `dor_global` against `threshold_global`, per stand** — not against
   a single AOI-wide cutoff. The threshold is fitted without the stand's own
   block, so it varies by block on purpose.
2. **Read `m_good_*` beside `dor_*`.** At the `local` scope the reference side
   now carries real signal (`m_good_local` alone: AUC 0.683) and the ratio beats
   both components. At `global` it is closer (`m_bad` 0.703 vs `dor_global`
   0.721), so a `global` map is still partly a map of distance from converted
   land. Step `05` warns when this holds.
3. **Check `edge_m`.** Below 20 m a 10 m cell is a spectral mixture of the stand
   and whatever is next to it — and 428 of 737 stands have no pixel above that
   line, because they are small.

Unscoreable stands come out `NaN` with the reason recoverable from
`n_good`/`n_bad`/`n_valid`. Nothing is silently dropped or estimated.

---

## 5. The configuration, and why each number is what it is

These are the defaults in the code. Every one was measured; the experiment id
points into the research tree in `Geethen_forests/condition/research/`
(`/data/P-Prosjekter2/156004_08_effektovervaking_2026_2030_opsjon_2_fjernmaling/Geethen_forests/condition/research/`),
which was not ported into this repo.

| knob | value | why |
|---|---|---|
| `--k` (neighbours) | 25 | a **variance** choice, not an accuracy one (E05). A 5-nearest mean out of a 115-donor pool is usually five pixels of *one* donor. k=25 cuts the interval by a third and costs ~0.027 pixel AUC |
| `--shrink` (whitening λ) | 0.2 | a few AlphaEarth directions carry the terrain and phenology variance over this AOI and swamp the condition contrast. Pooled pixel AUC 0.649 → 0.713. Fitted on **unlabelled** pixels only |
| `--exclude-km` (good pool) | 3 km | contamination control. It is the transformed annulus's own inner edge, so one rule serves both pools. Replaced a whole-block exclusion, which also destroyed the regional matching `local` exists for: +0.055 stand AUC at `global`, +0.072 pixel AUC at `local` (E20) |
| annulus (transformed pool) | 3–8 km | as in the DegreeofRecovery paper: outside the stand's own edge, inside its region |
| transformed pool geometry | **pooled per forest type** (cap 2,000/type/year) | a per-stand ring makes `m_bad` a measure of distance from *that stand's own* farmland, so two stands on one hillside are judged against different yardsticks. Pooling: degraded recall 0.533 → 0.581 and degraded-called-good 0.105 → 0.038 at the deployed scope (E38), corroborated on independent Hansen-cleared labels (E26). It moves ~20% of stands between classes (E27). `--annulus-bad` restores the per-stand ring |
| cleared donor pixels | dropped | 1,072 pixels across 35 donors are ground Hansen says was felled at or before the donor's own survey year. Costs nothing measurable (E25) — kept because a clearcut in the good pool flatters exactly the probes that look like clearings, and because it is the state E26 was run in. `--keep-cleared-donors` restores them |
| transformed classes | WorldCover 40, 50 | grassland (30) and bare (60) deliberately excluded — in Norway they absorb natural fjell, pasture and scree |
| good pool source | `tilstand = god`, same forest type | WorldCover cannot tell an intact kalklindeskog from a spruce plantation on one — both are class 10 |
| scopes | `local` (**32** nearest ≤50 km) **and** `global` | they answer different questions and disagree exactly where a stand is unusual for its region but not for its type. Both are always reported |
| `--n-donors` (local pool) | 32 | was 8. The case for 8 was that its degraded recall was the steadiest of the candidates — on **three** seeds. At ten, 8 gives 0.540 ±0.052 and 32 gives 0.563 ±0.065: the same. What separates them is within-locality AUC, 0.685 ±0.008 against **0.758 ±0.005**, ~9 seed sd, and telling condition apart *within* a locality is the binding limitation of this map. Costs 0.6 more wrongly reassured degraded stands out of 35, inside the spread (E44) |
| pool caps | 60 px/donor, 2,000 global, 300 bad | tractability; `bootstrap_ci` resamples a pool at full length |
| `--gate-pctl` (trajectory) | 2.0 | gate the slope **magnitude** at a calibrated false-positive rate. With the axis input, recall on Hansen-cleared pixels went 26% → 52% at the same 2% FPR (T04); on the shipped AE-score input it is 33% (gates stand −0.100 / +0.159, pixel −0.083 / +0.184) |
| `--input` (trajectory) | `ae_global` | one score everywhere. `--input axis` restores the naturalness-axis projection, which finds Hansen clearing better (cell recall 0.52, stand AUC 0.75 against 0.64) |
| stable / fluctuating cut | sd ≥ 0.50 z stand, 0.55 z pixel | the 81.4th percentile of Hansen-clean units. The AE score jitters about twice as much as the axis, so the old fixed 0.30 z called 73 % of stands fluctuating |
| stand score | **median of the stand's cell scores** | checkable against the cell map; costs nothing in separation (stand AUC 0.836 against 0.829 for the mean-embedding score, difference −0.008, 95 % CI −0.027 to 0.012) |
| stand interval | bootstrap over **donor stands** | the row-wise version was about a third too narrow, so roughly one stand in eight carried a class its references did not support (E09) |
| threshold | Youden J, leave-block-out | fitted on labelled stands outside the scored stand's own 5 km block. Its **own** error bar is reported (`threshold_se_*`, `threshold.json`) but never used to withdraw a verdict: E43 built the joint test of `score − threshold` and found that counting the boundary honestly costs ~40% of the degraded stands found without reducing wrong reassurance, because 36 degraded stands in six localities do not pin a boundary down |
| spatial blocks | 5 km single-linkage, 66 | used for the threshold and every interval — the job no radius can do |

Escape hatches, all off by default: `--legacy` (the published pre-research
configuration), `--exclude-block` (the pool rule shipped between the research
work and E20), `--no-gate` (the p-value trajectory rule), `--no-whiten`,
`--annulus-bad` (the per-stand transformed ring, pre-E38), `--keep-cleared-donors`
(pre-E25 good pool), `--bad-pools <name>` (candidate transformed pools; these
need `02f` from the development tree). Use them to reproduce an old number, not
to produce a new one.

**Settled negatives — do not spend time re-deriving these.** The 36
field-labelled `dårlig` stands cannot anchor the bad end-member (substituting
them costs almost all the separation; mixing is worse than WorldCover alone).
Supervised LDA/logreg on the field labels collapses on olivinskog under block CV
(12 bad stands). The Hamed–Rao autocorrelation correction is anti-conservative
on 9 points. Summarising pixel scores beats nothing over the mean-embedding
probe. Tuning k or the pool caps buys nothing.

---

## 6. What "working" looks like

Production run of **2026-10-23** (median stand score, AE-score trajectory), all defaults. A reimplementation should land
near these; a large departure means something in §7 went wrong.

**Scale.** 737 stands (589 kalklindeskog, 148 olivinskog) · 168,832 scored 10 m
cells, 51,719 of them claimed by more than one stand · 2,005,866 pixel-year
scores · 66 spatial blocks in 5 UTM zones · labels: 263 god (250 of them surveyed in 2017 or later, so
usable both as donors and as scoreable-in-their-own-year evaluation stands),
141 moderat (135 scoreable), 20 dårlig, 16 sværtRedusert.

**Ordering (check 1), monotone at both scopes:**

| scope | god | moderat *(held out)* | dårlig | sværtRedusert |
|---|---|---|---|---|
| local | 0.556 | 0.525 | 0.482 | 0.431 |
| global | 0.562 | 0.543 | 0.511 | 0.463 |

**Separation (check 2), stand grain, block bootstrap:**

| scope | kalklindeskog | olivinskog | both | threshold | Youden J |
|---|---|---|---|---|---|
| local | 0.859 | 0.772 | **0.836** [0.73–0.90] | 0.5108 | 0.564 |
| global | 0.820 | 0.727 | 0.779 [0.68–0.89] | 0.5115 | 0.479 |

**Components (check 3).** `m_good_local` 0.699 · `m_good_global` 0.671 ·
`m_bad` 0.700 · `dor_local` **0.775** · `dor_global` 0.768 (pixel grain).

**Controls (check 4).** Within log-area strata the stand AUC is 0.795 (`local`) /
0.775 (`global`). Stand area alone: 0.652, which is the yardstick to beat, not
zero. The within-locality stand AUC (0.792 / 0.732) is from the 2026-09-03 run
and was not recomputed. Only a handful of blocks contain both a good and a poor
label, so the within-locality figure rests on few localities — it is the honest
number and it is also a fragile one.

**Trajectory.** 582 stable, 115 fluctuating, 25 declining, 15 improving (by type:
kalklindeskog 485 / 73 / 20 / 11, olivinskog 97 / 42 / 5 / 4). Against Hansen, at
cell grain: recall 0.326 at FPR 0.020, AUC 0.733 on 2,258 cleared / 72,090 clean
pixels (the axis input: 0.52 and 0.81). At stand grain the slope AUC is 0.64
against 0.75 for the axis (difference 0.11, 95 % CI 0.02–0.19). The olivinskog
fluctuating count (42) comes from a cut pooled over both forest types; it is not
evidence that olivinskog is unstable.

**Stand verdicts (2025).** `global`: 460 good, 152 indistinguishable, 125 poor.
`local`: 307 good, 199 indistinguishable, 187 poor, 44 `no_data` (no donor
inside the radius). Headline classes use `local` and give the 44 stands the
`global` class: kalklindeskog 307 / 157 / 125, olivinskog 12 / 61 / 75
(good / indistinguishable / poor). The management set — good condition but
declining — is 1 stand; 4 more are poor but improving.

> These use the **2026-09-09 signed-square** embedding. Everything published
> before that date was computed on the linear read and is not comparable — see
> the dequantisation note in stage B. The previous outputs are kept under
> `outputs/data/linear_dequant_2026-09-09/` (in the production data root).

**Products.** 66 GeoTIFFs (12.1 MB) + a 70.8 MB GeoPackage.

---

## 7. Reimplementing it from scratch

If you are rebuilding this in another language or another stack, the algorithm
is §1 and [`METHODS.md`](METHODS.md). What will actually cost you time is this list.
Every item is a bug that was found the expensive way, and each has a test in
[`tests/condition/`](../../tests/condition/) that fails if it comes back.

1. **The Source Cooperative tiffs are bottom-up.** `transform.e = +10` and
   `transform.f` is the *south* edge. Applying the usual north-up convention
   reads the block mirrored about the tile centre — a silent wrong answer, not
   an error. `tests/condition/test_aef_reader.py` pins this against `ds.index()`.
2. **Read block-aligned or wait days.** Band-interleaved 1024×1024 blocks: one
   pixel costs what a million cost.
3. **AlphaEarth is nodata outside a tile's natural UTM zone.** A 31N tile
   carries valid *coordinates* for an easting that belongs in zone 32 and
   nothing but nodata there. This cost 18 stands their entire bad pool before
   the zone filter went in (`tests/condition/test_worldcover.py`).
4. **Overlapping polygons must not be rasterised into one label raster.** One
   embedding per piece of ground, membership as a separate many-to-many table,
   every exclusion by `pixel_id`. Excluding by `poly_id` never worked: the
   probe's own ground comes back into the pool wearing a neighbour's id.
5. **Anchor each label to its survey year.** A donor contributes only its
   survey-year pixels; a stand surveyed before 2017 cannot be a donor at all
   (263 `god` → 250 usable). Carry `label_age` on every scored row.
6. **Mann–Kendall needs the tie correction.** `Var(S) = [n(n−1)(2n+5) −
   Σt(t−1)(2t+5)]/18`. And the stable-vs-fluctuating gate must use **sd, not
   CV** — CV explodes on a z-scored, zero-centred series.
7. **Gate the slope magnitude, not the p-value.** Mann–Kendall is correctly
   calibrated on nine points (year-shuffled FP rate 4.3% against a nominal 5%);
   it simply lacks power. This is worth a doubling of recall.
8. **Whitening helps condition and hurts trajectory** (axis input: 0.825 → 0.685). Sharper
   for where-you-are, noisier for which-way-you're-going. Do not apply it to
   both because it helped one.
9. **Leave-one-out is not enough, and leave-block-out is too much.** Without any
   spatial rule every `god` stand is its own nearest neighbour; with a whole
   locality excluded you also delete the regional matching. A 3 km radius is the
   thing that works — and blocks still do the threshold and the intervals.
10. **Bootstrap the donors, not the rows.** 97k pixels from 421 stands in 66
    localities are not 97k independent observations.
11. **Report within log-area strata.** Bad stands are smaller, and area alone
    separates the labels at 0.652.
12. **Keep the DoR maths vendored verbatim.**
    `src/effektovervaking_forests/condition/core.py` is copied unchanged from
    DegreeofRecovery `paper_release/`. If it needs a change, re-copy it — do not
    edit it here. A test pins its hash.

The pipeline as written is deterministic given a seed (`--seed 42` throughout);
one generator must be threaded through the donor cap and then the global draw,
or the global pool is a different subsample.

---

## 8. When the data changes

* **A new AlphaEarth year.** Add it to `AEF_YEARS` in
  `src/effektovervaking_forests/condition/paths.py`, re-run from `02` (the cache
  is per (tile, year), so only the new year downloads), then `02b` onwards
  (`forestmon condition run --from 02`).
* **A new field survey.** Re-run from `00`. Labels change roles, survey years
  and blocks; thresholds and axes are refitted. Nothing is carried over.
* **New or edited stand polygons.** Re-run from `00`. The bad-pool set-cover
  (`02c`/`02d`) depends on stand geometry, so it must be redone too.
* **A different AOI or forest type.** `LAYER_TYPE` and `ID_COLUMN` in
  `paths.py`, then everything. Note that the method needs a good pool of the
  same habitat type — that is what `tilstand` supplies here, and WorldCover
  cannot substitute for it.

---

## 9. Limitations to carry into any report

* **The bad pool is converted land, not degraded forest.** Cropland and built-up
  sit far from any forest, so part of what the score measures is distance from
  converted land. This is why `m_good` is reported separately.
* **Olivinskog has 65 donors to kalklindeskog's 185**, and a median 5.3 km from a
  stand to its 8th nearest donor against kalklindeskog's 2.1 km. It is the weaker of the two everywhere.
* **36 poor stands in 6 localities, 3 of which also hold a good one.** That is
  the binding constraint on every accuracy figure here, and no modelling choice
  relaxes it.
* **Field labels span 2015–2025** and describe whole stands. A `tilstand` is a
  weak label for any single pixel; a pixel-grain AUC below the stand-grain one
  is expected.
* **Hansen sees canopy removal only.** It validates the trajectory (cell-grain recall 0.33); it
  does not bound the degradation these habitats actually suffer.
* **`local` covers 44 fewer stands than `global`.** Report both, and say which
  one a figure came from.

---

## 10. Provenance

This pipeline was ported from `Geethen_forests/condition/deploy/` — itself a copy
of `condition/` at the state that produced the 2026-10-23 run (median stand
score, AE-score trajectory), minus the research harness and the candidate-bad-pool
extractor — into this repo as `src/effektovervaking_forests/condition/` (library)
and `src/effektovervaking_forests/condition/steps/` (the numbered steps, each a
module with `main(argv)`; the step id is the old script's number, so METHODS.md
and the research notes still point at the right code).

* `deploy/MANIFEST.json` recorded git commit **5b93f83** of the Geethen_forests
  repo and a SHA-256 per file.
* `core.py` is byte-identical to upstream (DegreeofRecovery `paper_release/`), and
  a test pins its hash.
* The deploy-only `sync_check.py` and `MANIFEST.json` were **not** ported; this
  repo's git history replaces them as the record of which code produced a result
  set.
* The data root moved: the deploy code kept `data/` and `outputs/` next to the
  code (overridable with `CONDITION_ROOT`); the package now takes them from
  `CONDITION_ROOT`, default `<FORESTMON_DATA_DIR>/condition`. The input
  GeoPackage's site default is carried explicitly in `paths.py`
  (`FORESTS_KARTLEGGING_GPKG` overrides it).
* [`METHODS.md`](METHODS.md) was copied unchanged.
* The port was verified by running steps 04–08 with the original deploy scripts
  and the ported package on identical copied inputs and comparing the outputs.
  Parity: see docs/condition/PORT_PARITY.md
