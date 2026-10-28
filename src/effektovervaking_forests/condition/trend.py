"""Step 1 -- trajectory classification: z-score -> Theil-Sen -> Mann-Kendall.

Ported from RECOVER (`scripts/analysis/analyze_trajectories.py`,
`scripts/analysis/test_trend_classification.py`, "Option A"), which ran this on
annual GPP and short-vegetation-height series. Those products no longer carry
recent years, so the same three-step recipe is applied here to an annual scalar
derived from the AlphaEarth embedding (see `condition.axis`).

What is kept from RECOVER:
  * the fully vectorised pairwise formulation -- all pairs (i<j) computed once
    with `np.triu_indices`, so B pixels x T years costs one (B, T*(T-1)/2)
    array rather than a Python loop per pixel;
  * z-score normalisation before the trend test, so the classification responds
    to change *relative to the stratum's own spread* rather than to absolute
    level (RECOVER standardised per biome; here per forest type -- see
    `zscore_by_group`);
  * the rule-based class assignment (significant positive / significant
    negative / stable / fluctuating) with a CV gate separating stable from
    fluctuating.

What is fixed relative to the RECOVER version:
  * Var(S) now carries the standard tie correction and is computed from each
    row's own count of valid years, instead of assuming no ties. With 9 annual
    steps and a quantised input, ties are common and the uncorrected variance
    inflates |Z| (i.e. reports too many significant trends).
  * `sen_slope` is the median over pairs with a finite difference only; pairs
    touching a NaN are excluded rather than propagated.

The Mann-Kendall p-value uses the normal approximation to S. With T = 9 annual
observations that is the usual practice but it is an approximation; treat
p near the threshold as indicative, and prefer the effect size (`sen_slope`,
expressed in z-units per year) when ranking.

**The class rule gates the effect size, not the p-value.** Validated against
Hansen Global Forest Change loss year -- an independent, Landsat-based annual
product -- the significance rule turned out to be correctly calibrated and
badly underpowered: on clean pixels it fires at 4.5% against a nominal 5% (the
year-shuffled null gives 4.3%, so the tie correction is doing its job), but it
flags only 26% of pixels Hansen says were cleared. Ranking the same series by
Theil-Sen slope magnitude and cutting at the 2nd percentile of undisturbed
reference pixels finds 50% of them at the same 2% false-positive rate, and lifts
stand-grain AUC from 0.844 to 0.921 (`condition/research/`, experiments
T01-T04). Mann-Kendall asks whether a trend exists; a manager needs to know
whether it is big enough to act on, and with nine annual points those are very
different questions. `mk_p` is still reported.

Two corrections that did NOT help, so that they are not tried again: the Hamed &
Rao autocorrelation-corrected variance makes a correctly calibrated test
anti-conservative here (6.4% on the shuffled null), because a nine-point
autocorrelation estimate is mostly noise; and a best-single-step statistic --
which ought to suit a clearcut-then-recover shape better than a monotone test --
ranks worse per pixel than the slope does.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "zscore_by_group",
    "theil_sen_mann_kendall",
    "classify_trend",
    "calibrate_slope_gate",
    "TREND_CLASSES",
    "GATE_PCTL_DEFAULT",
]

# The false-positive rate the slope gate is calibrated to, as a percentile of
# the undisturbed reference distribution. 2% each side; see
# `calibrate_slope_gate`.
GATE_PCTL_DEFAULT = 2.0

TREND_CLASSES = ("improving", "declining", "stable", "fluctuating", "no_data")

# Minimum number of finite years before a row is testable at all. RECOVER used
# 5 of 23; with only 9 annual embeddings we ask for 6.
MIN_VALID_YEARS = 6


def zscore_by_group(
    values: np.ndarray, groups: np.ndarray, eps: float = 1e-9
) -> np.ndarray:
    """Z-score each row against the pooled mean/sd of its group.

    `values` is (B, T), `groups` is (B,). The mean and sd are taken over *all*
    cells of the group (every pixel, every year), exactly as RECOVER's
    biome-stratified standardisation did -- so a series is expressed relative to
    what the whole stratum does, and a trend of +0.1 z/yr means the same thing
    in olivinskog as in kalklindeskog.
    """
    out = np.empty_like(values, dtype=np.float32)
    for g in np.unique(groups):
        m = groups == g
        block = values[m]
        mu = np.nanmean(block)
        sd = np.nanstd(block) + eps
        out[m] = (block - mu) / sd
    return out


def theil_sen_mann_kendall(y: np.ndarray, chunk: int = 100_000) -> pd.DataFrame:
    """Theil-Sen slope + Mann-Kendall Z/p for every row of `y` (B, T).

    Rows are processed in chunks so the (B, T*(T-1)/2) pair array stays bounded.
    Returns one row per input row with columns:
        sen_slope  Theil-Sen slope, units of `y` per time step
        mk_s       Mann-Kendall S statistic
        mk_z       normal-approximation Z (tie-corrected variance)
        mk_p       two-sided p-value
        n_valid    finite observations used
        mean, sd, cv, autocorr_lag1
    """
    y = np.asarray(y, dtype=np.float64)
    if y.ndim != 2:
        raise ValueError(f"expected a 2-D (pixels, years) array, got {y.shape}")
    parts = [_tsmk_block(y[k : k + chunk]) for k in range(0, len(y), chunk)]
    return pd.concat(parts, ignore_index=True)


def _tsmk_block(y: np.ndarray) -> pd.DataFrame:
    B, T = y.shape
    i, j = np.triu_indices(T, k=1)
    dx = (j - i).astype(float)

    diffs = y[:, j] - y[:, i]  # (B, P)
    finite = np.isfinite(diffs)

    # --- Theil-Sen: median slope over finite pairs only -------------------
    slopes = np.where(finite, diffs / dx[None, :], np.nan)
    with np.errstate(invalid="ignore"):
        sen = _nanmedian_safe(slopes)

    # --- Mann-Kendall S ---------------------------------------------------
    signs = np.where(finite, np.sign(diffs), 0.0)
    S = signs.sum(axis=1)

    n = np.isfinite(y).sum(axis=1).astype(float)
    # Var(S) = [n(n-1)(2n+5) - sum_t t(t-1)(2t+5)] / 18
    var_s = (n * (n - 1.0) * (2.0 * n + 5.0) - _tie_correction(y)) / 18.0

    valid = (n >= MIN_VALID_YEARS) & (var_s > 0)
    sigma = np.sqrt(np.where(valid, var_s, 1.0))

    Z = np.zeros(B)
    pos, neg = (S > 0) & valid, (S < 0) & valid
    Z[pos] = (S[pos] - 1.0) / sigma[pos]
    Z[neg] = (S[neg] + 1.0) / sigma[neg]
    # scipy is only needed for the normal survival function; keep the import
    # local so the module imports in a scipy-free environment.
    from scipy import stats

    p = 2.0 * stats.norm.sf(np.abs(Z))
    p[~valid] = np.nan
    Z[~valid] = np.nan
    sen[~valid] = np.nan

    mean = np.nanmean(y, axis=1)
    sd = np.nanstd(y, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        cv = sd / np.abs(mean + 1e-6)

    return pd.DataFrame(
        {
            "sen_slope": sen,
            "mk_s": S,
            "mk_z": Z,
            "mk_p": p,
            "n_valid": n,
            "mean": mean,
            "sd": sd,
            "cv": cv,
            "autocorr_lag1": _lag1(y),
        }
    )


def _nanmedian_safe(a: np.ndarray) -> np.ndarray:
    """np.nanmedian over axis 1, returning NaN (not a warning) for all-NaN rows."""
    out = np.full(a.shape[0], np.nan)
    ok = np.isfinite(a).any(axis=1)
    if ok.any():
        out[ok] = np.nanmedian(a[ok], axis=1)
    return out


def _tie_correction(y: np.ndarray) -> np.ndarray:
    """sum over tie groups of t(t-1)(2t+5), per row.

    Mann-Kendall's Var(S) is reduced by this term whenever the series repeats a
    value. The embedding-derived series is a projection of quantised int8 data
    and does tie, so the correction is not optional here.
    """
    B, T = y.shape
    out = np.zeros(B)
    ys = np.sort(np.where(np.isfinite(y), y, np.nan), axis=1)
    for b in range(B):
        row = ys[b][np.isfinite(ys[b])]
        if row.size < 2:
            continue
        _, counts = np.unique(row, return_counts=True)
        t = counts[counts > 1].astype(float)
        if t.size:
            out[b] = np.sum(t * (t - 1.0) * (2.0 * t + 5.0))
    return out


def _lag1(y: np.ndarray) -> np.ndarray:
    a, b = y[:, 1:], y[:, :-1]
    da = a - np.nanmean(a, axis=1, keepdims=True)
    db = b - np.nanmean(b, axis=1, keepdims=True)
    num = np.nansum(da * db, axis=1)
    den = np.sqrt(np.nansum(da**2, axis=1) * np.nansum(db**2, axis=1))
    with np.errstate(divide="ignore", invalid="ignore"):
        return num / (den + 1e-9)


def calibrate_slope_gate(
    slopes, pctl: float = GATE_PCTL_DEFAULT
) -> tuple[float, float]:
    """(declining_below, improving_above) from the slopes of UNDISTURBED pixels.

    `slopes` must come from reference pixels believed not to have changed --
    here, pixels in stands with no Hansen forest loss in any year. Cutting at
    the `pctl` and `100 - pctl` percentiles of that distribution fixes the
    false-positive rate of each class at `pctl`% by construction, whatever the
    noise level of the embedding series happens to be, and lets recall be
    whatever the data supports rather than whatever a p-value allows.

    Note what this does NOT claim: the reference pixels are "no evidence of
    clearing", not "intact" -- Hansen sees canopy removal and is blind to the
    selective removal and understorey change these habitat types are mostly
    lost to. So the calibrated rate is an upper bound on the false-positive
    rate against real stability, and the recall figures it buys are lower bounds.
    """
    s = np.asarray(slopes, dtype=float)
    s = s[np.isfinite(s)]
    if len(s) < 100:
        raise ValueError(
            f"need at least 100 undisturbed reference slopes to calibrate a "
            f"gate, got {len(s)}"
        )
    return float(np.percentile(s, pctl)), float(np.percentile(s, 100.0 - pctl))


def classify_trend(
    feat: pd.DataFrame,
    alpha: float = 0.05,
    sd_stable: float = 0.30,
    min_slope: float = 0.0,
    gate: tuple[float, float] | None = None,
) -> pd.Series:
    """Rule-based trajectory class.

    With `gate = (lo, hi)` -- the recommended path, from
    `calibrate_slope_gate` -- the effect size decides:

    improving    Theil-Sen slope > hi
    declining    slope < lo
    stable       neither, and the series' own sd < `sd_stable`
    fluctuating  neither, and sd >= `sd_stable`
    no_data      fewer than MIN_VALID_YEARS finite years, or Var(S) <= 0

    With `gate = None` it falls back to RECOVER's "Option A" significance rule,
    which additionally requires Mann-Kendall p < `alpha` and |slope| >
    `min_slope`. That rule is correctly calibrated and finds half as much real
    clearing at the same false-positive rate -- see the module docstring -- so it
    is kept for comparison rather than for use.

    RECOVER separated stable from fluctuating with a coefficient of variation
    (`cv < 0.3`) because it ran on raw GPP, which is strictly positive. The
    condition axis here is z-scored and therefore centred near zero, where CV
    explodes and carries no information -- so the gate is the standard
    deviation instead, in the same z-units as the slope. `cv` is still reported
    for continuity but should not be thresholded.
    """
    slope = feat["sen_slope"]
    if gate is None:
        sig = feat["mk_p"] < alpha
        up = sig & (slope > min_slope)
        down = sig & (slope < -min_slope)
    else:
        lo, hi = gate
        if not lo < hi:
            raise ValueError(
                f"gate must be (declining_below, improving_above) "
                f"with lo < hi, got {gate}"
            )
        up = slope > hi
        down = slope < lo
    flat = ~(up | down)

    out = pd.Series("fluctuating", index=feat.index, dtype=object)
    out[flat & (feat["sd"] < sd_stable)] = "stable"
    out[up] = "improving"
    out[down] = "declining"
    out[~np.isfinite(feat["mk_p"])] = "no_data"
    return out
