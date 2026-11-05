# Method specification: stand condition and trajectory from AlphaEarth

What the shipped `forestmon condition` pipeline computes, and why each default
is what it is. How to run it is in [README.md](README.md). Experiment ids in
parentheses, e.g. (E44), refer to the research ledger of the development
project; that ledger is not part of this repository.

Two results per stand and per 10 m cell, from the same 64-d annual embedding
(2017-2025):

1. **Condition** (step 04) - where has the stand arrived? A 0-1 "AE score",
   adapted from [DegreeofRecovery](https://github.com/Geethen/DegreeofRecovery).
2. **Trajectory** (step 03) - which way is it moving? Adapted from
   [RECOVER](https://github.com/Geethen/RECOVER). It reads the yearly condition
   scores, so step 04 runs first.

## 1. Inputs

| input | source | use |
|---|---|---|
| AlphaEarth annual embedding, 64 bands, 10 m, 2017-2025 | Source Cooperative `tge-labs/aef` (int8 COGs; anonymous HTTPS) | the feature space |
| Mapped stands, six layers of `skog_kartlegginger.gpkg` | `FORESTS_KARTLEGGING_GPKG` | probes and good donors |
| ESA WorldCover v200 (2021 epoch) | AWS `esa-worldcover` | transformed (bad) pool |
| Hansen Global Forest Change loss year | Earth Engine (step 02e; required by step 04) | donor hygiene, trajectory calibration, validation |

The embedding is dequantised as `sign(raw) * (raw/127.5)^2`, giving unit-norm
vectors. Tiles are bottom-up and nodata outside their natural UTM zone; the
reader is block-aligned (whole 1024x1024 blocks, all bands).

Stands: 737 (589 kalklindeskog, 148 olivinskog). Field label `tilstand`:

| `tilstand` | stands | role |
|---|---:|---|
| `god` | 263 | good reference pool |
| `moderat` | 141 | held out: scored, never a reference |
| `dårlig`, `sværtRedusert` | 20 + 16 | evaluation only (36 stands are too few to anchor an end-member) |
| none | 297 | scored, never a reference |

Per stand, step 00 records the survey year and a **spatial block** (5 km
single-linkage cluster of stand centroids; 66 blocks). The forest type is the
mapping layer's type (olivinskog or kalklindeskog).

## 2. Unit of scoring

The probe is a **10 m pixel-year**, not a stand: a stand is rarely in one
condition, and sub-stand degradation is what the project looks for. Overlapping
polygons are handled by a pixel-membership model (step 02b): each piece of
ground carries its embedding once, keyed by `pixel_id`; which stands cover it
is a separate many-to-many table (30.6 % of pixels belong to more than one
stand). Pixels that two layers label contradictorily enter no pool. All pool
exclusions are by `pixel_id`, never by stand id, and donor stands within 20 m of
the probe are excluded outright.

## 3. Reference pools

**Good pool.** Pixels of `tilstand = god` stands of the *same forest type*
(WorldCover cannot tell intact kalklindeskog from a spruce plantation: both are
class 10).

* *Survey-year only.* A label describes the day it was assigned, so a donor
  contributes only its survey-year pixels; stands surveyed before 2017 cannot be
  donors (263 `god` give 244 usable). Every output row carries
  `label_age = year - survey_year`; the threshold is fitted on labelled stands in
  their own survey year.
* *Spatial exclusion.* A probe never sees a donor whose centroid is within
  **3 km**. This is the inner edge of the transformed annulus, so one rule serves
  both pools. Whole-block exclusion was rejected because it destroyed the regional
  matching `local` exists for (E20).
* *Cleared donors dropped.* Donor pixels with a Hansen loss year at or before the
  donor's survey year are removed (1,072 pixels, 35 donors). Free, and keeps
  clearcuts out of the good pool (E25).
* *Two scopes, both always reported.*

| scope | pool | answers |
|---|---|---|
| `local` | the 32 nearest eligible `god` stands within 50 km (E44); for olivinskog every donor within 20 km, at least 8 (E60) | poor for its region? |
| `global` | every eligible `god` stand of the type | poor for its type? |

* *Caps.* 60 pixels per donor, 2,000 for the global pool, stratified by donor
  under a fixed seed, so one large stand cannot supply most of the yardstick.

**Bad (transformed) pool.** ESA WorldCover cropland (40) and built-up (50) taken
from a **3-8 km ring** around every stand, in the same year as the probe.
Grassland and bare ground are excluded on purpose (in Norway they absorb fjell,
pasture and scree). Rings are **pooled per forest type** (cap 2,000 pixels per
type and year) so that all stands of a type are judged against one yardstick,
rather than each against its own surrounding farmland (E38). Step 02c plans the
AlphaEarth blocks as a set cover so that every stand has at least 150 transformed
pixels in its ring; stands still short get the outer radius widened 8, 16, 32,
64 km (recorded per stand).

## 4. Metric: shrunk-whitened cosine distance

Distances are taken after

```
x -> normalise( Sigma_l^(-1/2) (x/|x| - mu) ),   Sigma_l = (1-l) Sigma + l (tr Sigma / p) I,   l = 0.2
```

with mu and Sigma fitted on **unlabelled** pixels of the same forest type, so no
label information enters the metric. A few embedding directions carry terrain
and phenology variance and swamp the condition contrast; whitening is the single
largest gain in the project (pooled pixel AUC 0.627 to 0.684, E04 vs E12).
Shrinkage 0.2 keeps the worst-estimated directions from dominating: full
whitening gives pixel AUC 0.648 and leaves 39.9 % of stands undecided against
31.6 %.

## 5. Score: kNN Degree of Recovery

For a pixel with embedding x in year t:

```
            m_bad(x)
dor(x) = ---------------------
          m_good(x) + m_bad(x)
```

where `m_good` / `m_bad` are the mean cosine distances (in the whitened metric)
to the **k = 25** nearest good / transformed reference pixels. A score near 1
is close to the good pool. The score is computed as a dense matmul plus top-k.
`k = 25` is a variance choice: a five-nearest mean out of a donor pool is often
five pixels of one donor; k = 25 cuts the interval by a third and costs about
0.027 pixel AUC (E05). Pixels whose pool has fewer than k pixels (e.g. a
single-donor local pool) score NaN rather than averaging a pool that is one donor
seen many times; `n_good` / `n_bad` / `n_valid` record why.

Outputs per pixel-year: `dor_local`, `dor_global`, `m_good_*`, `m_bad`,
`label_age`. There is deliberately no per-pixel confidence interval: the
bootstrap measures reference-pool error, which all pixels of a stand share.

## 6. Stand score, interval and class

* **Stand score** = median of the stand's cell scores in that year (checkable
  against the cell map). Stand AUC is 0.836 for the median and 0.829 for the
  mean-embedding probe (difference -0.008, 95 % CI -0.027 to 0.012). The
  mean-embedding value is kept as `dor_<scope>_probe`. Also written: IQR, 10th
  percentile and `frac_poor`, the share of the stand's area below threshold.
* **Interval**: donor-clustered bootstrap, 2,000 draws. Donor stands (good side)
  and 500 m localities (transformed side) are resampled as whole clusters,
  because 2,000 reference pixels from about 115 donor stands are not 2,000
  independent observations. One draw is shared by all cells of the stand-year,
  every cell is rescored under it, and the draw's value is the median over cells
  (stands above 2,001 cells use 2,001 cells at evenly spaced ranks, shifted to
  the full-stand median; checked within 1e-4). Row-wise resampling was about a
  third too narrow, so roughly one stand in eight carried a class its references
  did not support (E09). Median width: 0.049 (`local`), 0.042 (`global`).
* **Class**: compare the interval with the threshold. Entirely above: good;
  entirely below: poor; straddling: `indistinguishable`. Unscoreable stands are
  NaN, never estimated. The threshold's own sampling error is reported
  (`threshold_se_*`) but not used to withdraw verdicts (E43).

## 7. Threshold: leave-block-out Youden J

The threshold maximises Youden's J (sensitivity + specificity - 1) between
labelled `god` and degraded stands, each in its survey year. It is fitted
**leave-block-out**: one threshold per spatial block, fitted on labelled stands
outside that block, so a stand is classified by a threshold that never saw its
locality. Values are in `threshold.json` and the `threshold_*` columns. Shipped
values (median stand score): `local` 0.511 (J 0.564, sensitivity 0.735,
specificity 0.829; 238 `god`, 35 degraded) and `global` 0.512 (J 0.479; 250 and
36). Blocks serve this job and the intervals, not pool construction. A J near 0
would mean the pools do not separate; report the continuous score instead.

Headline stand class uses `local`; stands with no local score take their
`global` class.

## 8. Trajectory

Input: the yearly **AE score at the `global` scope**: the stand's median cell
score per year at stand grain, each cell's own score at pixel grain. (A
projection onto a fixed "naturalness axis" was used before 2026-10-23; it finds
Hansen clearing better but gives a different scalar from the condition class.)

1. **z-score** within forest type over all pixels and years of that type.
2. **Theil-Sen slope** (median of pairwise slopes), z-units per year.
3. **Mann-Kendall** S, Z and two-sided p, with the tie correction
   `Var(S) = [n(n-1)(2n+5) - sum t(t-1)(2t+5)] / 18`. `mk_p` is reported but
   does not decide the class.

Classification gates the **effect size**, not the p-value. Mann-Kendall is
correctly calibrated on nine points (year-shuffled false-positive rate 4.3 % vs
5 % nominal) but lacks power. The gates `gate_lo` / `gate_hi` are the 2nd and
98th percentiles of slopes of units Hansen says were never cleared, so each
class has a 2 % false-positive rate by construction.

| class | rule |
|---|---|
| `improving` | slope > `gate_hi` |
| `declining` | slope < `gate_lo` |
| `stable` | neither, series sd < `sd_stable` |
| `fluctuating` | neither, series sd >= `sd_stable` |
| `no_data` | fewer than 6 finite years, or Var(S) <= 0 |

Shipped calibration (`trajectory_gate.json`): gates -0.100 / +0.159 z/yr (stand)
and -0.083 / +0.184 (pixel); `sd_stable` is the 81.4th percentile of Hansen-clean
units, 0.50 z (stand) and 0.55 z (pixel). Gates and cut are pooled over both
forest types, so per-type fluctuating counts (olivinskog 42) reflect the shared
cut, not instability. The improving gate sits well above zero because the AE
score drifts upward across the area (median stand slope +0.04 z/yr). The stable
test uses the sd, not the coefficient of variation, which explodes on a
zero-centred z series. Without step 02e (no Earth Engine) the step falls back to
the p < 0.05 rule and says so; the classes then have no stated false-positive
rate.

## 9. Validation (step 05)

All checks use labelled stands at `label_age == 0`.

1. **Ordering**: median score falls god > moderat > dårlig > sværtRedusert, with
   the held-out `moderat` between the anchors.
2. **Separation**: AUC and Youden J at stand and pixel grain, per forest type,
   with a bootstrap over whole spatial blocks. Differences under about 0.04 are
   ties.
3. **Components**: `m_good` and `m_bad` AUC separately; warns if the transformed
   term alone comes within 0.02 of the ratio, since a high score can mean
   "far from farmland" rather than "close to good forest".
4. **Controls**: the same AUC within log-area strata, within localities, and on
   pixels at least 20 m inside their own stand. Stand area alone separates the
   labels at 0.652.
5. **Coherence**: declining stands should score lower than improving ones.
   Trajectory and condition share the AE score, so agreement is partly by
   construction; the useful output is the disagreement (good but declining;
   poor but improving).

Trajectory is also checked against Hansen (cell recall and AUC at 2 % FPR).

## 10. Key evidence

Production run 2026-10-23 (737 stands, 168,832 cells, 2.0 M pixel-year scores).

| result | value |
|---|---|
| Ordering | monotone at both scopes; `local` 0.556 / 0.525 / 0.482 / 0.431 |
| Stand AUC, god vs degraded, `local` | 0.836 [0.73, 0.90]; kalklindeskog 0.859, olivinskog 0.772 |
| Stand AUC, `global` | 0.779 [0.68, 0.89] |
| Pixel AUC, `local` / `global` | 0.775 / 0.768 |
| Whitening (l = 0.2) | pixel AUC 0.627 to 0.684 (E04, E12) |
| 3 km exclusion vs whole block | `local` pixel AUC 0.697 to 0.769; `m_good` alone 0.593 to 0.685 (E20) |
| Pooled transformed end-member | degraded recall 0.533 to 0.581; degraded called good 0.105 to 0.038 (E38) |
| 32-stand local pool vs 8 | within-locality AUC 0.685 to 0.758 (E44) |
| Slope gate vs p < 0.05 | recall on Hansen-cleared 0.26 to 0.51 at 2 % FPR (axis input; T04) |
| Trajectory vs Hansen, shipped input | cell recall 0.33 at 2 % FPR, cell AUC 0.73, stand AUC 0.64 |
| Stand classes 2025, `local` (+`global` fallback) | kalklindeskog 307 good / 157 indistinguishable / 125 poor; olivinskog 12 / 61 / 75 |
| Trajectories | 582 stable, 115 fluctuating, 25 declining, 15 improving |

## 11. Known limitations

* **The bad pool is converted land, not degraded forest.** Cropland and built-up
  are far from any forest, so part of the score is distance from converted land;
  read `m_good_*` beside the score, especially at `global`.
* **Few degraded labels.** 36 degraded stands in six localities, three of which
  also hold a good stand, bound every accuracy figure. Olivinskog rests on
  twelve degraded stands (pixel AUC interval [0.54, 0.82]) and has 65 donors to
  kalklindeskog's 185.
* **Within-locality discrimination is weak.** Pooled AUCs partly reflect telling
  localities apart. Stand area alone separates the labels at 0.652, and 428 of
  737 stands have no pixel 20 m inside their boundary (see the `edge_m` band).
* **`local` covers fewer stands** (44 have only a `global` score: no eligible
  donor within 50 km, or fewer donor pixels than k).
* **Roughly a quarter of stands are `indistinguishable`.** That is the honest
  answer under a donor-clustered interval; report the continuous score.
* **Labels span 2015-2025** and describe whole stands; a pixel-grain AUC below
  the stand-grain one is expected. There is no independent validation set for
  condition (`moderat` shares the survey campaign).
* **Hansen sees canopy removal only** and is 30 m. It validates the trajectory
  (screening flag, not a detector); absence of loss is not absence of
  degradation. Stand-grain `declining` is a screening flag.
* **Nine annual points** is a short series; WorldCover is a single 2021 epoch
  applied to all years.
* **Embedding provenance.** The community mirror was checked against Earth
  Engine's `GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL` (cosine 1.0 after the signed-
  square dequantisation).
