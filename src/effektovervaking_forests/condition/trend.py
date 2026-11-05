"""Trajectory classification: z-score -> Theil-Sen slope -> Mann-Kendall.

Run on an annual scalar derived from the embedding. All pairs (i<j) are
computed at once (`np.triu_indices`); Var(S) carries the tie correction and
pairs touching a NaN are excluded. The class rule gates the Theil-Sen slope
(effect size), calibrated on undisturbed pixels, not the p-value; `mk_p` is
still reported. METHODS.md section 8.
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

# Percentile of the undisturbed slope distribution the gate is cut at (each side).
GATE_PCTL_DEFAULT = 2.0

TREND_CLASSES = ("improving", "declining", "stable", "fluctuating", "no_data")

# Minimum finite years before a row is testable.
MIN_VALID_YEARS = 6


def zscore_by_group(
    values: np.ndarray, groups: np.ndarray, eps: float = 1e-9
) -> np.ndarray:
    """Z-score each row against the pooled mean/sd of its group.

    `values` is (B, T), `groups` is (B,); mean and sd are over all cells of the
    group (every pixel, every year).
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

    Processed in chunks to bound the (B, T*(T-1)/2) pair array. Returns one row
    per input row: sen_slope (units of `y` per step), mk_s, mk_z (tie-corrected
    normal approximation), mk_p (two-sided), n_valid, mean, sd, cv, autocorr_lag1.
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
    # Local import: scipy is only needed for the normal survival function.
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
    """sum over tie groups of t(t-1)(2t+5), per row (Var(S) tie correction)."""
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
    """(declining_below, improving_above) from slopes of UNDISTURBED pixels.

    Cuts at the `pctl` and `100 - pctl` percentiles of reference pixels with no
    Hansen loss, fixing each class's false-positive rate at `pctl`%. "No loss"
    is not "intact", so the rate is an upper bound and recall a lower bound.
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

    With `gate = (lo, hi)` from `calibrate_slope_gate` (the shipped path) the
    effect size decides: slope > hi improving, slope < lo declining, otherwise
    stable if the series' sd < `sd_stable` else fluctuating. `no_data` if fewer
    than MIN_VALID_YEARS finite years or Var(S) <= 0.

    With `gate = None`, falls back to the significance rule (Mann-Kendall
    p < `alpha` and |slope| > `min_slope`), kept for comparison. Stable vs
    fluctuating uses sd, not CV: the z-scored series is centred near zero where
    CV is meaningless.
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


def annual_matrix(
    df: pd.DataFrame,
    value: str,
    years: list[int],
    keys: list[str],
) -> tuple[pd.DataFrame, np.ndarray]:
    """Pivot long (keys, year, value) rows into a dense (n, len(years)) matrix.

    Returns the key frame (matrix row order) and the matrix; missing years are NaN.
    """
    wide = (
        df.pivot_table(index=keys, columns="year", values=value, aggfunc="mean")
        .reindex(columns=years)
        .reset_index()
    )
    mat = wide[years].to_numpy(dtype=float)
    return wide[keys].copy(), mat
