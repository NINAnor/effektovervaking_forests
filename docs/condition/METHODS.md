# Condition of olivinskog and kalklindeskog from AlphaEarth embeddings

Two steps over the same 64-dimensional annual embedding, 2017–2025:

1. **Trajectory** — is the stand moving toward or away from good condition?
2. **Condition** — where has it arrived?

The trajectory is reported as step 1 but computed after the condition step, because
since 2026-10-23 it runs on the condition score (run order: 04 then 03).

Both are adaptations of published work in this group's own repositories:
the trajectory recipe from [RECOVER](https://github.com/Geethen/RECOVER), the
condition score from
[DegreeofRecovery](https://github.com/Geethen/DegreeofRecovery) (`paper_release/`).
This document records what was carried over unchanged, what had to change for
Norwegian forest condition, and why.

---

> ## Status, 9 September 2026 — the embedding was corrected and the ledger re-run
>
> Until 2026-09-09 this pipeline dequantised the Source Cooperative mirror
> **linearly** (`raw/127`). That is wrong: the mirror stores a signed square
> root, and the linear read sat at cosine **0.9617** to what Earth Engine serves
> — an error in *direction*, which a cosine-based score cannot absorb. See
> §0 *How the int8 becomes a float* for the parity table.
>
> `AEF_DEQUANT` now defaults to `signed_square`. The product was re-extracted and
> re-scored, and on the same day the **whole research ledger (E00–E48, T00–T05)
> was re-run** on the corrected embedding. Two experiments are incomplete: E17's
> symbolic-regression arms need `pysr`, which is not installed in this
> environment, and E36 selects its candidates from the ledger, which has grown
> since it was first run.
>
> **What that means for this document.** The validation figures in §3 and the
> product counts are from the corrected run. **The research tables in §1 and §2
> below are the pre-correction measurements** unless a line says otherwise — they
> are why each default was chosen, and the re-run confirmed most of them, but
> take current magnitudes from
> [`CONDITION_LEDGER.html`](CONDITION_LEDGER.html) and
> [`REFERENCE_ROUND.html`](REFERENCE_ROUND.html), or from the ledger rows dated
> 2026-09-09 16:55 onward.
>
> *(The 2026-10-23 note below changes the stand score and the trajectory input;
> the 9 September figures here are mean-embedding and axis results.)*
>
> **Four conclusions did not survive the re-run**, and each is flagged in the
> ledger page: E44's case for the deployed 32-stand local pool (32 donors now
> report about three more field-degraded stands out of 35 as good than eight
> donors do, outside seed noise); E27's finding that no stand crosses the
> good↔poor diagonal (five do); E43's verdict that an honest boundary rule buys
> no safety (it now halves wrong reassurance); and E41's reading of the
> scope-disagreement labels (it now favours `global`, not `local`).

---

> ## 2026-10-23: median stand score and AE-score trajectory
>
> Two things changed on this date. The client asked for one score everywhere,
> checkable against the cell map.
>
> **1. The stand score is the median of the stand's 10 m cell scores.** Until
> October 2026 it was the AE score of the stand's *mean embedding*, one probe
> vector per stand-year. Both scopes (`local`, nature type) now use the median
> of the cells. Column names are unchanged: `dor_local` and `dor_global` hold the
> median (also written as `dor_<scope>_median`); the old mean-embedding value is
> kept as `dor_<scope>_probe`. Every cell score, pixel percentile, `frac_poor`,
> the year effect on cells and the cell maps are unchanged.
>
> **2. The stand interval follows the median.** Same donor-stand and 500 m
> locality cluster bootstrap, 2,000 draws. One draw is now shared by all cells of
> the stand-year, every cell is rescored under it, and the draw's stand value is
> the median over cells. A stand with more than 2,001 cells uses 2,001 cells at
> evenly spaced ranks of its cell scores, shifted to the full-stand median; the
> point estimate always uses all cells, and the shortcut was checked to sit
> within 1e-4 of using all of them. Median interval width is 0.049 at `local`
> (was 0.059) and 0.042 at nature type (was 0.054). The count of
> `indistinguishable` stands barely moved (local, 2025: 200 to 199), so a
> narrower interval is not a more certain class.
>
> **3. The threshold was refitted on the new scores.** `local` 0.511 (was 0.502),
> Youden J 0.564 (was 0.557), sensitivity 0.735, specificity 0.829 on 238 `god`
> and 35 degraded stands. Nature type 0.512 (was 0.510), J 0.479 (was 0.467) on
> 250 and 36. Leave-block-out: 37 of 39 blocks get 0.511, one 0.499, one 0.503
> (was 38 at 0.502 and one 0.492).
>
> **4. The trajectory runs on the AE score, not the naturalness axis.** The
> yearly input is the AE score at the nature type scope: the stand's median cell
> score per year at stand grain, each cell's own score at pixel grain.
> `03_trajectory.py` therefore runs **after** `04_condition.py`, and no longer
> builds reference pools (3.7 s). `--input axis` restores the axis projection.
> The slope gates stay the 2nd and 98th percentiles of Hansen-clean units:
> stand -0.100 / +0.159 z/yr (axis: -0.093 / +0.068), pixel -0.083 / +0.184. The
> AE score drifts upward across the whole area (median stand slope +0.04 z/yr
> against -0.004 on the axis), so "improving" now means rising faster than
> unchanged stands drift.
>
> **5. The stable/fluctuating cut was recalibrated.** A stand is `fluctuating`
> when the sd of its yearly z-values is at least 0.50 z (stand grain) or 0.55 z
> (pixel grain). That is the 81.4th percentile of Hansen-clean units, which keeps
> the axis run's share of clean stands called fluctuating (18.6 %). The earlier
> fixed 0.30 z called 73 % of stands fluctuating, because the AE score jitters
> about twice as much year to year as the axis (0.57 against 0.29 z on unchanged
> stands).
>
> **What it cost, measured.** The stand-score change costs nothing: stand AUC,
> `god` against degraded, is 0.836 for the median and 0.829 for the mean-embedding
> probe (difference -0.008, 95 % CI -0.027 to 0.012). The trajectory input does
> cost. On the Hansen check the trend finds clearing less well than the axis did.
>
> | Hansen check, `declining` at 2.0 % false positives | AE score (shipped) | axis (`--input axis`) |
> |---|---:|---:|
> | cell grain: recall on cleared cells | 0.33 | 0.52 |
> | cell grain: slope AUC | 0.73 | 0.81 |
> | stand grain: slope AUC | 0.64 | 0.75 |
>
> At stand grain the difference is 0.11 (95 % CI 0.02 to 0.19). The trajectory
> classes are validated against Hansen at cell grain; read stand-grain
> `declining` as a screening flag, not a detector.
>
> **Counts, 2025.** Headline classes use the `local` scope; the 44 stands with no
> local score take their nature type class. Kalklindeskog 307 good / 157
> indistinguishable / 125 poor (was 311 / 162 / 116); olivinskog 12 / 61 / 75
> (was 20 / 56 / 72). 67 stands change `local` class; none jumps between good and
> poor. Olivinskog's drop in `good` is the threshold rising from 0.502 to 0.511
> with the olivinskog median at 0.48, not a bias (median minus probe: mean
> -0.003, sd 0.019). Trajectories: 582 stable, 115 fluctuating, 25 declining,
> 15 improving (was 559 / 124 / 33 / 21). By type, kalklindeskog 485 / 73 / 20 / 11
> and olivinskog 97 / 42 / 5 / 4. The slope gates and the sd cut are pooled across
> both forest types, which is why olivinskog's fluctuating count moved from 7 to
> 42 and kalklindeskog's from 117 to 73. That is a property of the shared cut,
> not evidence that olivinskog is unstable.
>
> **Speed.** `04_condition.py --boot-rng per-stand --batch-small 200 --workers 8
> --parallel process` scores in 2.4 min; the whole step takes 2.7 min wall.
>
> **Reading the rest of this document.** Experiments E00-E56 and T00-T05 were
> run with the mean-embedding stand score and the axis as the trajectory input.
> They are kept as the record of what was measured and are labelled "(axis input)"
> or "(mean-embedding score)" where a reader could take them for the shipped
> method. Thresholds, gates, counts and validation tables in sections 1 and 3 are
> current unless marked as research results.

---

## 0. Data

**AlphaEarth Foundations annual satellite embedding**, 64 bands, 10 m, 2017–2025
— the same product as Earth Engine's `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`,
read instead from the community mirror on
[Source Cooperative](https://source.coop/tge-labs/aef) (`tge-labs/aef`), so the
pipeline needs no Earth Engine account, quota or export job.

**Polygons**: the six mapped layers of `skog_kartlegginger.gpkg` — 737 stands,
148 olivinskog and 589 kalklindeskog, median 1.93 ha and 0.38 ha respectively.

**Field condition labels**: `tilstand` on the MI and NiN5k layers, assigned by
the surveyor. This is the asset the DegreeofRecovery paper did not have, and the
whole design here is built around it.

| `tilstand` | polygons | role |
|---|---:|---|
| `god` | 263 | good reference pool |
| `moderat` | 141 | **held out** — scored, never a reference |
| `dårlig` | 20 | bad reference pool |
| `sværtRedusert` | 16 | bad reference pool |
| *(none)* | 297 | scored, never a reference — 295 HB13 + 2 NiN5k |

### Reading the mirror efficiently

The mirror's COGs are **band-interleaved with 1024×1024 blocks**. A 32×32 window
therefore costs 64 block fetches (~40 MB), the same as reading a whole block:
measured from this workstation, a 32×32×64 window took **>90 s** and a full
1024×1024×64 block took **6–10 s**. Window size is nearly free; block count is
everything.

So the reader (`condition/aef.py`) never reads per-polygon windows. It resolves
the polygons to the set of 1024×1024 blocks they touch, reads each block once in
full, and cuts every polygon out of it in memory. For this AOI that is **783
block reads for all nine years** — about 31 GB and ~10 minutes at six concurrent
tiles, instead of a per-polygon read pattern that would have run for days.
`scripts/01_plan_reads.py` prints the plan before the run.

Two further mirror-specific facts, both handled in `condition/aef.py` and
checked in `tests/test_aef_reader.py`:

- The `.tiff` is **bottom-up** — positive y step, origin at the tile's south
  edge. The sibling `.vrt` presents it north-up but is a *warped* VRT that
  re-blocks to 128×512 and destroys the block alignment, so we read the `.tiff`
  and flip.
- Values are **int8 with nodata −128**. The dequantisation is
  `v = (raw/127.5)² · sign(raw)`, a signed square — **not** a global scale.

  This pipeline originally applied a linear `raw/127` read, on the assumption
  that the quantisation was one global factor which cosine distance would be
  invariant to. That assumption was wrong.
  `scripts/09_check_gee_parity.py` — written to check exactly this, and run for
  the first time on 2026-09-09 — measured both rules over 400 pixels of this AOI
  in 2024 against `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL`:

  | rule | cosine to GEE | implied scale | ‖v‖ | max abs difference |
  |---|---|---|---|---|
  | `linear` = raw/127 | 0.9617 | 0.380 | 2.531 | 0.2598 |
  | `signed_square` = (raw/127.5)²·sign | **1.000000** | **1.0000** | **1.000** | 0.0111 |

  AlphaEarth embeddings are unit-norm by construction and only the second
  recovers that; 0.0111 is the int8 granularity. The linear read therefore
  distorted the embedding **in direction** — the one error a cosine-based score
  cannot absorb — and the "scale-free" arguments elsewhere in this document and
  in `condition/axis.py` were sound reasoning from a false premise.

  **`condition.paths.AEF_DEQUANT` now defaults to `signed_square`, and the whole
  pipeline was re-extracted and re-scored on it on 2026-09-09.** The re-download
  read byte-for-byte the same int8 as the original run (verified against the
  int8 recovered analytically from the superseded table, max difference exactly
  0), so the only thing that changed between the two sets of results is the
  dequantisation. What it moved:

  | | linear | signed_square |
  |---|---|---|
  | stand AUC, local (both types) | 0.785 | 0.782 |
  | stand AUC, global | 0.747 | 0.775 |
  | pixel AUC, local / global | 0.764 / 0.721 | 0.732 / 0.768 |
  | kalklindeskog / olivinskog, stand local | 0.766 / 0.798 | 0.853 / 0.703 |
  | Youden J, local / global | 0.463 / 0.412 | 0.475 / 0.467 |
  | Hansen-cleared recall at 2% FPR | 0.513 | 0.524 |
  | stands changing local condition class | — | **222 of 737 (30.1%)** |

  Headline separation is about the same; the *decisions* are not, and the
  per-forest-type split reverses. The point is not that the score got better —
  it is that these numbers are now computed on the embedding Earth Engine
  serves, and the previous ones were not. Set `AEF_DEQUANT=linear` only to
  reproduce the superseded results; they are kept in
  `outputs/data/linear_dequant_2026-09-09/`.

---

## 1. Trajectory classification

RECOVER classified abandoned agricultural land by z-scoring an annual series,
fitting a Theil-Sen slope, and testing it with Mann-Kendall
(`analyze_trajectories.py`, `test_trend_classification.py`, "Option A"). It ran
on GPP and short vegetation height, neither of which now carries recent years.

The recipe transfers; the input does not. Mann-Kendall needs a **scalar per
year**, and AlphaEarth gives a 64-d vector. The choice of scalar decides what
"improving" means.

### What the shipped trajectory runs on

Since 2026-10-23 the scalar is the **AE score at the nature type scope**, the
same number `04_condition.py` reports: the stand's median cell score in each year
(stand grain) and each cell's own score (pixel grain). The trajectory and the
condition class are read off one number, and a trajectory can be checked against
the cell maps. `03_trajectory.py` reads the yearly scores that step 4 wrote, so
it runs after step 4 and takes seconds.

The cost is on the Hansen check (section 3, check 4c and the 2026-10-23 note
above): `declining` recovers 0.33 of Hansen-cleared cells at 2.0 % false
positives, where the axis recovered 0.52. The AE score is also noisier from year
to year, which is why the stable/fluctuating cut is recalibrated (below).

### The naturalness axis (axis input, `--input axis`)

The scalar used until October 2026, and still available with `--input axis`, is
the direction that separates the two reference pools step 2 needs:

```
w = normalise( mean(good pixels, unit-normalised) − mean(bad pixels, unit-normalised) )
c(t) = ⟨ x(t)/‖x(t)‖ , w ⟩            ∈ [−1, 1]
```

`c(t)` is a signed, monotone reparameterisation of exactly the good-vs-bad
contrast the condition score measures. Step 1 says which way a stand is moving
along that axis; step 2 says where it has got to. They are not two unrelated
numbers stapled together.

The two clouds are the ones step 2 scores against, and they carry the same two
corrections: the good side is survey-year-anchored, the bad side is WorldCover
transformed land in a 3–8 km ring. Measured separation along the fitted axis is
**+0.576 for kalklindeskog and +0.430 for olivinskog** — far enough apart that a
projection onto it is meaningful.

Properties that matter (these are measurements of the axis input):

- **Scale-free** — both `x` and `w` are unit vectors, so the int8 quantisation
  cancels.
- **It moves when the forest is actually cleared.** Across a Hansen loss year the
  projection falls by 0.097 on cleared pixels and by 0.0006 where nothing
  happened, separating the two at AUC 0.888 (95% CI 0.845–0.970, bootstrapping
  spatial blocks). The scalar the trend test runs on measures what it claims to;
  before this check that was an assumption.
- **Time-invariant by construction** — the axis is fitted once and held fixed:
  the good side is each `god` stand at its own survey year, the bad side is
  transformed pixels pooled over all years so the axis cannot encode one year's
  atmosphere. Re-fitting per year would move the ruler with the thing being
  measured and make a trend meaningless.
- **One axis per forest type** — olivinskog and kalklindeskog have different good
  and bad states, so the axis is never pooled across them.

Scalars we did not use: the year-to-year embedding distance `‖x(t) − x(t−1)‖`
detects disturbance but is unsigned, so it cannot separate recovery from
decline; PCA1 of the 64 bands (the closest analogue to RECOVER's GPP/height
joint signal) maximises variance, which in a forest stand is dominated by
phenology and terrain rather than condition.

### The test

Per polygon (mean `c` over its pixels each year) and, optionally, per pixel:

1. **z-score within forest type**, over all pixels and all years of that type —
   RECOVER's biome stratification, restratified to the unit that matters here.
   A slope of +0.1 z/yr then means the same thing in both forest types.
2. **Theil-Sen slope** — median of all pairwise slopes.
3. **Mann-Kendall** — S statistic, normal-approximation Z, two-sided p.

| class | rule |
|---|---|
| `improving` | Theil-Sen slope > `gate_hi` |
| `declining` | slope < `gate_lo` |
| `stable` | neither, and series sd < `sd_stable` |
| `fluctuating` | neither, and sd ≥ `sd_stable` |
| `no_data` | fewer than 6 finite years, or Var(S) ≤ 0 |

### The class rule gates the effect size, not the p-value

The rule above is **not** RECOVER's. RECOVER, and the first version of this
pipeline, required `p < 0.05` from the Mann-Kendall test. Validating that
against Hansen Global Forest Change loss year — Landsat-based, annual, and the
only label in this project that is not derived from AlphaEarth — showed the
significance rule to be correctly calibrated and badly underpowered:

| axis input | p < 0.05 | Theil-Sen magnitude gate |
|---|---:|---:|
| recall on Hansen-cleared pixels | 0.261 | **0.513** |
| false positives on Hansen-clean pixels | 0.019 | 0.020 |
| stand-grain AUC, cleared vs clean | 0.844 | **0.921** |

Same series (axis input), same Theil-Sen slope; only the decision rule changed.
Mann-Kendall asks whether a trend exists, which on nine annual points it can
rarely establish; a manager needs to know whether the trend is large enough to
act on. `gate_lo` and `gate_hi` are the 2nd and 98th percentiles of the slopes
of pixels Hansen says were never cleared, so the false-positive rate of each
class is fixed at 2% **by construction** rather than inherited from a p-value.
`02e_hansen_reference.py` builds that reference; `mk_p` is still reported.

Measured on this AOI, the shipped gates (AE-score input, `trajectory_gate.json`)
are −0.100 / +0.159 z-units per year at stand grain and −0.083 / +0.184 at pixel
grain. They differ because a stand's median series is smoother than a single
pixel's, so one gate per grain is calibrated. The improving gate sits well above
zero because the AE score drifts upward across the whole area (median stand slope
+0.04 z/yr): "improving" means rising faster than unchanged stands drift. For
comparison, the axis input gave −0.093 / +0.068 at stand grain (the 9 September
re-run; the first run gave −0.086 / +0.074 stand and −0.070 / +0.069 pixel).

The stable/fluctuating cut is calibrated the same way. `sd_stable` is the 81.4th
percentile of the series sd of Hansen-clean units: 0.50 z at stand grain, 0.55 z
at pixel grain. That keeps the 18.6 % of clean stands the axis run called
fluctuating, under a fixed 0.30 z cut. The AE score jitters about twice as much
as the axis on unchanged stands (0.57 against 0.29 z), and a fixed 0.30 z would
call 73 % of stands fluctuating. The cut and the slope gates are pooled across
both forest types.

Two corrections that did **not** help, recorded so they are not tried again.
The Hamed & Rao autocorrelation-corrected variance makes the test
*anti*-conservative here — 6.4% on a year-shuffled null against a nominal 5% —
because a nine-point autocorrelation estimate is mostly noise. And a
best-single-step statistic, which ought to suit a clearcut-then-recover shape
better than a monotone test, ranks worse per pixel than the slope does.

The significance rule is kept behind `--no-gate` for comparison. Without
`02e_hansen_reference.py` the pipeline falls back to it and says so.

Two deliberate departures from the RECOVER implementation:

- **Var(S) carries the tie correction**, `Var(S) = [n(n−1)(2n+5) − Σₜ t(t−1)(2t+5)]/18`.
  RECOVER assumed no ties. With nine annual steps on a quantised input, ties are
  common and the uncorrected variance inflates |Z| — it reports too many
  significant trends.
- **The stability gate is the standard deviation, not the coefficient of
  variation.** RECOVER used `cv < 0.3` because raw GPP is strictly positive. The
  yearly series is z-scored and centred near zero, where CV explodes and carries
  no information. `cv` is still reported, but must not be thresholded.

With nine observations the normal approximation to S is the usual practice but
remains an approximation. Treat p near the threshold as indicative and prefer
the effect size (`sen_slope`, in z-units/yr) for ranking.

---

## 2. Condition (Degree of Recovery)

The scoring maths is the published one, **vendored verbatim** as
`condition/core.py` from `DegreeofRecovery/paper_release/src/degree_of_recovery/core.py`
so results stay comparable:

```
dor = mean_cos(x → 5 nearest bad refs)
    / (mean_cos(x → 5 nearest good refs) + mean_cos(x → 5 nearest bad refs))
```

k = 5, cosine distance, 2,000-resample bootstrap 95 % CI, and a CI-vs-threshold
rule that returns `indistinguishable` rather than guessing when the interval
straddles the threshold. A score near 1 is close to the good pool.

Six things are re-defined for this project: the **unit** scored, how
**overlapping polygons** are handled, the **good pool**, the **time** a label
refers to, the **bad pool**, and **pool size**. Each has its own section below.
Three more — the **metric** the distances are taken in, the **spatial exclusion**
of the reference pools, and the **level the interval resamples at** — were added
after the validation work in `condition/research/` and have their own sections
after those.

### The unit: a pixel, not a polygon

The published method scores one probe per site. Here the probe is a **pixel**.
A stand is not in one condition — a kalklindeskog with a spruce plantation over
its northern half averages to a number describing neither half — and sub-stand
degradation is exactly what this project is looking for. Every pixel of every
stand is scored in every year 2017–2025 (2.0 M pixel-year scores), and a stand
is summarised as the *distribution* of its pixels: median, IQR, 10th percentile,
and `frac_poor`, the share of its area below the threshold.

Stand-level scores with a bootstrap CI are still produced, one per stand-year.
The stand score is the **median of the stand's cell scores** (since 2026-10-23;
before that it was the score of the stand's mean embedding, kept as
`dor_<scope>_probe`). The interval rescoring is described under "The interval
resamples donor stands".

There is deliberately **no per-pixel confidence interval**. The paper's
bootstrap resamples the reference pool, so it measures reference-pool sampling
error — which every pixel of a stand shares. Attaching it to each pixel would
present a common-mode uncertainty as independent per-pixel precision. The
genuine per-pixel spread is the quantiles above.

### Overlapping polygons

The six layers map the same ground repeatedly: **2,236.7 ha of polygon area sits
on 1,693.7 ha of ground**, and 353 of 737 stands share more than 1 % of their
area with another. The original extractor rasterised all polygons into one
integer label raster, so a shared pixel went to whichever polygon was drawn
last. The damage was not subtle: 152 stands kept under half their pixels, and
several `god` reference stands of up to 5 ha kept exactly **one** pixel — then
went straight into the good pool at that size.

`condition/membership.py` replaces that model. A piece of ground carries its
embedding **once**, keyed by `pixel_id`; which polygons cover it is a separate
many-to-many table. 30.6 % of pixels are claimed by more than one polygon. Three
things follow:

- a stand gets all its pixels back (median pixels per `god` stand: 25 → 52);
- a pixel two layers label contradictorily is `conflict` and enters no pool,
  instead of silently counting as whichever polygon won the draw;
- **leave-one-out becomes exact.** Excluding by `poly_id` never worked across
  overlaps — the probe's own ground returned to the pool wearing a neighbour's
  id. Pools are excluded by `pixel_id`, and donor stands within 20 m of the
  probe are excluded outright.

No re-download was needed: the collision lost *memberships*, not pixels, so the
correct membership is recovered by re-running point-in-polygon on the
deduplicated pixel centres.

### The good pool: same forest type, two scopes

The paper's WorldCover good pool cannot see this problem — an intact
kalklindeskog and a spruce stand planted on top of one are both WorldCover class
10. So the good pool is **`tilstand = god` stands of the probe's own forest
type**, and it is built at two scopes, both reported for every pixel:

| scope | pool | answers |
|---|---|---|
| `local` | the 32 nearest eligible `god` stands, within 50 km | is this stand poor *for its region*? |
| `global` | every eligible `god` stand of the type | is this stand poor *for the type*? |

They disagree where a stand is unusual for its region but not for the type,
which is informative rather than a defect.

### `tilstand` is a statement about a date

A surveyor who wrote `god` in 2017 said the stand was in good condition **in
2017**. These labels span 2015–2025, so some are eight years older than the
embedding being scored, and a stand cut in 2020 still carries its 2017 `god`.

So a donor contributes **only its survey-year pixels**, and a stand surveyed
before 2017 cannot be a donor at all. Of 263 `god` stands, 244 are usable: 13
were surveyed before AlphaEarth begins, 6 have too few unambiguous pixels left
after conflict resolution. The good pool is therefore a fixed cloud — "stands a
surveyor called `god`, as they looked when that was checked" — and does not move
with the probe year. Every output row carries `label_age = year − survey_year`.

The threshold is calibrated on labelled stands **in their own survey year**, for
the same reason.

**Ground the donor had already lost is dropped.** A `god` label describes the
day the surveyor stood there, but nothing stops part of the polygon from being
ground Hansen records as felled *before* that day, and a clearcut inside the good
pool flatters exactly the probes that look like clearings. Donor pixels with a
Hansen loss year at or before their donor's survey year are therefore excluded
(1,072 pixels across 35 donors; `--keep-cleared-donors` restores them). E25
measured the cost at nothing detectable — stand AUC 0.8051 → 0.8056, inside the
seed spread — so this is a safeguard rather than an improvement, kept because it
is free and because it is the configuration E26's independent corroboration of
the pooled transformed pool was run in.

### The bad pool: transformed land from WorldCover

The field labels give only 36 bad stands, 1,608 usable pixels across both forest
types — too thin to anchor an end-member. WorldCover supplies the other end,
where it is *not* blind: it can say where forest has been replaced outright.

**Cropland (40) and built-up (50) only.** Grassland (30) is excluded on purpose —
in Norway it absorbs natural fjell vegetation and semi-natural pasture as readily
as a clearcut — and bare/sparse (60) because Norwegian bare ground is
overwhelmingly rock and scree.

Pixels come from a **3–8 km annulus** around each stand, as in the paper: the
inner radius excludes the stand's own edge, the outer keeps the reference in the
same region and phenology. Eight stands have no transformed land within 8 km and
had their ring widened to 16 km; the radius used is recorded per stand.

**Those rings are then pooled per forest type, and that pooled cloud is what a
probe is scored against.** Extraction is unchanged — the set-cover below still
plans blocks so that every stand's own ring is populated — but at scoring time
the rings of one forest type become a single end-member for it, capped at 2,000
pixels per (type, year). A per-stand ring asks each probe a slightly different
question, because its transformed end-member is whichever farmland happens to
surround it, so two stands on the same hillside are judged against two different
yardsticks and the difference between their scores carries that as noise. See
[E22 and E38](#should-the-end-member-be-pooled-or-drawn-per-stand-e21e22) for
what pooling is worth and what it does not settle.

#### Making that affordable

The rings touch **329** AlphaEarth blocks per year against the 87 the stands
themselves need, and a block costs ~40 MB whether you read one pixel of it or a
million — about 118 GB, naively. But no stand needs its whole ring, only a few
hundred reference pixels from it, so block selection is a **set-cover problem**:
choose the fewest blocks such that every stand has ≥150 transformed pixels
inside its own annulus. Greedy by marginal deficit reduction, since a block near
a town serves dozens of stands at once.

That picks **54 blocks — 2.2 GB per year, ~4 minutes for all nine years.**
`scripts/02c_plan_bad_pool.py` solves it and prints the cost before
`02d_extract_bad_pool.py` pays it.

One trap, worth recording: **AlphaEarth populates ground only in its natural UTM
zone.** A 31N tile carries valid coordinates for easting 662,000 — 6.1 °E, which
is zone 32 — but every band there is nodata (measured: 31 % finite on the 31N
tile, 100 % on the 32N tile covering the same ground). Candidates outside a
block's own zone are silently unreadable; before that filter was added, 18 stands
lost their entire bad pool. Pinned in `tests/test_worldcover.py`.

#### Which land should the end-member actually be? (E15/E16)

Cropland and built-up were chosen by argument, and grassland (30) and bare (60)
excluded by argument: in Norway grassland absorbs natural fjell vegetation and
semi-natural pasture as readily as a clearcut, and bare ground is mostly rock and
scree. Those arguments have now been measured. `scripts/02f_bad_pool_variants.py`
draws seven candidate strata from each stand's own annulus -- the shipped
`converted`, Hansen-cleared forest, grass, shrub, bare, wetland and moss/lichen --
and E15 swaps them into the scorer one at a time with every other choice fixed.

The result is that **the pooled ranking and the within-locality ranking are very
nearly inverted**:

| transformed pool | pooled pixel AUC | within-locality AUC |
|---|---:|---:|
| converted (the shipped definition) | **0.720** | **0.561** — last of twelve |
| converted + grass | 0.705 | 0.615 |
| everything | 0.667 | 0.619 |
| cleared forest | 0.655 | 0.587 |
| grass | 0.638 | 0.623 |
| wetland | 0.632 | **0.653** |

All eleven alternatives beat the shipped pool within a locality. Cropland and
built-up are concentrated near towns, so distance-to-converted-land is largely a
statement about which locality a stand is in; the open-vegetation classes are
distributed inside a locality as well as between them, which is where a condition
signal has to live. It also fixes the imbalance check 3 reports: the shipped pool
gives `m_bad` 0.698 against `m_good` 0.616, while `cleared` gives 0.617/0.616 and
`grass` 0.609/0.616 -- a genuine ratio of two comparable terms rather than one
term wearing a ratio's clothes.

**The two forest types want opposite pools**, which is the finding with an
ecological reason behind it:

| transformed pool | kalklindeskog (1097 pairs) | olivinskog (146 pairs) |
|---|---:|---:|
| converted | **0.540** | **0.719** |
| wetland | **0.664** | 0.575 |
| grass | 0.625 | 0.603 |
| converted + grass | 0.603 | 0.705 |

Olivinskog sits on ultramafic substrate that is naturally open and sparse, so
grassland and bare ground beside an olivinskog are its own habitat rather than
evidence of transformation -- exactly the argument this document already made, now
confirmed for olivinskog and refuted for kalklindeskog, where converted-only is
barely above chance inside a locality. The axis and the whitener are already
fitted per forest type; the transformed pool is the one reference that is not, and
this is the argument for changing that.

E16 re-runs the leading pools over five seeds under the donor-clustered interval.
Seed sd on the within-locality AUC is 0.011-0.021, so the +0.056 that `grass` buys
is about four of them. Decisiveness is where the candidates separate: `grass`
leaves 29.8% of stands `indistinguishable` against the shipped pool's 30.2% -- no
cost at all -- while `cleared` leaves 45.6% and `everything` 47.3%.

**Hansen-cleared forest was the hypothesis and it lost.** It is a nearer
end-member and it balances the two distance terms, but it improves the
within-locality separation less than grass does and costs fifteen points of
decisiveness. It carries a second problem even where it wins: Hansen loss is this
project's only independent validation of the trajectory step, and spending it on
the condition pool as well would end that independence unless the two uses are
split by epoch.

Nothing here is shipped. `04_condition.py --bad-pools` selects a pool and the
default is still `converted`, because the evidence is four localities -- see the
limits below.

#### Should the end-member be pooled, or drawn per stand? (E21/E22)

E15 swapped the transformed pool's **land cover** eleven ways and kept its
geometry: every candidate was still the probe stand's own 3–8 km annulus. E21
changed the geometry by accident — it needed a pooled transformed cloud as a
control for the percentile scorer below — and the control beat the shipped score
on every metric, in all four configurations. E22 then priced it properly, over
five seeds, against a size control that raises the annulus cap from 300 to 2,000
so that "more neighbours to take a k=25 mean over" is not the explanation:

| config | transformed pool | stand AUC | pixel AUC | within-locality (stand) | Youden J |
|---|---|---:|---:|---:|---:|
| global / 3 km | annulus, cap 300 (shipped) | 0.765 ±0.011 | 0.735 ±0.011 | 0.667 ±0.011 | 0.437 |
| | annulus, cap 2000 (size control) | 0.761 ±0.011 | 0.734 ±0.010 | 0.662 ±0.013 | 0.434 |
| | **pooled per forest type** | **0.799 ±0.009** | **0.783 ±0.007** | **0.720 ±0.014** | **0.473** |
| local / 3 km | annulus, cap 300 (shipped) | 0.782 ±0.002 | 0.769 ±0.006 | 0.626 ±0.003 | 0.479 |
| | annulus, cap 2000 (size control) | 0.780 ±0.002 | 0.768 ±0.005 | 0.626 ±0.002 | 0.474 |
| | **pooled per forest type** | **0.805 ±0.003** | **0.796 ±0.004** | **0.667 ±0.004** | **0.519** |

**The size control moves nothing** — 0.765 against 0.761, well inside the seed
spread — so the pool cap is not the mechanism. What is left is the pooling, and
it is worth +0.023 stand AUC, +0.027 pixel AUC and +0.041 within-locality AUC at
the deployed configuration, against a seed sd of 0.002–0.011.

The direction is the surprise. E15's diagnosis of the shipped pool was that
cropland and built-up cluster near towns, so distance-to-converted-land is partly
a statement about *which locality a stand is in* — which predicts that pooling,
by removing the stand's own neighbourhood from the comparison, should cost
within-locality separation. It gains 0.041 of it instead. The reading that
survives is the opposite one: the per-stand annulus makes `m_bad` a measure of
how far the stand is from **its own** farmland, so two stands on the same
hillside are scored against two different end-members and the difference between
their scores carries that as noise. One end-member per forest type removes it.

This is the same conclusion E15 reached from the other direction — the
transformed pool is the one reference not fitted per forest type, and it should
be — arrived at without changing the land cover at all. The two are composable
and have not been measured together.

**Now shipped, and still qualified.** `04_condition.py` pools the annuli per
forest type by default (`--annulus-bad` restores the per-stand ring;
`condition.refs.PoolBuilder.type_bad_rows` is the pool). What settled it was not
the AUC above. E24 confirmed on a full-depth redraw that the stand-AUC gain does
**not** replicate on an independent draw of the same land cover (+0.001 against
+0.023 here) while the within-locality gain does, so the numbers in this table
should be read as a property of the v1 draw as much as of pooling. What justifies
deployment is E38, which ran the same contrast at the deployed local scope over
three seeds with the error-side metrics in view:

| local scope, 404 common stands | per-stand ring | pooled per type |
|---|---:|---:|
| degraded recall | 0.533 | **0.581** |
| degraded stands wrongly called good | 0.105 | **0.038** |
| decided | 0.558 | **0.616** |
| balanced accuracy among decided | 0.751 | **0.829** |
| stand AUC / within-locality | 0.789 / 0.635 | 0.804 / 0.689 |

Cutting wrong reassurance from 10.5% to 3.8% is the largest error-side gain in
the ledger, and E26 reproduces the pooling advantage on Hansen-cleared labels,
which are independent of the field survey. E27 had earlier read this change as a
wash, but it was a single seed and measured only decisiveness and
accuracy-when-decided — with 250 good stands against 36 degraded, neither can
see a degraded stand being turned into a confident *good* call. The cost is real
and unchanged: about a fifth of stands change reported class.

#### Do the land cover and the pooling compose? (E23)

E15 changed *what* the transformed pool is made of and kept the per-stand
annulus; E22 changed *where* it is drawn from and kept cropland and built-up.
Both were read as removing the same thing — a locality signal — which predicts
that they are one fix arriving twice, and that pooling should help most exactly
where E15 said the land cover hurts most: `converted`. E23 crosses the two, at
the deployed configuration, five seeds, reading both geometries off one feature
table per (pool, seed) so nothing but the geometry differs within a row.

**The prediction is wrong, and inverted.** Pooling buys nothing wherever the pool
contains converted land and a great deal wherever it does not (Δ = pooled −
annulus, five-seed means; seed sd 0.002–0.014 on stand AUC, 0.003–0.028 on the
within-locality one):

| transformed pool | Δ stand AUC | Δ pixel AUC | Δ within-locality (stand) |
|---|---:|---:|---:|
| converted (control) | **−0.009** | −0.022 | +0.002 |
| converted + cleared | +0.021 | +0.002 | −0.004 |
| converted + grass | +0.014 | +0.002 | +0.007 |
| everything | +0.031 | +0.020 | −0.001 |
| cleared forest | +0.042 | +0.017 | +0.002 |
| moss/lichen | +0.050 | +0.018 | **+0.067** |
| wetland | +0.059 | +0.040 | −0.028 |
| grass | +0.070 | +0.059 | +0.029 |
| all open veg | +0.072 | +0.062 | +0.016 |
| bare | **+0.112** | +0.095 | +0.018 |

So the two are **neither independent nor redundant — they interact, with a sign
nobody predicted**. Sample sufficiency is not the explanation: `grass`, `bare`
and `all open veg` all fill the 300-pixel annulus cap for every one of the 421
labelled stands, so pooling is not rescuing them from a starved pool. (`shrub`
and `moss/lichen` genuinely are starved — they serve only 75 and 99 stands, and
41 and 31 more would become scoreable if the pool were pooled — but they are not
where the gain is concentrated.)

The reading that fits is the one E15 already made for olivinskog, generalised:
**grass, bare, wetland and moss beside a stand are often that stand's own
habitat**, not evidence of transformation. Ultramafic substrate is naturally open
and sparse; so is ground beside a kalklindeskog on limestone. A per-stand annulus
of those classes is therefore contaminated with the stand's own surroundings and
makes a poor end-member, and pooling across the forest type averages that
contamination away. Cropland has no such problem — farmland 3–8 km from an
olivinskog is not olivinskog habitat — so its annulus is already clean and
pooling adds nothing. This is a hypothesis that fits twelve rows, not a tested
mechanism.

**E23 does not replicate E22, and the reason is the draw, not the method.**
E22 measured pooling on the v1 pool (`bad_pixels.parquet`) at +0.023 stand and
+0.041 within-locality; E23's `converted (control)` measures the same contrast on
the v2 candidate table at −0.009 and +0.002. The two are not the same object.
`02f_bad_pool_variants.py` samples each v2 candidate pixel in **exactly one
year** — 159,305 pixels, 159,305 (pixel, year) rows — where every v1 pixel exists
in all nine. So the per-(type, year) cloud v2 can form is up to nine times
thinner: v1's is at the 2,000 cap for every stand-year, v2's falls to 225 in the
thinnest one. The redraw costs 0.054 of within-locality AUC on its own
(`converted` annulus: 0.626 on v1, 0.572 on v2), which is larger than most of the
effects in the table above.

**Nothing here beats the shipped configuration.** The best cell on the metric
E15/E16 argue for is `wetland` + annulus at 0.623 ±0.006 and `grass` + pooled at
0.621 ±0.004, against the shipped v1 pool's 0.626 ±0.003. The best stand AUC is
`converted + grass` + pooled at 0.769 ±0.004, against the shipped 0.782 ±0.002.

**What would settle it:** re-extract the same candidates in every year, so all
twelve land covers can form a pooled cloud of the same depth as v1's. That is
`scripts/02g_bad_pool_v2_allyears.py`, and E24 below is this table run again on
it. Read E24 rather than acting on anything here.

#### The same cross, on a draw that can answer it (E24)

`scripts/02g_bad_pool_v2_allyears.py` re-extracts E15's stratified sample — the
same 103,914 locations from the same cached shards, same strata, same memberships
— in all nine years instead of each stand's survey year alone. 918 requests, 11
minutes, no failures, **100% of locations complete in all nine years**. Every
pool's per-(type, year) cloud now clears the 2,000 cap, `shrub` (5,038) and
`moss` (7,690) included, where v2 fell to 225.

**The draw is verified identical where it should be.** The annulus geometry never
used more than one year per candidate, so re-extracting the others must not move
it — and across all twelve land covers the v3-minus-v2 difference on every
annulus row is **0.000** on both stand and within-locality AUC. Only the pooled
rows move, which is exactly the defect and nothing else.

**The defect was real but modest.** v3 minus v2, pooled rows: `converted` +0.011
stand / +0.024 within-locality, `moss/lichen` +0.035 / +0.049, `grass` +0.004 /
+0.018, and the rest within ±0.011. So it was worth correcting and it was not
worth the whole of E23's inversion.

**E22 half-survives.** Its within-locality gain reproduces on an independent
redraw of the same land cover — +0.025 on v3 `converted` against +0.042 on v1 —
but its stand-AUC gain does not: +0.001 against +0.023. Since E15/E16 argue the
within-locality number is the one that answers a manager's question, the half
that survives is the half that matters, but the headline should be stated as
+0.025, not +0.042.

**E23's interaction survives intact, on a clean draw.** Pooling is still worth
least to anything containing converted land and most to the natural open classes
(within-locality Δ: `converted + cleared` −0.006, `everything` −0.000, `cleared`
+0.007, `converted + grass` +0.015 — against `grass` +0.047, `moss/lichen`
+0.116, `grass + shrub + moss` +0.036). The reading offered there stands: an
annulus of grass or moss beside a stand is partly that stand's own habitat, and
pooling across the forest type averages that contamination away, while farmland
3–8 km away was never the stand's habitat and its annulus was already clean.

**And they compose super-additively.** Within v3, where every cell is the same
draw, on within-locality stand AUC:

| | annulus | pooled per type |
|---|---:|---:|
| `converted` (shipped definition) | 0.572 ±0.003 | 0.598 ±0.003 |
| `grass` | 0.592 ±0.008 | **0.639 ±0.007** |

Land cover alone buys +0.020, geometry alone +0.026, both together **+0.067** —
an interaction term of +0.021 on seed sds of 0.003–0.008. Stand AUC tells the
same story more starkly: `grass` is 0.072 *worse* than `converted` at the annulus
(0.683 against 0.755) and exactly level once pooled (0.757 against 0.757). Grass
is not a worse end-member; it is an end-member the per-stand annulus cannot
deliver.

**Pooling also makes the sparse strata usable at all.** With a per-year cloud,
the stands whose own annulus cannot reach k but whose pooled cloud can rise from
41 to **341** for `shrub` and from 31 to **315** for `moss/lichen`. Those two
serve only 75 and 99 stands under the annulus; pooled, they would serve nearly
all 421.

**What still cannot be compared.** The v1 row and the v3 rows are different
*sampling designs*, not just different years: v1 is a 54-block set-cover read off
the Source Cooperative COGs, v3 is a per-annulus Earth Engine stratified sample.
v1 `converted` pooled remains the highest within-locality number measured
anywhere (0.667 ±0.004) and v3 `converted` pooled reaches 0.598, and that 0.069
is design, not method. So the honest comparison is *within* v3 — where the best
available pool is `grass` + pooled at 0.639 against the shipped definition's
0.572 — and the v1 row is there only to show E22's original contrast in the same
table.

**Still not shipped**, and now for a smaller reason than before: the evidence is
six degraded localities, and moving both the land cover and the geometry at once
changes every published number. But two separate experiments now agree that the
shipped per-stand `converted` annulus is the weakest cell in a 26-cell table, and
`04_condition.py --bad-pools` already exists to select a different one.

**Read E26 before acting on this section.** On an independent label set with
eleven degraded localities instead of four, the *geometry* result strengthens and
the *interaction* above does not survive — pooling helps the converted-containing
pools too, and the land-cover spread collapses from 0.041 to 0.012. The
super-additive composition is a property of the field labels, not of the method.

#### The good pool contains clearcuts (E25)

Found while assembling E26's labels, not looked for. A donor contributes its
**anchor-year** pixels, and 1,072 of the pixels donors own — 258 rows once the
60-per-donor cap is applied, across 29–35 donors — are ground Hansen says had
already been felled by that year. The reference cloud that defines "good
condition" contains clearcut, in the shipped configuration, today.

It is a reference defect, so it inflates `m_good` for precisely the probes that
look like clearings, which is the direction that hides degradation. Removing it
should therefore *raise* separation if it matters. Five seeds, both draws, both
geometries:

| draw | geometry | good pool | stand AUC | within-locality |
|---|---|---|---:|---:|
| v1 `converted` | annulus | as shipped | 0.782 ±0.002 | 0.626 ±0.003 |
| | | cleared removed | 0.783 ±0.001 | 0.628 ±0.002 |
| v1 `converted` | pooled | as shipped | 0.805 ±0.003 | 0.667 ±0.004 |
| | | cleared removed | 0.806 ±0.005 | 0.667 ±0.006 |
| v3 `converted` | annulus | as shipped | 0.755 ±0.003 | 0.572 ±0.003 |
| | | cleared removed | 0.757 ±0.002 | 0.570 ±0.005 |

**Real, and immaterial.** Every difference is inside the seed spread. 258 rows of
a 2,000-row pool is too few to move a k=25 mean. So this is worth fixing because
it is wrong and the fix is free — `condition.refs.anchor_donors` already filters
donors on role and could filter pixels on Hansen loss year in the same pass — and
not worth re-running anything for. Its real use is as a precondition for E26,
where scoring felled ground as degraded against a pool containing felled ground
would have measured nothing.

#### Hansen-cleared ground as independent labels (E26)

Every within-locality number in this document rests on the **four** blocks that
hold both a `god` and a `dårlig` stand. That is the reason E22, E23 and E24 gave
+0.041, −0.009 and +0.025 for one contrast: the method was never moving that
much, the evidence was too thin to hold still. Hansen loss is a second,
independent statement about the same ground — a pixel felled in 2019 is degraded
in 2020, whoever surveyed it — and using it as an extra degraded class roughly
triples the base:

| | degraded units | blocks | blocks with both classes |
|---|---:|---:|---:|
| field labels | 36 stands | 6 | **4** |
| + Hansen-cleared | 4,668 px in 29 (stand, year) cells | 15 | **11** |

Two rules, in `research/hansen_labels.py`. E15's `cleared` transformed pool is
**refused** — it is drawn from the same Hansen loss, so scoring against it would
be marking the exam with the answer sheet, which removes `cleared forest`,
`converted + cleared` and `everything` from the table. And the good pool has its
own Hansen-cleared pixels stripped first (E25). The probe grain is the pixel and
that is the point: 8 of the 29 cells lie inside stands the surveyors called
`god`, which a single stand-level score cannot see by construction.

**The ranking transfers.** Over the 20 variants both experiments share, the
Spearman correlation between the field-label and Hansen-label orderings, compared
at matched grain:

| | ρ | p |
|---|---:|---:|
| pixel AUC | **+0.887** | <0.0001 |
| stand AUC | +0.839 | <0.0001 |
| within-locality, pixel | +0.671 | 0.0012 |
| within-locality, stand | +0.556 | 0.011 |

**Pooling is corroborated; the interaction is not.** Restated as the thing two
label sets can disagree about — what pooling is worth, pool by pool
(within-locality pixel AUC):

| transformed pool | Δ on field labels | Δ on Hansen labels |
|---|---:|---:|
| `bare` | +0.123 | +0.122 |
| `grass` | +0.066 | +0.079 |
| `all open veg` | +0.070 | +0.064 |
| `grass + shrub + moss` | +0.059 | +0.066 |
| `wetland` | +0.048 | +0.055 |
| `moss/lichen` | +0.042 | +0.045 |
| v1 `converted` | +0.035 | +0.084 |
| `converted` (v3) | **−0.017** | **+0.035** |
| `converted + grass` | **−0.007** | **+0.019** |
| `shrub` | −0.003 | −0.002 |

The deltas agree at ρ = +0.733 (p = 0.016), pooling helps in 7 of 10 pools on the
field labels and **9 of 10** on Hansen's, and for the six open-vegetation pools
the two label sets agree to within 0.013. **The pooled transformed cloud is now
the best-supported unshipped change in this document.**

But E23/E24's *interaction* — that pooling helps only pools without converted
land — does **not** survive. It rested entirely on the converted-containing pools
showing no gain, and on Hansen's labels they gain +0.019 to +0.084. The most
likely explanation is the one the interaction was invented to describe, pointed
the other way: the 36 `dårlig` stands cluster in the same few localities as the
converted annuli that serve them, so on the field labels the per-stand annulus
gets credit it cannot earn anywhere else. With degraded ground spread over 11
blocks instead of 4, that credit disappears.

**And the land cover barely matters once pooled.** On Hansen labels the pooled
pools are `converted + grass` 0.780, `grass` 0.771, `converted` 0.768, `all open
veg` 0.757 — a spread of 0.012 where E24's field-label table had `grass` beating
`converted` by 0.041. Read the two together and the conclusion is narrower and
firmer than E24's: **the geometry is the finding, the land cover is not.**

**Caveats that stop this being decisive.** Hansen sees outright felling, while
`dårlig`/`sværtRedusert` also covers spruce plantation, grazing damage and
structural degradation — so this widens the evidence and narrows the question.
The pools are not scored on identical evidence: `shrub` and `moss/lichen` reach
only 3 and 5 blocks because their annuli cannot serve the felled cells, and v1
reaches 15 blocks against v3's 12, so the v1-vs-v3 rows remain incomparable here
for a second reason on top of the sampling-design one. And 8 of the 29 felled
cells sit inside `god` stands, which is the interesting part but also means the
two classes are not drawn from disjoint stands.

#### The pooled end-member, priced in stands rather than AUC (E27)

E26 left the pooled transformed cloud as the best-supported unshipped change
here. Every number supporting it is an AUC, and an AUC is not what this pipeline
reports. What it reports is a verdict: a stand's score (the mean-embedding score when E27 was run; the median of its cells now), a bootstrap interval
over donor stands, a threshold fitted outside the stand's own block, and
`indistinguishable` when the interval straddles it. So E27 runs that whole path
both ways and counts stands.

| | annulus (shipped) | pooled per type |
|---|---:|---:|
| `indistinguishable` | 43.6% | **39.6%** |
| median CI width | 0.071 | 0.069 |
| labelled stands decided | 155 | 162 |
| correct when decided | **72.3%** | 71.6% |

| annulus ↓ / pooled → | good | indistinguishable | poor |
|---|---:|---:|---:|
| **good_condition** | 115 | 16 | **0** |
| **indistinguishable** | 18 | 128 | 30 |
| **poor_condition** | **0** | 16 | 81 |

**80 of 404 stands — 19.8% — change their reported class.** Of the 55 labelled
stands that moved, 18 became correct, 14 became wrong and 23 moved between two
non-answers: a net of four, on fifty-five movers.

Two readings, and the second is the one that matters.

The reassuring one: **no stand crosses the diagonal.** Not one goes from
`good_condition` to `poor_condition` or back. Every movement passes through
`indistinguishable`, so the change does not reverse any verdict this pipeline has
issued — it only moves stands in and out of the undecided band.

The sobering one: **the AUC gain does not survive translation into the decision
rule.** Pooling is worth +0.04 to +0.12 of within-locality AUC across two label
sets, and it converts into four percentage points of decisiveness bought at
seven-tenths of a point of accuracy-when-decided — a wash, on 155 decided stands.
The reason is visible in the third row of the table: the interval is wide
(median 0.069) relative to the effect, and the leave-block-out threshold moves
with the pool as well, so a better-ranking score lands in almost the same place
once both are accounted for.

**So this too should not ship**, and now for a reason that has nothing to do with
thin evidence. A change that reclassifies a fifth of the stands, reverses no
verdict, and nets four correct answers out of fifty-five movements is not paying
for the disruption. E09's finding applies again: with an honest donor-clustered
interval, decisiveness is the binding constraint, and improving the *ranking* is
not the way to buy it.

**What this does not settle.** These are the 421 labelled stands, not the 737 the
product ships; a product-level count needs the transformed pool extracted for
every mapped stand, which `02f` has only ever done for the evaluation set. And it
prices the geometry alone, at the shipped `converted` land cover — which is the
right thing to price, because E26 is what removed the reason to change the land
cover.

#### Global Human Modification: measured, and rejected

gHM v3 (`projects/sat-io/open-datasets/GHM/HM_2022_90M`, Theobald et al. 2025) was
the obvious source for an end-member on the forest manifold: its `FR` band is
"biological resource use", which is forestry. It cannot serve. Over WorldCover
tree cover in these annuli `FR` has median 0.000, 90th percentile 0.000, and only
2-4 % of pixels above 0.1. The band reads correctly -- an Oregon clearcut mosaic
gives a 90th percentile of 0.248 against Elverum's 0.013 -- so `FR` is a
forestry-*concession-presence* layer with essentially no Fennoscandian coverage,
not a use-intensity layer. `AG`, `BU`, `EX` and `NS` are identically zero inside
the stands.

gHM is worth carrying for a different reason. `TI` (transport corridors) and `HI`
(human intrusion) separate the field labels **backwards** -- AUC 0.415 and 0.441,
degraded stands being the more remote ones -- and median `AA` runs 0.318 for
`god`, 0.305 for `dårlig` and 0.165 for `sværtRedusert`. That is a free 90 m
measurement of the remoteness confound the next section is about, and it belongs
next to `area_ha` in the confound controls rather than in a reference pool.


### Pool size

The published pools held ~150 reference points per site; a `god` pool here is
every pixel of 244 stands. Both pools are capped and **stratified by donor
stand** (60 pixels per donor, 2,000 per global pool, 300 bad per stand) under a
fixed seed, so one 200 ha olivinskog stand cannot supply most of the yardstick.

The global pool is drawn **once per forest type** and each stand masks its own
exclusions out of it; rebuilding it per stand would concatenate the same rows
589 times and discard 99 % of the work. Together with expressing the score as a
matmul (`1 − X̂ P̂ᵀ`, k smallest by `np.partition`) rather than a per-probe
Python loop, that is what makes 2.0 M pixel-year scores a 3-minute run.

### The metric: cosine distance on a shrunk-whitened embedding

The published scorer measures plain cosine distance in the raw 64-d embedding.
Over this AOI those axes are strongly anisotropic — a handful of directions
carry most of the terrain, phenology and acquisition variance — and a plain
cosine distance lets them dominate the good-vs-bad contrast. So distances are
taken after

```
x  ->  normalise( Σ_λ^{-1/2} (x/‖x‖ − μ) )
Σ_λ = (1 − λ) Σ + λ (tr Σ / p) I ,   λ = 0.2
```

with μ and Σ fitted **on unlabelled pixels of the same forest type only**, so no
label information enters the metric that is then used to measure distance to the
label classes. This is the single biggest gain in the project: pooled per-pixel
AUC **0.627 → 0.684** and olivinskog **0.579 → 0.634** on the corrected
embedding (E04 unwhitened against E12 at shrinkage 0.2, `global`), under the
leave-block-out protocol below. *(The linear read gave 0.649 → 0.713 and
0.624 → 0.683; the gain is smaller but the conclusion is unchanged.)*

The shrinkage is not a tuning nicety. Whitening divides by every eigenvalue, so
the smallest — the worst estimated — get the largest weight, and the score then
swings with whichever donors were drawn. At λ = 0 everything except the ranking is worst: the
honest interval widens until a large share of stands cannot be classified. The
original argument for shrinking was that the held-out `moderat` class stopped
landing between the anchors at λ = 0 — **on the corrected embedding it does not**,
so the case for λ = 0.2 is now the AUC and the decisiveness instead: pixel AUC
0.684 against 0.648 at full whitening, and 31.6% of stands undecided against
39.9%.
`--no-whiten` is the published metric; `--shrink 1.0` is equivalent.

### A probe never sees a donor from inside 3 km

The pools exclude the probe stand and anything overlapping it. That is not
enough. The six layers map the same localities repeatedly and `god` stands
cluster, so the nearest eligible donor is routinely a few hundred metres away —
same hillside, same orbit, same acquisition date — and the score then partly
measures locality rather than condition.

**The good pool therefore excludes any donor whose centroid is within 3 km**, and
3 km is not a new number: it is the inner edge of the annulus the *transformed*
pool has always used, so both pools now obey one contamination rule and there is
no second radius to tune. `--exclude-km` changes it; `--no-exclude-block` removes
every spatial rule and warns.

This replaced an earlier fix that excluded the probe's whole 5 km spatial block
from the pool. That rule conflated three separable jobs — keeping contaminated
ground out of the pool, matching a reference to its region, and holding out
localities so an accuracy figure is not a fit reported as a validation — and did
the first by destroying the second, which is the entire purpose of the `local`
scope. **Blocks are still used for the third job and only for it**: the threshold
is fitted leave-block-out and every interval resamples blocks (`condition/score.py`).

E20 swept the pool rule at the shipped estimator over five seeds, with the
evaluation protocol held constant:

| scope | pool rule | stand AUC | pixel AUC | olivinskog px | m_good alone | Youden J | donors |
|---|---|---:|---:|---:|---:|---:|---:|
| global | 3 km | 0.760 | **0.735** | **0.706** | **0.654** | 0.422 | 159 |
| global | whole block | 0.705 | 0.716 | 0.687 | 0.626 | 0.382 | 115 |
| local | 3 km | **0.789** | **0.769** | **0.761** | **0.685** | 0.488 | 8 |
| local | whole block | 0.750 | 0.697 | 0.660 | 0.593 | 0.403 | 8 |

Seed sd is 0.002–0.011. The column that matters most is **m_good alone**: under
the block rule the reference side contributed almost nothing and, at `local`,
separated the labels *backwards*. Excluding a whole locality was removing most of
what the good pool had to say. The block rule also left six more stands with no
local donor at all. `--exclude-block` restores it.

Two cautions. The sweep is not perfectly monotone (2 km scores below both 1 km
and 3 km at global), so 3 km is "the annulus edge also works well here" rather
than a tuned optimum — it was chosen because it was already justified on the
transformed side. And this is one AOI with 36 degraded stands in six localities.

### The interval resamples donor stands, not reference pixels

The paper's bootstrap resamples the distance vector row by row. That is right
when the pool is 150 independent reference points and wrong here, where 2,000
reference pixels come from about 115 donor stands: sixty pixels of one stand are
one stand seen sixty times. Resampling whole donor stands (and, on the
transformed side, whole 500 m localities) widens the median stand interval from
**0.068 to 0.097** and moves the share of stands whose interval straddles the
threshold from **21% to 30%**. **About one stand in eleven was carrying a
condition class the reference sampling never supported.** *(Re-run values;
the linear read gave 0.074 → 0.105 and 20% → 32%.)*

Two things bring that back without lying about it: `k = 25` rather than 5 —
a five-nearest mean out of 2,000 references is dominated by whichever single
donor happens to be closest, and averaging over more of them cuts the interval
by a third at no cost in separation — and the shrunk metric above. Together they
leave ~22% of stands unclassifiable, below the original 20%'s honest equivalent.

`condition/score.py` computes the clustered interval exactly, not by
approximation: only a cluster's own k smallest distances can ever be among the
pool's k smallest, so each draw needs a (n_clusters, k) table rather than a
concatenation of the whole pool. `--legacy` restores the row-wise version.

**Shipped interval for the median stand score (2026-10-23).** One cluster draw is
shared by all cells of a stand-year; every cell is rescored under it and the
draw's stand value is the median over cells. A stand with more than 2,001 cells
uses 2,001 cells at evenly spaced ranks of the cell scores, shifted to the
full-stand median. The point estimate uses all cells (the shortcut agrees to
within 1e-4). Median interval width is 0.049 at `local` and 0.042 at nature type.
The widths quoted elsewhere in this section (0.068, 0.097, 0.064) are
mean-embedding results.

Two opt-in flags change how the resamples are drawn, never what they
estimate. `--boot-rng per-stand` seeds each stand's generator from
`(seed, crc32(poly_id))` instead of consuming one generator in stand order, so a
stand's interval no longer depends on which stands were scored before it and
stands can be scored in parallel. `--boot-draw thinned` uses the fact that only
the `m` clusters owning one of the prefix's smallest distances (about 200 of
1,300) can move a resample's kNN mean: it draws how many of the `c` picks land
on them, `Binomial(c, m/c)`, and then that many uniform picks among the `m` --
exactly the joint law of their multiplicities under the full draw, from about
`m + 1` random numbers instead of `c`. A resample the prefix cannot settle gets
the rest of its draw and goes to the exact path. Both give the same resample
distribution from a different random stream: the DoR is unchanged and the
interval bounds move by bootstrap noise only. The default (`shared`, `full`)
reproduces earlier intervals exactly.

### The threshold

Shipped values (median stand score, 286 labelled stands in their survey year):
`local` 0.511, Youden J 0.564, sensitivity 0.735, specificity 0.829 on 238 `god`
and 35 degraded; nature type 0.512, J 0.479 on 250 and 36. Leave-block-out, 37 of
39 blocks get 0.511, one 0.499 and one 0.503. Before the median score, `local`
was 0.502 and J 0.557. Figures below this point in the section, and the
leave-one-out research tables, are mean-embedding results.

The paper's per-class calibrated thresholds were fitted to land-cover reversion
classes in a different reference frame and do not transfer. The threshold is
calibrated here on the surveyors' own labels by **Youden's J** (sensitivity +
specificity − 1) over the leave-one-out scores, and the achieved J is reported.
A J near 0 means the pools do not separate: report the continuous score and its
CI, and withhold the per-stand classification.

It is also fitted **leave-block-out**: one threshold per spatial block, fitted on
the labelled stands outside that block, because a threshold fitted to all the
labelled stands and then applied to those same stands is a fit reported as a
validation. Each stand is classified by the threshold that never saw its own
locality; the per-block values are in `threshold.json` and in every raster's
`threshold_global` band.

**What is still missing from it: its own error bar.** The stand interval is a
statement about the references; the threshold it is compared against is fitted
on 36 degraded stands in six localities and then treated as known. E39 measures
what that costs -- resampling the training blocks gives the threshold an
interval whose median width is **0.064**, against a median stand CI width of
**0.068**. The two uncertainties are the same size, and only one of them is in
the decision rule. See "E39" below before quoting a decisiveness figure.

### Is the ratio the right formula, and is 0–1 the right output? (E17/E18/E19)

The ratio was inherited, not chosen. E02 compared it against three hand-written
alternatives; E17 searches the space of closed forms instead of enumerating four
of them, with [PySR](https://github.com/MilesCranmer/PySR) (`research/sr.py`,
`exp.py E17`). Two things about how that search is set up decide what it can
find.

**It searches the combination, not the embedding.** Handing symbolic regression
64 AlphaEarth bands would return an expression over unnamed learned dimensions —
not interpretable, which is the only reason to prefer a formula in the first
place, and not supportable by 36 degraded stands. The features are the distance
geometry a probe already generates against its own pools: kNN means at k = 1, 5,
25, the spread of those neighbours and the median distance to the whole cloud, on
each side. Every one has a plain-language reading, so any equation over them has
one too.

**The label set is six localities.** The 36 `dårlig`/`sværtRedusert` stands sit in
**six** 5 km blocks and one of those holds fifteen of them, so leave-block-out CV
is leave-one-degraded-locality-out and every fitted candidate — symbolic
regression included — is fitting six localities. That is why a gradient-boosted
model is in the table: it is strictly more flexible than any expression PySR
returns at complexity 18, so if it cannot beat the unsupervised ratio out of
block, no closed form over the same features will either. It does not. It comes
last.

| candidate (6-fold leave-block-out) | pixel AUC | stand AUC | within-locality (pixel) |
|---|---:|---:|---:|
| `dor` — the shipped ratio | 0.703 | 0.710 | 0.688 |
| `m_bad − m_good` (E02's winner) | 0.723 | 0.738 | 0.703 |
| **`m_bad` alone** | 0.704 | **0.790** | 0.698 |
| `−m_good` alone | 0.616 | 0.501 | 0.626 |
| conformal typicality vs the reference cloud | 0.602 | 0.523 | 0.626 |
| logistic regression on the two distances | 0.679 | 0.726 | 0.704 |
| logistic regression on all ten features | 0.634 | 0.677 | 0.661 |
| PySR on the two distances | 0.671 | 0.703 | **0.707** |
| PySR on all ten features | 0.641 | 0.700 | 0.675 |
| gradient-boosted ceiling on all ten | 0.601 | 0.614 | 0.642 |

Every fitted candidate lands within ±0.02 of the hand-written ones, and E14's
seed spread on this AOI is ±0.01–0.02. **Nothing here is a difference.** What the
search returns is more interesting than the AUC it returns it with:

```
PySR, two distances     5 of 6 folds:  square(m_bad)
                        1 of 6 folds:  square(m_bad) + 0.72 − m_good
PySR, ten features      5 of 6 folds:  square(b25) or square(b5)
                        1 of 6 folds:  b5 − g1 + 0.48
```

Given both distances and told to fit the field labels, symbolic regression throws
the reference distance away and keeps a monotone function of the transformed one.
The Pareto front fitted on every labelled stand says the same thing more
precisely — `b1` at complexity 1, `square(b25)` at 2, and the first
reference-side term appears at **complexity 12**, as a multiplier. That is the
provenance check of E00 arriving from the other direction: the ratio's numerator
is doing the work, and the denominator is mostly along for the ride.

Symbolic regression also fails where E07's LDA and logistic regression failed, in
the same place: olivinskog. Pixel AUC there is 0.610 for PySR and 0.534 — chance —
for the boosted ceiling, against 0.669 for the unsupervised ratio. With 146
within-locality stand pairs on that forest type, anything fitted learns the two
localities it was given.

#### What the 0–1 number should be, if it stays 0–1

Ranking metrics cannot answer this. AUC is invariant to every monotone transform,
so `dor`, `log(m_bad/m_good)` and any rescaling of them are one candidate as far
as separation is concerned — E17 shows `dor` and `logratio` scoring identically to
three decimals, as they must. What separates bounded scores is whether the value
*means* something. The ratio's does not, quite: its midpoint is wherever the two
clouds happen to be equidistant, which moves with the transformed pool, and on
this AOI 98% of probe pixels fall between **0.451 and 0.726** — a quarter of the
nominal range carries everything.

E18 puts four alternatives that are also in [0, 1] against it, each with a
sentence attached, and E19 re-runs the unsupervised ones under five seeds.

| bounded score | stand AUC | within-locality (stand) | Spearman vs `tilstand` | Brier | ECE | p2–p98 range |
|---|---:|---:|---:|---:|---:|---:|
| `dor` — the shipped ratio | 0.717 ±0.010 | 0.629 ±0.012 | 0.154 | 0.236 | **0.057** | 0.271 |
| `axis01` — position on the naturalness axis | 0.643 ±0.004 | 0.540 ±0.007 | 0.113 | 0.237 | 0.065 | 0.435 |
| `unmix` — reference share of a 2-endmember mixture | 0.634 ±0.005 | 0.536 ±0.008 | 0.119 | 0.290 | 0.259 | 1.000 |
| conformal typicality `p_good` | 0.523 | 0.483 | −0.024 | 0.369 | 0.315 | 0.715 |
| **`P(good)` — isotonic calibration of `m_bad`** | **0.726** | **0.690** | **0.200** | **0.232** | 0.081 | 0.765 |

Four findings, in descending order of how much they should change anything.

**The ratio is nearly calibrated already, by accident.** Under a balanced prior —
equal weight to the two classes and to every stand within a class — `dor` has an
expected calibration error of 0.057, better than any fitted probability here. It
is not a probability by construction and nothing guarantees this holds on other
ground, but on this AOI the number a `dor` of 0.6 implies is close to the
frequency it corresponds to. That is the strongest argument for keeping it, and
it had not been checked.

**Calibrating `m_bad` beats the ratio on everything except calibration.** A
monotone isotonic map from `m_bad` to P(good), fitted leave-block-out under an
explicit 50:50 prior, gives the best stand AUC, the best within-locality stand
AUC (0.690 against 0.629 — this is the largest within-locality figure anywhere in
the ledger, E15's best pool included), the best rank correlation with the
four-level field class, and it spreads the four classes over 0.587 / 0.493 /
0.440 / 0.356 where the ratio compresses them into 0.572 / 0.571 / 0.557 / 0.500.
The cost is that it needs labels: 36 degraded stands in six blocks fit the map.

**Position along the naturalness axis wins at pixel grain and loses at stand
grain, which is a warning about pixel-grain within-locality AUC.** `axis01` — the
projection onto the reference-minus-transformed direction, rescaled so the
transformed centroid is 0 and the reference centroid is 1 — reaches 0.726 ±0.002
within a locality at *pixel* grain against the ratio's 0.707 ±0.018, and 0.540
against 0.629 at *stand* grain. Pooling pixels inside a block lets a score win by
separating pixels within a stand or by favouring stands with more of them; the
stand-grain figure is the one comparable to E15/E16 and the one to quote. Two
notes worth keeping: `axis01` is algebraically identical to fully constrained
(sum-to-one) linear unmixing, so the naturalness axis position and the mixing
fraction are one quantity, not two; and relaxing that constraint (`unmix`) leaves
the share undefined for 7.1% of `god` pixels against 2.9% of `dårlig` ones, an
asymmetric drop that flatters it.

**Conformal typicality does not work here, and the reason is informative.** The
leave-donor-out p-value against the reference cloud alone — "only 8% of surveyed
reference forest looks this unlike reference forest" — is the most defensible
sentence any of these scores can produce, needs no transformed pool at all, and
would have sidestepped everything E15/E16 could not settle. It ranks at 0.523 at
stand grain and orders the four field classes 0.140 / 0.162 / 0.152 / 0.161 —
non-monotone, i.e. not at all. A `dårlig` stand is not an *outlier* with respect
to surveyed forest; it is ordinary ground that happens to be in poor condition,
and one-class novelty detection cannot see that. The transformed end-member, for
all the trouble E15/E16 had finding a good one, is carrying the signal.

#### The percentile DoR: RECOVER's other scorer (E21)

`DegreeofRecovery/paper_release` ships **two** scorers, and only one of them
reached this pipeline. `src/degree_of_recovery/core.py` — the library, vendored
verbatim into `condition.core` — holds the kNN-margin ratio. A second lives in
`scripts/04_score_ecoregion_dor.py` and is not in the library at all:

```python
test_sim = cosine_similarity(x, refs).mean()            # to the WHOLE pool
base     = cosine_similarity(refs[500], refs).mean(1)   # each ref, likewise
pct_dor  = (pct_vs_good + (100 - pct_vs_bad)) / 2       # percentileofscore
```

Vendoring the library imported the ratio and silently dropped this, and E17/E18
never noticed: they searched the space of *combinations* of two kNN distances and
never asked whether the distances should be ranked within their own reference
distribution first. The motive is E18's own complaint — a percentile is uniform
on its range by construction, where the ratio squeezes 98% of probe pixels into
0.451–0.726 with a midpoint that moves with the transformed pool.

The port needs four decisions this AOI is not an ecoregion for: the ecoregion's
`ref_state = bad` sample becomes the transformed cloud pooled per (forest type,
year); step 04's self-inclusive baseline is run both as written and with the
donor stand (good side) or 500 m locality (transformed side) left out, since 60
near-duplicate pixels per donor are not 60 independent references; both the
faithful mean-to-the-whole-pool geometry and the pipeline's k=25 kNN are scored;
and `pct_vs_all` is not ported, because the two pools come from unrelated sources
and their union is a reference distribution of nothing. Stand AUC:

| config | `dor` | pct mean, self | pct mean, LOO | pct kNN, self | pct kNN, LOO |
|---|---:|---:|---:|---:|---:|
| global / block (E17–E19 protocol) | **0.710** | 0.542 | 0.536 | 0.619 | 0.558 |
| local / block | **0.759** | 0.732 | 0.615 | 0.692 | 0.691 |
| global / 3 km | **0.752** | 0.574 | 0.570 | 0.647 | 0.589 |
| local / 3 km (deployed) | **0.779** | 0.674 | 0.622 | 0.724 | 0.669 |

**It loses in all four, and the transformed side is why.** The percentile asks
where a probe falls among references *of that pool*, and forest pixels are so
much farther from cropland than cropland is from itself that `pct_vs_bad` is
pinned at the floor: its pixel median is 0.0 for `god` and 0.8 for `dårlig`, out
of 100. Half of step 04's score is therefore constant here, and `pct_dor`
degenerates to a monotone function of `pct_vs_good` — a one-class statistic,
which is exactly what conformal `p_good` already was, and it fails for the same
reason. A `dårlig` stand is not unusual *for surveyed forest*; it is ordinary
ground in poor condition.

**The self-inclusion bias is real, large, and harmless to the ranking.** The
compression it was predicted to cause is there — the kNN percentile's pixel
median is 1.6 self-inclusively against 24.8 leave-donor-out, i.e. a reference
pixel's nearest neighbours really are its own stand's siblings and every probe
looks atypical beside them. But removing it *lowers* AUC in all eight comparisons.
The bias is a near-monotone squeeze applied to every probe alike, so it costs
interpretability and nothing else. Worth knowing before anyone reads a percentile
off this kind of pool as if it meant a frequency.

The experiment's one durable result is the control it carried — see
[the pooled end-member above](#should-the-end-member-be-pooled-or-drawn-per-stand-e21e22).

`exp.py E18` and `E19` run in the `geo` env like everything else. `E17` is the
one experiment with an extra dependency — PySR, and through it a Julia toolchain,
which is why the search lives behind its own experiment id and `research/sr.py`
imports `pysr` inside the functions that need it rather than at module level:

```bash
~/.pixi/envs/geo/bin/python -m venv --system-site-packages ~/.venvs/sr
~/.venvs/sr/bin/python -m pip install pysr      # first import installs Julia
~/.venvs/sr/bin/python exp.py E17
```

**Nothing here is shipped.** The within-locality evidence is still four blocks and
1,243 stand pairs, and `m_bad` alone has a failure mode the labelled stands cannot
show: every labelled stand is forest, so nothing in this evaluation penalises a
score that calls anything far from converted land "good". The ratio's denominator
is what stops open water, an unmapped bog or a cloud artefact scoring well, which
is why `04_condition.py` writes `m_good` alongside `dor` in the first place. If
one thing here is worth acting on, it is reporting `P(good)` next to `dor` rather
than instead of it.

---
### Three ideas from CSIRO's HCAS, tested (E28–E31)

[ClassicHCAS](https://github.com/csiro/ClassicHCAS) is the R implementation of
the Habitat Condition Assessment System, CSIRO's national habitat-condition
method for Australia (HCAS 3.3). It is a **reference-only** method: it has no
bad end-member anywhere. It builds a two-dimensional surface from every *pair*
of reference sites — (distance in *predicted* space, distance in *observed*
space) — normalises each predicted-distance slice into a probability, and scores
a target by how probable its own observed departure is given how far the
reference sits from it. That is the design in which everything E15/E16/E21–E26
fought over never arises, so it is worth knowing whether it works here.

Three of its constructions port to this pipeline without new data, and all three
were tested at the deployed configuration (shrunk-whitened metric, k = 25,
3 km exclusion), five seeds for E28/E29, three for E30.

**None of them ships.** Two fail, one fails with a mechanism worth keeping, and
the fourth thing found — a by-product of the first — is the only result here
that changes what should be in the product.

#### The denominator is a better numerator (E28)

`variable_importance()` never uses an absolute departure. It reports a
signal-to-noise ratio: the weighted mean departure of the target from its
references, divided by the weighted mean distance among those references
*themselves* (a distance-weighted Gini mean difference). The denominator is a
local, per-probe estimate of how much intact forest varies round here anyway —
the same instinct as this project's shrunk whitening, which is the largest gain
in the ledger, but computed per probe instead of once per forest type. It is
also the right shape for the locality and `area_ha` confounds.

It does not work. `m_good` is the column to read — the reference side's AUC on
its own, which E20 puts at 0.654 global and 0.685 local:

| scope | variant | stand AUC | pixel AUC | **m_good alone** | Youden J |
|---|---|---:|---:|---:|---:|
| global | none (shipped) | 0.760 ±0.009 | 0.735 ±0.011 | **0.654** | 0.422 |
| global | snr good | 0.726 ±0.005 | 0.644 ±0.007 | 0.484 | 0.390 |
| global | snr both | 0.593 ±0.020 | 0.579 ±0.010 | 0.484 | 0.212 |
| local | none (shipped) | 0.789 ±0.004 | 0.769 ±0.006 | **0.685** | 0.488 |
| local | snr good | 0.699 ±0.011 | 0.581 ±0.010 | 0.455 | 0.337 |
| local | snr both | 0.582 ±0.012 | 0.538 ±0.010 | 0.455 | 0.224 |

No pixels are dropped (the finite share is identical across variants), so this is
not an artefact of a division blowing up. The mechanism is measured directly:

| scope | ρ(signal, noise) | AUC of signal | **AUC of noise** | AUC of the ratio | noise: god / dårlig |
|---|---:|---:|---:|---:|---|
| global | 0.742 | 0.641 | **0.637** | 0.469 | 0.490 / 0.568 |
| local | 0.427 | 0.684 | **0.694** | 0.433 | 0.397 / 0.503 |

A ratio only sharpens a contrast if its denominator is uncorrelated with its
numerator. Here they correlate at ρ = 0.74 (global), and — the part that decides
it — **the dispersion term separates the field labels on its own, in the same
direction as the departure does.** Degraded stands sit where the reference cloud
disagrees with itself. Dividing one class-separating quantity by another cancels
the signal and slightly inverts it: the ratio's AUC is 0.469 and 0.433, below
chance in both scopes.

**The by-product is what made E32 worth running.** On this single seed the noise
term alone reaches 0.694 against the departure's 0.684; over five seeds it is
0.680 against 0.685, so the honest statement is that **the dispersion of a
probe's own retained references is about as good a condition indicator as its
distance to them**, not better. It is free either way — computed from the same k
neighbours the score already selects, with no transformed pool involved — and the
question it raises is whether it carries information the departure does not.
E32 answers that.

#### The dispersion as a term rather than a denominator (E32)

E28 divided by the dispersion and scored below chance. E32 adds it instead, in
three unsupervised forms — `noise` alone, `sum` = departure + dispersion, and
`geom` = √(departure × dispersion) — because E07 and E17 both established that
anything *fitted* on 36 degraded stands in six blocks collapses on olivinskog.

**On the reference side alone, the combination genuinely adds information.**
Pixel AUC of the reference term by itself, five seeds, `local`:

| reference term | pixel AUC | olivinskog pixel |
|---|---:|---:|
| `m_good` (shipped) | 0.685 ±0.005 | 0.715 |
| `noise` alone | 0.680 ±0.011 | 0.703 |
| `sum` | **0.717 ±0.009** | **0.751** |
| `geom` | **0.719 ±0.010** | **0.753** |

+0.034 over either component, on seed sds of 0.005–0.011, and +0.038 on
olivinskog — the forest type that defeats every fitted method in the ledger. So
the dispersion and the departure are not redundant; they are two partly
independent views of the reference side.

**It does not survive into the score.** Substituted into the ratio at `local`:

| reference term | stand AUC | pixel AUC | within-locality (stand) | Youden J |
|---|---:|---:|---:|---:|
| `m_good` (shipped) | **0.789 ±0.004** | 0.769 ±0.006 | **0.635 ±0.002** | **0.488** |
| `noise` | 0.723 ±0.010 | 0.735 ±0.009 | 0.568 ±0.009 | 0.376 |
| `sum` | 0.771 ±0.007 | **0.773 ±0.008** | 0.608 ±0.007 | 0.427 |
| `geom` | 0.765 ±0.007 | 0.771 ±0.009 | 0.602 ±0.006 | 0.428 |

The ratio buys +0.004 of pixel AUC and pays 0.018 of stand AUC, 0.027 of
within-locality AUC and 0.061 of Youden J. That is the same shape as E29's
result arriving from the other direction: a change that improves one grain's
ranking while making the verdicts worse.

**And the diagnostic the experiment was built around returns a flat no.** The
worry was that the dispersion is really a property of the *locality* — at `local`
the pool is the eight nearest donors, so how much they disagree is largely a fact
about where the stand is — which would show up as pooled AUC without
within-locality AUC. Measured at stand grain, every reference term on its own
sits at 0.445–0.511 within a block, i.e. **at or below chance in all four forms**.
The reference side of this score carries no within-locality signal in any
formulation tried, dispersion included. That is not a new fact — the ledger has
said since E00 that `m_bad` carries the separation — but it is now measured for
the dispersion too, and it is why the +0.034 on the reference side does not
become +0.034 anywhere that matters.

**Verdict: report it, do not score with it.** `noise` is now a column on the
engine's per-pixel output and belongs in the per-pixel product as a band and in
the confound controls next to `area_ha` and gHM's `AA`. It does not belong in the
numerator or the denominator of the condition score.

#### Weighting the neighbours moves the ranking, not the decision (E29/E31)

The shipped score is an unweighted mean of the k nearest references. HCAS
weights them by a distance kernel — Gaussian `exp(−(d/λ)²)` or Cauchy
`1/(1+(d/λ)²)` — and its 2.0.0 default multiplies the single best-matching
reference's weight by `boost = k2`. `boost` is therefore a continuous family with
the flat k-mean at 1.0 and the nearest neighbour alone at the limit: it claims
the sharpness of a small k while still averaging over 25 references. That is
attractive here because k was raised 5 → 25 for interval width, not for
separation, and the cost of that trade was never tested against anything but
another flat mean. λ is relative (a multiple of each probe's own median retained
distance), because the two forest types sit at different distance scales in the
whitened space.

At the deployed `local` scope, five seeds:

| variant | stand AUC | pixel AUC | **within-locality (stand)** | **Youden J** |
|---|---:|---:|---:|---:|
| flat k=25 (shipped) | 0.789 ±0.004 | 0.769 ±0.006 | 0.635 ±0.002 | 0.488 |
| boost 5 | 0.791 ±0.003 | 0.771 ±0.006 | 0.636 ±0.002 | 0.500 |
| boost 10 | 0.793 ±0.003 | 0.771 ±0.005 | 0.639 ±0.006 | 0.507 |
| boost 25 | 0.791 ±0.005 | 0.771 ±0.006 | 0.644 ±0.009 | 0.512 |
| cauchy λ=1 + boost 25 | 0.790 ±0.005 | 0.770 ±0.006 | **0.648 ±0.008** | **0.512** |
| flat k=5 | 0.785 ±0.003 | 0.772 ±0.006 | 0.639 ±0.007 | 0.501 |
| flat k=1 | 0.783 ±0.008 | 0.765 ±0.009 | **0.660 ±0.017** | 0.493 |

Pooled AUC does not move at all — 0.789 to 0.791, on a seed sd of 0.003–0.005 —
which is E05's finding ("every knob moves the result by less than the bootstrap
interval") arriving from a new direction. But **within-locality stand AUC and
Youden J both rise monotonically as weight concentrates on the best match**,
+0.013 and +0.024 from the shipped estimator to `cauchy + boost 25`, on seed sds
of 0.002–0.008. E15/E16 argue the within-locality number is the one that answers
a manager's question, so on the stated metric this is a real if small gain. The
`global` scope agrees: within-locality 0.672 → 0.687 at boost 25.

**It does not survive into the decision path.** E31 runs the full path —
donor-clustered interval, leave-block-out Youden threshold, the
CI-vs-threshold rule — for four estimators. (The shipped row reproduces E27's
independently, to four decimals.)

| estimator | median CI width | indistinguishable | labelled stands decided | **accuracy when decided** | class changes |
|---|---:|---:|---:|---:|---:|
| flat k=25 (shipped) | **0.071** | 43.6% | 155 | **0.723** | — |
| flat k=5 | 0.084 | 45.3% | 150 | 0.700 | 41 (10.1%) |
| boost 25 | 0.075 | 42.3% | 157 | 0.688 | 33 (8.2%) |
| gaussian λ=0.5 | 0.077 | 44.8% | 151 | 0.715 | 29 (7.2%) |

`boost 25` decides two more labelled stands than the shipped estimator and is
3.5 points *less* accurate when it does, on a wider interval. By the currency
E09/E16 established — an estimator that classifies more stands has only improved
if it is no less accurate when it does — it fails. The half of the boost
hypothesis that was right is that it costs much less interval than `k = 5` does
(0.075 against 0.084, and 33 class changes against 41); the half that was wrong
is that the AUC it buys is not the kind that reaches a verdict.

Two things follow. `boost` is not a free lunch and is not shipped. And the
within-locality/Youden gains in E29 are a warning about those two metrics: they
moved by four seed sds while the decisions they are supposed to summarise got
worse.

#### The reference density, and why 64 dimensions defeat it (E30)

This is HCAS's thesis rather than its tactics: score condition with **no
transformed pool at all**. The one thing that does not transfer is the
*predicted* channel — in HCAS it is the RS signal a site would show under
reference condition, modelled from environmental covariates, which this project
does not have. Two substitutes were built, and both are constructions of this
project, not of HCAS:

- **`geo`** — predicted separation is geographic distance, so the surface is a
  dissimilarity-decay curve: "two intact stands 40 km apart normally differ this
  much". HCAS reaches for the same lever with `xy_penalty`.
- **`ambient`** — predicted separation is distance in the top-*m* eigen-directions
  of the unlabelled covariance (which this document already identifies as
  terrain, phenology and acquisition), observed separation is the rest.

The surface is rebuilt per forest type with the probe's whole 5 km block held
out, from ~400,000 donor pairs, same-donor pairs excluded (HCAS's zeroing of the
origin cell). `flat` is the control: identical two-stage selection and kernel,
density replaced by the plain observed distance, so what `density` gains over it
is the conditioning and nothing else.

| k1/k2 | variant | stand AUC | pixel AUC | within-locality | Youden J |
|---|---|---:|---:|---:|---:|
| 70/10 | flat / geo (control) | 0.542 ±0.005 | 0.658 ±0.006 | 0.432 | 0.188 |
| 70/10 | density / geo | 0.490 ±0.026 | 0.374 ±0.035 | 0.541 | 0.115 |
| 70/10 | flat / ambient m=8 (control) | 0.593 ±0.003 | 0.647 ±0.008 | 0.526 | 0.186 |
| 70/10 | density / ambient m=8 | 0.549 ±0.015 | 0.479 ±0.008 | 0.614 | 0.132 |
| 200/25 | density / ambient m=16 | 0.599 ±0.007 | 0.487 ±0.001 | 0.651 | 0.240 |
| 200/25 | **tail / ambient m=8** | **0.685 ±0.006** | **0.667 ±0.005** | 0.606 | **0.361** |
| 200/25 | tail / geo | 0.623 ±0.009 | 0.653 ±0.007 | 0.460 | 0.204 |

**The density is worse than its own flat control everywhere**, and `density/geo`
is below chance at stand grain. The reason is dimensional, and it is measured
rather than argued:

| forest type | surface mode (d_obs) | median d_obs: god | median d_obs: dårlig | 5th pct: god / dårlig |
|---|---:|---:|---:|---|
| kalklindeskog | 1.039 | 0.961 | 0.942 | 0.619 / 0.619 |
| olivinskog | 0.992 | 0.857 | 0.919 | 0.494 / 0.627 |

In 64 dimensions the pair-distance distribution is sharply unimodal at the
typical inter-reference distance, and **both classes sit on that mode** — for
kalklindeskog the degraded median (0.942) is *nearer* the mode than the intact
one (0.961), which is exactly the inversion the AUC reports. A density value
reads the bulk of the distribution. All of the condition signal is in the **left
tail**, which is precisely what a k-nearest mean reads and a density value does
not. HCAS does not have this problem because it operates on a handful of
centred, scaled RS summaries, where the bulk of the pair distribution is
informative.

`tail` is the one-line repair: same surface, same conditioning, but the
conditional survival function P(departure ≥ d | separation) instead of the
density — monotone in the departure rather than unimodal. It recovers most of
what the density loses (`ambient m=8`: 0.549 → 0.669 at 70/10, 0.577 → 0.685 at
200/25) and it is **the best reference-only score measured in this project**,
well clear of E18's conformal typicality at 0.523. So the conditioning does buy
something over plain one-class typicality.

It is still not enough. At 0.685 stand and 0.667 pixel it lands in the same band
as the shipped score's *reference side alone* (`m_good`, 0.654–0.685) and well
below the shipped ratio's 0.760/0.735. **E18's verdict stands, on a strictly
stronger test**: a degraded stand is not atypical of surveyed forest, even after
the expected departure is conditioned on how far the reference sits. The
transformed end-member — for all the trouble E15/E16/E21–E26 had finding a good
one — is still what carries the signal, and a reference-only method cannot
replace it on this AOI.

`condition/research/hcas.py` holds the port; nothing in `condition/src` changed.
### Supervised models on the embedding, and what a spatial fold is worth (E33–E35)

E07 fitted two high-bias linear models — shrunk LDA and L2 logistic — to the
field labels on the 64-d embedding under leave-one-block-out, got stand AUCs of
0.41–0.58 against the unsupervised ratio's 0.760, and olivinskog at 0.09–0.17,
i.e. ordered almost perfectly backwards. That is recorded above as "both collapse
on olivinskog". Two objections to reading it as a verdict on supervision in
general. A linear model failing says nothing about a model that can bend. And
E07 read only the **pooled** AUC, which under this protocol is not obviously a
valid quantity: every held-out block is scored by a *different* model, and two
models' decision functions share no scale or offset, so a pooled AUC compares
stand A's score from model 1 against stand B's score from model 2.

Three experiments close both gaps. The first is the one with consequences
outside this project.

#### What a spatial fold is worth (E34)

This document has asserted since the beginning that an ungrouped fit on
near-duplicate pixels "would report an AUC near 1 and predict nothing". Every
grouped number in the ledger rests on that belief and it had never been measured.
Three rungs, identical in every respect — same model, same capped labelled-pixel
set, same out-of-fold evaluation — differing only in what the fold boundary
respects:

| fold grouping | logreg C=1 | random forest | RBF SVM |
|---|---:|---:|---:|
| ungrouped pixels | 0.984 ±0.001 | 0.999 ±0.000 | **1.000 ±0.000** |
| leave-stand-out (no spatial buffer) | 0.846 ±0.003 | 0.858 ±0.002 | 0.877 ±0.003 |
| **leave-5 km-block-out (this project)** | **0.400 ±0.010** | **0.320 ±0.003** | **0.455 ±0.006** |

An RBF SVM reports a **perfect 1.000** on ungrouped pixel k-fold and **0.455** —
below chance — once the fold respects the locality. The assertion was right, and
it was if anything understated.

The middle rung is the one worth carrying outside this AOI. Leave-one-stand-out
CV — grouping by the mapped polygon, which most published work would call
rigorous — still reports 0.846–0.877 where the honest number is 0.320–0.455. The
extra 0.4 comes from *neighbouring* stands: the six layers map the same
localities repeatedly, so a stand's neighbour is very nearly the stand. Grouping
by the analysis unit is not enough; the grouping has to be spatial, and it has to
be at the scale the autocorrelation actually runs at. That is what the 5 km
blocks and the 3 km pool exclusion are for, and this is the measurement that
justifies their cost.

Blocked, the failure is not uniform across the two forest types: kalklindeskog
holds up at 0.633–0.751 while olivinskog inverts to 0.136–0.317. E07's olivinskog
finding survives every model class tried.

#### The zoo (E33)

Nine estimators — from strongly regularised linear through RBF SVM, random
forest, gradient boosting and a small MLP — on the raw and the shrunk-whitened
embedding, leave-one-block-out, three seeds:

| representation | model | stand (pooled) | olivinskog (pooled) | within-locality (stand) | within-locality (pixel) |
|---|---|---:|---:|---:|---:|
| none | logreg C=1 | 0.584 ±0.008 | 0.187 | 0.811 ±0.003 | 0.509 |
| none | linear svm | 0.628 ±0.005 | 0.214 | 0.808 ±0.001 | 0.541 |
| none | rbf svm | 0.576 ±0.009 | 0.230 | 0.808 ±0.004 | 0.496 |
| none | random forest | 0.542 ±0.004 | 0.176 | 0.716 ±0.005 | 0.544 |
| none | hist gbm | 0.573 ±0.003 | 0.192 | 0.717 ±0.004 | 0.480 |
| none | mlp (32,) | 0.545 ±0.008 | 0.188 | **0.816 ±0.003** | 0.533 |
| none | knn k=25 | 0.552 ±0.000 | 0.410 | 0.617 ±0.000 | 0.484 |
| whiten | logreg C=0.01 | 0.542 ±0.004 | 0.105 | 0.797 ±0.001 | 0.491 |
| whiten | rbf svm | 0.579 ±0.005 | 0.341 | 0.763 ±0.000 | 0.558 |
| | *unsupervised ratio* | *0.789* | *0.809* | *0.635* | *0.769 (pooled)* |

Three things. **Flexibility does not help**: random forest and gradient boosting
land *below* the linear models on within-locality AUC, which is the signature of
fitting localities rather than condition. **Whitening does not help** either — it
is slightly worse than the raw embedding for most models, where for the
unsupervised score it is the largest single gain in the ledger. And the two
columns disagree violently: pooled AUC says every model fails, within-locality
AUC says most of them beat the shipped score's 0.635. Note also that
within-locality *pixel* AUC sits at chance throughout, so whatever separates
survives only in the stand mean — and this document already warns that stand area
predicts the field label almost as well as the embedding does.

The within-locality column rests on the 4 of 66 blocks that hold both a `god` and
a `dårlig` stand. It is not enough on its own to overturn E07.

#### Was the pooled number ever a measurement? (E35)

The discriminating test needs no new labels. Under leave-one-block-out every
stand in a block — `god`, `moderat`, unlabelled, all of them — is scored by that
block's own model, so ranking each stand against its own block's whole population
puts every fold on one [0, 1] scale using no label information. Pooled AUC over
those percentiles is a valid quantity where the raw pooled AUC is not.

**And the control matters as much as the test.** Fold-normalising is itself a
within-block correction that discards between-locality information, so comparing
a fold-normalised supervised number against a raw unsupervised one would be
rigged. The unsupervised score gets the same treatment on the same 247 stands
(39 fall in blocks too small to support a percentile and are dropped from both):

| model | pooled raw | pooled fold-normalised | olivinskog raw | olivinskog fold-norm |
|---|---:|---:|---:|---:|
| logreg C=1 | 0.530 ±0.008 | 0.695 ±0.003 | 0.101 | 0.394 |
| rbf svm | 0.579 ±0.005 | 0.699 ±0.002 | 0.341 | 0.550 |
| random forest | 0.443 ±0.007 | 0.686 ±0.011 | 0.153 | 0.524 |
| hist gbm | 0.469 ±0.008 | 0.695 ±0.014 | 0.178 | 0.559 |
| **unsupervised `dor`, global** | *0.753* | **0.716** | *0.714* | **0.716** |
| **unsupervised `dor`, local** | *0.788* | **0.704** | *0.809* | **0.703** |

**E07's headline was substantially a measurement artefact.** Every model family
lands at 0.686–0.699 once the folds are made comparable, not at the 0.44–0.58 the
raw pooled number reports. Supervised models on the embedding do separate the
field labels; they were never being read correctly.

**And they still lose.** On the one footing where the two families are
comparable, the unsupervised ratio is at 0.704–0.716 against the best supervised
0.699 — ahead, though by much less than the raw comparison suggested. On
olivinskog the gap is not small at all: 0.703–0.716 against 0.394–0.559, so
E07's olivinskog verdict stands even after the correction.

**The asset the table does not show is the one that decides it.**
Fold-normalising *costs* the unsupervised score — 0.788 → 0.704 at `local` — 
because it throws away real between-locality information that the ratio carries
and a per-fold supervised model structurally cannot. The ratio is one function of
the embedding everywhere; only its threshold is fitted per block. A supervised
model under honest spatial CV is a different function in every locality, which
means it cannot produce a map whose values mean the same thing in two places.
For a product whose whole purpose is to rank 737 stands across Norway on one
scale, that is not a tuning detail — it is the requirement.

So the corrected verdict, replacing E07's: **supervised models on the embedding
are not useless and are not better. They are roughly as separating as the
unsupervised ratio within a locality, worse on olivinskog, worse overall on the
only common footing, and structurally unable to deliver a comparable score across
localities — while being the only approach here whose apparent skill collapses by
0.5 of AUC when the fold boundary is drawn honestly.**

`condition/research/supemb.py` holds the zoo and the ladder; nothing in
`condition/src` changed.

## 3. Validation

The tables in checks 1 and 2 are the current run (median stand score, 2026-10-23).
Checks 3 and following keep the 9 September values where marked.

`scripts/05_validate.py` runs six checks the RECOVER paper could not. All are
evaluated at `label_age == 0` — each labelled stand in the year it was actually
surveyed — because scoring a 2017 `god` against a 2025 embedding would test what
happened in between, not the method.

1. **Ordering** — median DoR should fall `god > moderat > dårlig >
   sværtRedusert`. `moderat` is in no pool, so its position between the anchors
   is genuinely out of sample. **Both scopes pass**, and the held-out `moderat`
   lands between the anchors in both:

   | scope | god | moderat | dårlig | sværtRedusert |
   |---|---:|---:|---:|---:|
   | local | 0.556 (238) | 0.525 (131) | 0.482 (20) | 0.431 (15) |
   | global | 0.562 (250) | 0.543 (135) | 0.511 (20) | 0.463 (16) |

   The absolute values are lower than the pre-whitening ones because the metric
   changed; only the ordering is meaningful, and it holds in both scopes.

2. **Separation** — AUC of the leave-one-out DoR between labelled good and bad,
   at **both grains and per forest type**, because the pooled figure hides the
   real story:

   | grain | scope | kalklindeskog | olivinskog | both |
   |---|---|---:|---:|---:|
   | stand | local | 0.859 [0.71, 0.94] | 0.772 [0.61, 0.88] | **0.836** [0.73, 0.90] |
   | stand | global | 0.820 [0.67, 0.97] | 0.727 [0.42, 0.81] | 0.779 [0.68, 0.89] |
   | pixel | local | 0.864 [0.71, 0.93] | 0.754 [0.50, 0.84] | 0.775 [0.58, 0.88] |
   | pixel | global | 0.851 [0.73, 0.94] | 0.734 [0.50, 0.84] | 0.768 [0.59, 0.89] |

   Intervals bootstrap whole spatial blocks: 87k pixels from 286 stands in 66
   localities are not 87k independent observations, and a row-wise interval
   would understate them by roughly the square root of that ratio. They are
   wide, and that width is the honest precision of every comparison here —
   **differences under about 0.04 are ties.**

   The stand-level change from the mean-embedding score is small: `local` stand
   AUC 0.836 against 0.829 (difference −0.008, 95 % CI −0.027 to 0.012). The
   paragraph below describes the 9 September run (mean-embedding score).

   **Olivinskog is no longer the failure case, and `local` is no longer the
   weaker scope.** Under the published metric olivinskog scored 0.563 at pixel
   grain and 0.456 at `local`, i.e. worse than chance; the whitened metric took
   it to 0.668, and replacing the block exclusion with a 3 km radius takes it to
   0.756 — above kalklindeskog at that scope. The remaining caveat is sample
   size, not method: twelve degraded olivinskog stands in eight blocks, and an
   interval of [0.54, 0.82]. A figure that moves this much on a configuration
   change should be held loosely.
3. **What the ratio is made of** *(9 September run, mean-embedding score)* — `dor = m_bad / (m_good + m_bad)` can rise
   because a pixel moved toward the good cloud *or* because it is far from the
   transformed one, and the transformed distance is still the larger of the two
   (median `m_bad` 0.772 against `m_good_global` 0.545). What changed with the
   3 km pool radius is which term carries the *signal*:

   | component alone, pixel grain | block rule | 3 km radius |
   |---|---:|---:|
   | `m_good_local` | 0.445 *(backwards)* | **0.683** |
   | `m_good_global` | 0.574 | 0.640 |
   | `m_bad` | 0.607 | 0.703 |
   | `dor_local` | 0.512 | **0.764** |
   | `dor_global` | 0.617 | 0.721 |

   The reference side now carries real signal and the full ratio beats either
   component in both scopes. It is close at `global` (0.721 against `m_bad`'s
   0.703), so step 5 still prints the per-component AUCs and warns when the
   transformed term comes within 0.02 of the *best* scope's ratio. **Read
   `m_good_*` alongside the score, never the score alone.**
4. **Trajectory against Hansen loss year** (independent of AlphaEarth). On 2,258
   pixels Hansen says were cleared 2018-2024 and 72,090 in stands with no recorded
   loss, `declining` recovers 0.326 at a 2.0 % false-positive rate; the slope AUC
   is 0.733. The axis input scored 0.52 recall and AUC 0.81 at cell grain, and at
   stand grain the AE-score slope AUC is 0.64 against 0.75 (difference 0.11, 95 %
   CI 0.02 to 0.19). Hansen sees canopy removal only, so this is a sensitivity
   floor.

5. **Coherence** — `declining` stands should score lower than `improving` ones.
   The trajectory now runs on the same AE score as the condition class, so the
   two are not independent: agreement is partly by construction, and the useful
   output is the disagreement. In the current run `declining` stands have median
   `dor_local` 0.424 and `improving` 0.553. Disagreement is informative: it
   isolates stands in poor condition but recovering, and — the management
   priority — stands still in good condition but actively losing it.

6. **Within a locality, or across them?** *(9 September run, mean-embedding score)* Every AUC above pools stands from
   different 5 km blocks, which lets a score win by telling localities apart. The
   question a manager asks is within one: *of the stands on this hillside, which
   are degraded?* Computed inside each block and weighted by its good-bad pairs,
   the shipped configuration scores **0.561**, against 0.752 pooled -- and stand
   area alone scores **0.747** on the same comparison. Holding both locality and
   area tertile fixed leaves 0.535-0.572, near chance.

   This is a four-locality measurement and it must be read as one: only 4 of 66
   blocks hold both a `god` and a `dårlig` label, block 23 carries 93 % of the
   kalklindeskog evidence and block 51 carries 89 % of the olivinskog. Weight the
   four equally instead of by pair count and the shipped pool scores 0.691 against
   the leaders' 0.708-0.717, a much smaller gap. The robust statement is the
   narrow one: **in the largest locality the shipped configuration is at chance
   (0.513) and every alternative end-member beats it** (0.572-0.610).

A final block lists stands that class as good overall yet have more than 30 % of
their pixels below the threshold: part-degraded stands that a single stand score (mean or median) can hide, and
the concrete reason the probe is a pixel.

## Known limitations

- **The bad pool is a very distant end-member.** Cropland and built-up sit far
  enough from any forest in embedding space that `m_bad` is still the larger
  distance, and at the `global` scope it is nearly as separating as the whole
  ratio (check 3 quantifies it). Note also that the E15/E16 comparison of
  alternative end-members was run against the *previous* pool rule and has not
  been re-run since; the radius is exactly the change that most improves
  within-locality separation, so those rankings may no longer hold. This is no longer an open question: substituting the field `tilstand` bad
  labels was tested and costs almost all the separation, and mixing them in is
  worse than WorldCover alone. The distant end-member stays, and the whitened
  metric is what recovered the good side's contribution.
- **Stand area predicts the field label almost as well as the embedding does**
  on kalklindeskog (0.736 against 0.732 for the shipped score). The separation
  survives stratification by area, but not by a wide margin, and 428 of 737
  stands are too small to have a single pixel 20 m from their own boundary. The
  per-pixel product carries an `edge_m` band for this reason.
- **`local` costs coverage, for two separate reasons.** The mapping-density
  confound that used to make `m_good_local` separate *backwards* (AUC 0.445) was
  an artefact of the pool rule, not of the scope: with a 3 km radius it is 0.683
  and `local` is the better-separating scope at both grains. What remains is
  availability. 33 of 737 stands carry a `global` score only: **26** have no
  eligible donor of their type inside 50 km once the radius has excluded the near
  ones (`n_good_local == 0`), and **7** have exactly one donor contributing 12-14
  pixels, which is fewer than `k = 25`, so `score.dor_scores` returns NaN rather
  than averaging a "25 nearest" over a 14-pixel pool that is one donor seen
  fourteen times -- the failure mode k was raised to avoid in the first place.
  All seven would score at `k = 5`. The global pool never hits either case; its
  smallest is 1,498 pixels. Both reasons are recoverable from `n_good_local`.
  Report both scopes; they answer different questions.
- **Olivinskog rests on twelve degraded stands.** Its per-pixel AUC is now 0.756
  at `local` and 0.689 at `global` — a usable score rather than the sub-chance
  one the published metric gave — but the positive class is twelve stands in
  eight spatial blocks, and the block-bootstrap interval [0.54, 0.82] is the
  honest precision of that.
- **Small stands.** Median kalklindeskog stand is 0.38 ha ≈ 38 pixels, and five
  polygons cover fewer than five. Stands below a pixel fall back to their
  centroid pixel and are flagged by `n_pixels`.
- **Nine annual observations** is a short series for a trend test.
- **The reference pools are geographically clustered.** This is now handled
  rather than noted: pools and thresholds are held out by 5 km spatial block and
  every interval bootstraps blocks. What remains is that the *axis* is still
  fitted once per forest type from all donors; refitting it per block moves the
  Hansen validation figures by less than their interval, so it is not done in
  production, but it is why the axis check in `condition/research/` uses a
  block-left-out axis.
- **WorldCover is a single 2021 epoch** applied to all nine years. Cropland and
  built-up are stable enough for that to be reasonable, but a parcel converted
  in 2023 is treated as transformed in 2017 too.
- **No independent validation set for condition.** `moderat` is held out of the
  pools, but it is the same survey campaign and the same labelling protocol. The
  *trajectory* step now has one — Hansen loss year — and the condition step does
  not.
- **A quarter of stands cannot be classified**, and under an interval that
  resamples donor stands that is the correct answer rather than a defect. Report
  the continuous score and its interval for those.

---

## Where these numbers come from

Every choice above was made by an experiment recorded in
`condition/outputs/research/ledger.jsonl`, run by `condition/research/exp.py`
and written up as a report by `condition/research/report.py`.

### The four axes an experiment is scored on

Until 2026-09-04 the ledger ranked configurations on one number, the good/dårlig
AUC, and three things that decide whether a change is worth shipping were
invisible to it. `condition/research/metrics.py` now computes all four for every
run, and `condition/research/dashboard.py` flattens the ledger onto them and
builds the tracker page (`outputs/research/tracker.html`):

| axis | what it is | direction | where |
|---|---|---|---|
| **separability** | stand / pixel / within-locality AUC, good vs dårlig | higher | `auc_stand`, `auc_within_block` |
| **width** | the 95% block-bootstrap interval on that AUC, and the median per-stand CI width where a run paid for one | **lower** | `auc_stand.both.width`, `ci_width_median` |
| **indistinguishable** | share of stands the CI-vs-threshold rule refuses to call, and the free proxy — the share sitting in the good/bad overlap band | **lower** | `frac_indistinguishable`, `ambiguity.frac_in_band` |
| **four classes** | the six pairwise `tilstand` AUCs, their macro mean, and the **worst adjacent pair** | higher | `four_class.ordinal_auc`, `four_class.min_adjacent_auc` |

Three notes on why they are computed the way they are. The four-class summary
macro-averages the six pairs rather than pooling them, because pooling lets
god vs dårlig (250 × 20 stands) drown out dårlig vs sværtRedusert (20 × 16), and
the small pairs are the ones nobody optimised for. `min_adjacent_auc` is the
bottleneck boundary — a four-class map is only as good as its weakest one, and
on the baseline that is `moderat | dårlig` at 0.56 while the headline AUC reads
0.76. And the abstention count needs `engine.stand_intervals`, which costs a
600-resample bootstrap per stand, so the overlap band is carried alongside as a
zero-cost proxy that moves with it.

The trajectory experiments (T00–T05) score a different label task, so their AUCs
are not on this scale and the tracker keeps them out of every cross-experiment
ranking.

### Normalised axes, and the hypervolume that ranks them (`hv.py`)

Four axes cannot be traded against one another while two are gains, one is a
cost and each has its own floor, so `condition/research/hv.py` rescales each to
`0–1` with **fixed anchors — never the ledger's own min and max**, because a
score that moves when tomorrow's experiment adds a row is not a measurement:

| family | normaliser | 1.0 means |
|---|---|---|
| AUC-like (`auc_*`, `ordinal_auc`, `min_adjacent_auc`, accuracy) | `2(x − ½)` | perfect ordering; chance is 0 |
| interval widths | `1 − x` | the quantity is known exactly |
| shares (`frac_in_band`, `frac_indistinguishable`, overlap) | `1 − x` | no stand is ambiguous |
| Youden J, rank correlation | identity, clipped at 0 | — |

**Hypervolume** is the volume of that unit box a variant dominates, measured
from the origin — the corner where the score is at chance, its interval spans
the whole range and every stand is ambiguous. For a single variant it is the
product of its normalised axes, so a variant at chance on any one axis scores
zero; for a set it is the volume of the union, which is how much of the
achievable space a whole experiment reached. `hypervolume()` is the WFG
recursion, checked against a 400k-sample Monte Carlo estimate to ±0.001.

Default objectives: stand AUC, its interval width, the overlap-band share, and
the worst adjacent class pair. `ordinal_auc` is deliberately **not** in the
default set — it correlates +0.95 with stand AUC over this ledger, so including
both would weight separability twice and call it a trade-off.

Three groups of rows are kept out of the ranking (they stay browsable): E08
rebuilds the spatial blocks, so it would win the width axis by changing the
ruler rather than the method; E34's fold groupings leak by design; E35 scores
fold-normalised ranks. Variants missing a selected axis are dropped rather than
imputed.

### E36 -- the decisiveness axis, measured (and what that did to the ranking)

The first leaderboard used the score-axis overlap band as a free stand-in for
abstention. **It is not one.** E36 ran both on the same stands for the 22 leading
configurations, three seeds each: the band and the measured
`frac_indistinguishable` correlate at **rho = -0.49** -- the wrong sign -- and the
two rankings they produce agree at **rho = +0.13**. The mechanism is structural:
a verdict abstains when a stand's donor-clustered interval straddles the
threshold, and that interval is set by how many donor stands the pool holds,
while the band is set by how far apart the two labelled pools sit on the axis.
`global` pools 2,000 donors and so decides more stands; `local` spreads the
score further and so has a narrower band. The two axes move in opposite
directions for the same reason.

So the abstention objective is the measured one, at ~10 s of donor bootstrap per
configuration, and the leaderboard reads:

| HV | configuration | stand AUC | width | decided | worst adj. |
|---|---|---|---|---|---|
| **0.0546 ± 0.0024** | E29 `global` / cauchy l=1 | 0.754 | 0.254 | 77% | 0.594 |
| 0.0524 ± 0.0021 | E29 `global` / cauchy l=0.5 | 0.755 | 0.252 | 72% | 0.596 |
| 0.0518 ± 0.0023 | E29 `global` / gaussian l=1 | 0.756 | 0.251 | 71% | 0.595 |
| 0.0512 ± 0.0053 | E21 `local` / 3 km / pooled bad | 0.804 | 0.193 | 62% | 0.585 |
| 0.0486 ± 0.0122 | E04 `local` / whiten | 0.763 | 0.234 | 59% | 0.600 |
| 0.0419 ± 0.0022 | **the deployed configuration** | 0.788 | 0.260 | 56% | 0.588 |

Two things to carry away. **AUC and the decision rule disagree about scope.**
`local` has the higher stand AUC (0.788 against 0.754) and the shipped
configuration is built on it, yet `global` with a soft kernel decides 77% of
stands against `local`'s 56% *and* is right more often when it does (86% against
69%). Deciding more stands normally costs accuracy, because the extra stands are
the hard ones; here it does not, and that is the strongest single result the
four-axis view has produced.

**And the leaderboard is not resolved at the top.** The median seed spread on the
hypervolume is 0.0022 and the leader is 1.0 sd clear of second (these are
the mean of the per-seed hypervolumes; the tracker page recomputes it from the
seed-mean metrics, so its column differs in the fourth decimal) -- though second
and third are the same kernel family, so the *family* is ahead of everything
else by several sds. Quote the family, not the row.

### E38 -- scope x weighting, with the control the leaderboard never had

E36's leader was `global` with a Cauchy kernel and its rival was `local`, and the
table could not say which half of the winner did the work: **there was no
unweighted `global` row and no weighted `local` row**, and the two families are
not even scored on the same stands -- `local` can only ever reach **404** of the
421 labelled stands, because 17 have no eligible donor inside 50 km once the 3 km
exclusion is applied. Part of what read as `global` deciding more stands was a
larger denominator.

E38 runs all four cells of `scope` x `weight`, everything else pinned
(shrunk-whitened, k = 25, 3 km exclusion), three seeds each, every metric
computed on the **intersection** of the stands all four cells can score with the
leave-block-out threshold refitted on that intersection. And it replaces the
accuracy figure: on 250 good and 36 degraded stands, `accuracy_when_decided`
rewards a rule that calls the good ones and abstains on the rest, so the cells
are ranked on degraded recall, poor-call precision and the share of degraded
stands reported as *good*, none of which can be moved that way.

| cell (per-stand bad pool) | stand AUC | decided | acc when decided | **degraded recall** | poor precision | **degraded called good** | balanced acc |
|---|---|---|---|---|---|---|---|
| `local` / flat | 0.788 | 0.558 | 0.693 | **0.533** | 0.307 | **0.105** | 0.751 |
| `local` / cauchy | 0.788 | 0.553 | 0.706 | 0.486 | 0.299 | 0.114 | 0.749 |
| `global` / flat | 0.772 | 0.779 | 0.889 | 0.295 | 0.539 | 0.400 | 0.687 |
| `global` / cauchy | 0.774 | 0.771 | 0.886 | 0.305 | 0.519 | 0.400 | 0.689 |

**The kernel does nothing.** Every Cauchy-against-flat main effect is 0.019 or
smaller, at or under the seed sd of the same metric. E36's leader was a scope
result wearing a weighting label, and E29/E31's earlier verdict on weighting
("moves the ranking, not the decision") stands.

**And `global`'s 86% accuracy is bought in the one currency the map cannot
spend.** It decides 22 pp more stands and is 18.8 pp more accurate when it
decides -- while calling **40% of the field-degraded stands `good_condition`**
against `local`'s 10.5%, and finding 21 pp fewer of them. Balanced accuracy over
the decided stands reverses the ranking entirely: 0.751 `local` against 0.687
`global`. The result E36 called "the strongest single result the four-axis view
has produced" is an artefact of a 7:1 class imbalance in the validation set.

With the transformed pool pooled per forest type (E22/E24/E26), **`local` + flat
+ pooled bad is the best cell on every honest axis**: stand AUC 0.804, degraded
recall 0.581, degraded-called-good 0.038, balanced accuracy 0.829, within-
locality AUC 0.689. That is the configuration the next round should be built on.

E38 and E40 are marked non-rankable on the tracker for the mirror image of E36's
defect: their numbers are computed on the 404-stand intersection so that their
own cells are comparable, which makes them incomparable to a leaderboard row
scored on 421.

### E39 -- the decision rule, tested as seriously as the score

Everything E17-E37 varied sits upstream of one line: a stand is called when its
interval clears a single Youden threshold fitted on the other blocks. Three
assumptions live in that line, and E39 separates them at two configurations
(E38's winner, and the `global` family E36 ranked first).

**One threshold for two forest types: it does not matter.** Fitting Youden's J
separately per type moves balanced accuracy from 0.829 to 0.821 and degraded
recall from 0.581 to 0.562; partial pooling toward the shared threshold
(shrinkage weight `n_bad / (n_bad + 10)`) lands between them. All three sit
inside the seed spread. Under nested selection -- the rule chosen by balanced
inner leave-block-out accuracy on the training blocks alone, then applied to the
held-out block -- the choice is unstable across seeds (31 blocks pick `per_type`
at one seed, 27 pick `partial` at another, 11 pick `pooled` at a third) and the
result is indistinguishable from always using `pooled`. **The per-type threshold
is not the missing piece.**

**The threshold's own error bar: it is the size of the stand's.** Resampling the
training blocks gives a median threshold-interval width of 0.064 against a median
stand CI width of 0.068 -- a ratio of 0.93. Requiring a stand's interval to clear
the *whole* threshold interval before a verdict is issued:

| rule (E38's winner) | decided | acc when decided | balanced acc | degraded recall |
|---|---|---|---|---|
| pooled (shipped rule) | 0.616 | 0.749 | 0.829 | 0.581 |
| pooled + threshold CI | 0.330 | 0.803 | 0.836 | 0.286 |
| per_type + threshold CI | 0.347 | 0.818 | 0.845 | 0.286 |

**The map is roughly twice as decisive as the evidence supports.** Balanced
accuracy does not fall when the extra calls are withdrawn (0.829 -> 0.836), so
the withdrawn verdicts were not systematically wrong -- they were unsupported.
The same holds on `global`, where 51 pp of decisiveness disappears. Whether to
ship the two-sided rule is a policy question about what an `indistinguishable`
stand costs the user, not a statistical one; what is settled is that the current
figure of 56-62% decided is an overstatement, and every ledger row that ranked
configurations on `frac_indistinguishable` was ranking them on a quantity with a
missing term.

**Seeds are not localities.** Repeated seeds measure the draw of the reference
pool. The generalisation slice reported alongside every row is the subset of
stands in the five (`local`) or six (`global`) blocks that hold a degraded stand
at all -- the only localities where leave-block-out actually withholds a degraded
label. Balanced accuracy there is 0.685 against 0.829 pooled, which is the same
gap the report's within-locality AUC shows and the reason E41 exists.

### E40 -- the pool between nearest-eight and everything

E38 settles what each extreme buys and leaves the obvious question unasked: the
shipped `local` pool is the **eight nearest** surveyed `god` stands and `global`
is **all ~160** of the forest type, and nothing in the ledger had ever tried the
middle. Two ways to sit in between, and they fail differently. A **count**
(`n_donors`) gives every probe the same support and lets a probe in a sparse
locality reach 100 km for its 64th donor. A **radius** (`donor_km`, new, with the
count kept as a floor so a sparse probe is not left with two donors) gives every
probe the same environmental match and lets a donor-rich locality contribute
more. The 3 km exclusion is retained throughout.

| pool | donors | stand AUC | within-locality | CI width | decided | degraded recall | degraded called good | balanced acc | worst adjacent |
|---|---|---|---|---|---|---|---|---|---|
| `local` n=8 (deployed) | 8 | 0.804 | 0.689 | 0.0669 | 0.616 | **0.581** | **0.038** | **0.829** | 0.585 |
| `local` n=16 | 16 | 0.749 | 0.694 | 0.0675 | 0.595 | 0.495 | 0.086 | 0.804 | 0.546 |
| `local` n=32 | 32 | 0.797 | 0.758 | 0.0673 | 0.669 | 0.581 | 0.076 | 0.802 | 0.618 |
| `local` n=64 | 64 | 0.805 | 0.753 | 0.0686 | 0.686 | 0.514 | 0.181 | 0.802 | 0.613 |
| `local` r=6 km | 8 | 0.810 | 0.682 | 0.0659 | 0.656 | 0.533 | 0.076 | 0.784 | 0.607 |
| `local` r=10 km | 22 | 0.822 | 0.715 | 0.0683 | 0.707 | 0.562 | 0.133 | 0.774 | 0.622 |
| **`local` r=20 km** | 42 | **0.836** | **0.783** | 0.0672 | 0.723 | 0.543 | 0.181 | 0.771 | **0.636** |
| `global` | 159 | 0.796 | 0.719 | 0.0628 | 0.749 | 0.438 | 0.248 | 0.774 | 0.609 |

**A radius beats a count, and the middle beats both extremes on ranking.** The
20 km pool is the best-ranking configuration this ledger has produced: stand AUC
0.836 ± 0.002 against the deployed pool's 0.804 in the same table (that 0.804 is
the internal control -- it reproduces E21/E36's `local` + pooled row, which is
what makes the rest of the column readable), within-locality AUC 0.783 against
0.689, and worst-adjacent-class AUC 0.636 against 0.585. **That last number is
the largest movement the four-class bottleneck has ever seen here**, and E36 put
the leading configurations at 0.59-0.60.

**And it is not bought with a narrower interval.** The median stand CI width is
0.0672 against the deployed pool's 0.0669 -- flat. Across the eight pools, donor
count correlates with within-locality AUC at rho = +0.78 and with CI width at
rho = +0.11. The gain is discrimination, which is the success criterion this
question was set with.

**The count sweep is not monotone and the radius sweep is.** `n=16` drops to
0.749, outside its own seed sd, while every radius rung improves on the one
below it. A fixed count reaches for its Nth-nearest donor whatever the distance;
a radius does not, and the difference shows up as a pool that stays
environmentally matched.

**What it costs.** Degraded stands reported as `good_condition` rise from 0.038
to 0.181 and balanced accuracy falls from 0.829 to 0.771 -- decisiveness climbs
faster than the pool earns it. Read that against E39: those are exactly the
verdicts a threshold carrying its own interval could not have supported. E42
prices the two changes together rather than separately.

### E42 -- the radius pool under the honest threshold

E39 and E40 disagree about the same stands, so the two changes are priced
together rather than separately. Each pool is scored twice: at the shipped point
threshold, and requiring the stand interval to clear the whole block-resampled
threshold interval.

| pool | rule | decided | balanced acc | degraded recall | degraded called good | threshold CI width |
|---|---|---|---|---|---|---|
| `local` n=8 (deployed) | point | 0.616 | 0.829 | 0.581 | 0.038 | -- |
| `local` n=8 (deployed) | threshold CI | 0.330 | 0.836 | 0.286 | 0.038 | 0.0686 |
| `local` r=10 km | point | 0.707 | 0.774 | 0.562 | 0.133 | -- |
| `local` r=10 km | threshold CI | 0.437 | 0.799 | 0.286 | 0.124 | 0.0753 |
| `local` r=20 km | point | 0.723 | 0.771 | 0.543 | 0.181 | -- |
| **`local` r=20 km** | **threshold CI** | **0.483** | 0.779 | 0.286 | 0.124 | 0.0715 |
| `global` | point | 0.749 | 0.774 | 0.438 | 0.248 | -- |
| `global` | threshold CI | 0.177 | 0.966 | 0.295 | 0.000 | 0.1055 |

**The two changes compose.** The honest threshold removes precisely the calls the
radius pool should not have made -- degraded stands reported as good fall from
0.181 to 0.124 while balanced accuracy rises -- and withdraws nothing from the
deployed pool's 0.038, because it was never making those calls. **And the radius
pool keeps its support**: under the same rule it decides 0.483 of stands against
the deployed pool's 0.330. Local references really do lack independent backing,
and a 20 km radius supplies it without reaching across the country.

**What remains is a real choice, honestly stated.** Under the two-sided rule the
deployed pool decides a third of stands at balanced accuracy 0.836 with 3.8% of
degraded stands called good; the 20 km pool decides half of them at 0.779 with
12.4% called good. Every `local` pool converges on the same degraded recall of
0.286, so the trade is coverage against the cost of a wrong reassurance, not
detection.

**`global` fails differently and instructively.** Its threshold interval is the
widest of the four (0.1055 against a stand interval of 0.0628), so under the
honest rule it collapses to 0.177 decided. Its apparent decisiveness was resting
on a threshold estimate its own training blocks do not pin down.

### E41 -- the sampling frame, and one answer that needed no fieldwork

Every number in this ledger rests on 36 degraded stands in six 5 km localities,
and only **four** localities hold a `god` and a degraded label at once. No
configuration search fixes that. E41 writes the frame instead
(`outputs/research/validation_targets.csv`, 334 stands in 48 localities): the 63
stands called poor locally and good globally, the 60 abstained stands whose score
sits closest to the threshold (capped at three per locality), 74 olivinskog
stands the map calls or nearly calls poor -- the type where E38 measures degraded
recall at 0.08-0.50 against `kalklindeskog`'s 0.50-0.83 -- the 9 good-but-
declining priority stands, 88 matched controls (same locality, comparable size,
called good by both scopes) so the within-locality contrast is actually
measurable, and 40 independent stands drawn outside every target group.

**The payoff, in the currency that binds: 4 localities hold both classes today;
this frame could bring that to 34.**

And one question the report asked is already answered by labels that exist. **40
of the 63 local-poor / global-good disagreements carry a field class**: 19 `god`,
17 `moderat`, 4 degraded. Neither scope wins outright on the `god`-vs-not call
(`local` 52.5%, `global` 47.5%), but the disagreement set is enriched in
not-`god` -- 57.5% against a 40.2% base rate over all labelled stands -- which
is the signature of the global pool averaging real departure away rather than of
the local pool matching a degraded neighbourhood. It is a 40-stand sample and it
points one way; the frame is how it gets settled.

### E37 -- `diff` instead of the ratio, priced in stands

The difference score's edge on the four-class and overlap axes is 0.003 wide at
E06's configuration, so E37 asked the only question that settles it. At the
deployed configuration the swap moves **13 of 404 stands (3.2%)**, **none across
the diagonal** (no good <-> poor reversal), and of the twelve labelled stands
that move, **two become correct and seven become wrong**. Abstention 0.441 ->
0.448, accuracy-when-decided 0.709 -> 0.694, worst adjacent pair 0.589 -> 0.590.
**Do not ship `diff`.**

On the default four objectives before E36 corrected the abstention axis, `local`
with a 5 km good-pool radius (E20) topped the leaderboard at 0.0481; on the
corrected axis it is fifth at 0.0486 and the kernel-weighted `global`
configurations lead. Adding within-locality AUC as a fifth objective hands the
top to the **pooled-per-type** transformed cloud (E22/E24/E25). Read that against
E27: pooling's gain does not survive translation into verdicts, so the
hypervolume ranks the metrics, not the decision consequences, and a candidate
still has to be priced in stands-that-change-class before it ships.

The ledger holds forty-two experiment families, E00–E35 for condition and
T00–T05 for trajectory. The engine there reproduces this pipeline's pre-change
output to three decimals, which is what makes the differences attributable to one
change at a time.

Things that were tried and did not work, so they are not tried again: the field
bad pool (E03), supervised models fitted to the field labels on the embedding
(E07 first, then E33–E35 across nine model families — but read the correction
there: E07's pooled AUC was not a valid statistic, and the honest verdict is "as
separating within a locality, worse on olivinskog, and structurally unable to
produce a score comparable across localities" rather than "collapses"),
the Hamed & Rao
autocorrelation correction (T02), summarising pixel scores instead of averaging
the embedding (E13), tuning k or the pool caps as a route to separation
(E05 — every knob moves the result by less than the bootstrap interval),
searching for a better closed-form score with symbolic regression (E17 — the
search discards `m_good` and returns a monotone function of `m_bad`, and a
gradient-boosted model with far more freedom than any expression it returns
comes last of ten), and one-class conformal typicality against the reference
cloud (E18 — a degraded stand is not an outlier among surveyed forest, so
novelty detection cannot rank it).

Three more, all from CSIRO's ClassicHCAS and all measured at the deployed
configuration: standardising the departure by local reference dispersion
(E28 — the dispersion separates the labels on its own and in the same direction,
so the ratio cancels its own numerator and scores below chance); weighting or
boosting the k retained references (E29/E31 — it moves within-locality AUC and
Youden J by four seed sds and makes the actual verdicts *less* accurate, which
is a warning about those two metrics as much as about the estimator); and the
HCAS reference-density surface itself (E30 — a density value reads the bulk of a
64-dimensional pair-distance distribution, where both classes sit, and only the
left tail carries condition). E28's by-product was carried forward and
also fails (E32 — the dispersion adds a real +0.034 to the reference side and
+0.038 on olivinskog, but costs stand AUC, within-locality AUC and Youden J when
put into the ratio; it is reported as a band, not scored with).

E34 is the one experiment in this ledger whose result matters outside this
project. The same model, the same evaluation pixels and the same out-of-fold
protocol give an RBF SVM an AUC of **1.000** under ungrouped pixel k-fold,
**0.877** under leave-one-stand-out, and **0.455** — below chance — under
leave-one-5 km-block-out. Grouping by the mapped polygon, which most published
work would call rigorous, recovers less than half of the correction.
