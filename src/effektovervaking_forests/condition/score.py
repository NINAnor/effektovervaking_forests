"""Scoring: the vendored DoR maths, applied per pixel rather than per stand.

`condition.core` holds the published per-site primitives, untouched; this module
turns a stand's pixels into probes, keeps them out of their own reference pools
and scores 1.5 M pixel-years. METHODS.md sections 4-6.

* The probe is the pixel; the stand summary is a distribution over its pixels.
* Cosine distance to a pool is one matmul, `1 - unit(X) @ unit(P).T`, with the k
  smallest taken by partition (`knn_mean_unit`).
* The bootstrap resamples donor stands (and 500 m localities on the transformed
  side), not reference pixels, and runs once per stand-year, not per pixel: the
  pool error is common to all pixels of a stand.
* The stand score is the median of its cell scores (`median_cluster_ci`); the
  mean-embedding score is still written as `dor_<scope>_probe`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from effektovervaking_forests.condition import _kernels
from effektovervaking_forests.condition.core import (
    classify,
)

__all__ = [
    "unit_rows",
    "calibrate_threshold_blocked",
    "knn_mean",
    "knn_mean_unit",
    "stand_probe",
    "summarise_pixels",
    "calibrate_threshold",
    "classify_condition",
    "threshold_interval_blocked",
    "CONDITION_CLASS",
]

EPS = 1e-12

# Rename the vendored `classify` outcomes (recovery vocabulary) at the boundary
# instead of editing the vendored file.
CONDITION_CLASS = {
    "regenerating": "good_condition",
    "degraded": "poor_condition",
    "indistinguishable": "indistinguishable",
    "no_data": "no_data",
}


def unit_rows(x: np.ndarray) -> np.ndarray:
    """L2-normalise each row, leaving all-zero rows at zero."""
    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x, axis=1, keepdims=True)
    return x / (n + EPS)


def knn_mean(d: np.ndarray, k: int) -> np.ndarray:
    """Mean of the k smallest distances in each row (NaN rows if fewer than k)."""
    if d.shape[1] < k:
        return np.full(d.shape[0], np.nan, dtype=np.float32)
    return np.partition(d, k - 1, axis=1)[:, :k].mean(axis=1)


def knn_mean_unit(
    probes_u: np.ndarray,
    pool_u: np.ndarray,
    k: int,
    chunk: int = 2048,
    *,
    pool_t: np.ndarray | None = None,
) -> np.ndarray:
    """`knn_mean` of cosine distances, for rows already through `unit_rows`.

    Row chunks keep the similarity block in cache. The k values are summed in
    float64, so this agrees with the float32 numpy mean to ~1e-7, not bit for bit.
    `pool_t` is an optional precomputed `np.ascontiguousarray(pool_u.T)` to reuse
    when one pool is scored against many probe batches.
    """
    n = len(probes_u)
    if len(pool_u) < k:
        return np.full(n, np.nan, dtype=np.float32)
    out = np.empty(n, dtype=np.float32)
    qt = np.ascontiguousarray(pool_u.T) if pool_t is None else pool_t
    for s in range(0, n, chunk):
        out[s : s + chunk] = _kernels.topk_mean_rows(probes_u[s : s + chunk] @ qt, k)
    return out


def stand_probe(probes: np.ndarray, transform=None) -> np.ndarray:
    """The stand's probe vector, in the metric the distances are taken in.

    Averaged in the RAW embedding, then mapped, so only the ruler changes.
    """
    x = np.asarray(probes, dtype=float).mean(axis=0)
    if transform is not None:
        x = np.asarray(transform(x.reshape(1, -1))[0], dtype=float)
    return x


# Distances carried per cell into the bootstrap walk. A cell whose draw runs
# off the end is redone on its full row, so this is a speed knob, not an
# approximation.
_CELL_PREFIX = 256
# Stand-years with more cells get their INTERVAL from this many cells at evenly
# spaced ranks (odd, so the median cell is included); the point uses all cells.
MEDIAN_MAX_CELLS = 2001


def _cell_prefix(
    probes_u: np.ndarray, pool_t: np.ndarray, own: np.ndarray, T: int
) -> tuple[np.ndarray, np.ndarray]:
    """(d, own) -- each probe's T smallest cosine distances, ascending, + owners."""
    d = (1.0 - probes_u @ pool_t).astype(np.float32)
    n_pool = d.shape[1]
    if T >= n_pool:
        idx = np.argsort(d, axis=1, kind="stable")
    else:
        idx = np.argpartition(d, T - 1, axis=1)[:, :T]
        idx = np.take_along_axis(
            idx, np.argsort(np.take_along_axis(d, idx, 1), axis=1, kind="stable"), 1
        )
    return (
        np.ascontiguousarray(np.take_along_axis(d, idx, 1)),
        np.ascontiguousarray(own[idx]),
    )


def _cluster_draws(
    cluster: np.ndarray, n_boot: int, rng: np.random.Generator
) -> tuple[np.ndarray, np.ndarray, int]:
    """(codes, mult, n_clusters): clusters drawn with replacement at their own count.

    `mult[b, c]` is how often cluster `c` was drawn in resample `b`.
    """
    u, codes = np.unique(cluster, return_inverse=True)
    c = len(u)
    idx = rng.integers(0, c, size=(n_boot, c), dtype=np.int32)
    rows = np.repeat(np.arange(n_boot, dtype=np.int64), c)
    mult = np.bincount(rows * c + idx.ravel(), minlength=n_boot * c)
    return (
        np.asarray(codes, dtype=np.int32).ravel(),
        mult.reshape(n_boot, c).astype(np.int32),
        c,
    )


def boot_knn_cells(
    probes_u: np.ndarray,
    pool_t: np.ndarray,
    codes: np.ndarray,
    mult: np.ndarray,
    k: int,
    chunk: int = 4096,
    prefix: int | None = None,
) -> np.ndarray:
    """(n_cells, n_boot) kNN mean of each cell against each resampled pool.

    Exact: a cell whose prefix cannot settle every draw is walked again over its
    whole sorted row. Draws holding fewer than k references in all come back NaN.
    """
    T = _CELL_PREFIX if prefix is None else prefix
    n, n_boot = len(probes_u), mult.shape[0]
    out = np.full((n, n_boot), np.nan, dtype=np.float32)
    if not n or pool_t.shape[1] < k:
        return out
    walk = _kernels.boot_walk_cells
    for s in range(0, n, chunk):
        pu = probes_u[s : s + chunk]
        d, own = _cell_prefix(pu, pool_t, codes, T)
        o, ok = walk(d, own, mult, k)
        if not ok.all() and T < pool_t.shape[1]:
            bad = np.flatnonzero(~ok)
            d, own = _cell_prefix(pu[bad], pool_t, codes, pool_t.shape[1])
            o[bad] = walk(d, own, mult, k)[0]
        out[s : s + chunk] = o
    return out


def _rank_pick(scores: np.ndarray, m: int) -> np.ndarray:
    """At most `m` finite cells at evenly spaced ranks of `scores`.

    All finite cells when there are no more than `m` of them; otherwise `m`
    (odd) of them including the cell at the median rank.
    """
    fin = np.flatnonzero(np.isfinite(scores))
    order = fin[np.argsort(scores[fin], kind="stable")]
    if len(order) <= m:
        return order
    return order[np.round(np.linspace(0, len(order) - 1, m)).astype(np.int64)]


def median_cluster_ci(
    probes_u: np.ndarray,
    cell_scores: dict,
    good: dict,
    bad: tuple,
    k: int,
    n_boot: int,
    rng: np.random.Generator,
    max_cells: int = MEDIAN_MAX_CELLS,
) -> dict:
    """{scope: (point, lo, hi)} -- the stand's median cell score and its interval.

    `probes_u`: the stand-year's cells through the metric and `unit_rows`.
    `cell_scores[scope]`: their point scores. `good[scope]` is `(pool_t, owner)`
    (unit rows transposed, donor stand per row); `bad` is the same for the
    transformed pool with the 500 m locality per row.

    One draw of donor stands and of localities is shared by every cell of the
    resample (the pool error is common to the stand). The transformed side is
    drawn once and shared by both scopes.

    Above `max_cells` cells the interval is computed on `max_cells` of them at
    evenly spaced ranks of the point scores, shifted by the gap between their
    median and the stand's; the point value always uses every cell.
    """
    points = {
        s: float(np.nanmedian(v)) if np.isfinite(v).any() else np.nan
        for s, v in cell_scores.items()
    }
    out = {s: (points[s], np.nan, np.nan) for s in cell_scores}
    live = [
        s
        for s in cell_scores
        if np.isfinite(points[s])
        and good[s][0].shape[1] >= k
        and len(np.unique(good[s][1])) >= 2
    ]
    bad_t, bad_cluster = bad
    if not live or bad_t.shape[1] < k or len(np.unique(bad_cluster)) < 2:
        return out
    # Each scope ranks its cells by its own scores; the transformed side is
    # walked once, on the union of the picks.
    picks = {s: _rank_pick(cell_scores[s], max_cells) for s in live}
    cells, inv = np.unique(np.concatenate(list(picks.values())), return_inverse=True)
    inv = np.asarray(inv).ravel()
    at, start = {}, 0
    for s in live:
        at[s] = inv[start : start + len(picks[s])]
        start += len(picks[s])
    pu = np.ascontiguousarray(probes_u[cells])
    bcodes, bmult, _ = _cluster_draws(bad_cluster, n_boot, rng)
    mb_all = boot_knn_cells(pu, bad_t, bcodes, bmult, k)
    for s in live:
        pool_t, owner = good[s]
        gcodes, gmult, _ = _cluster_draws(owner, n_boot, rng)
        pus = pu[at[s]] if len(at[s]) < len(pu) else pu
        mb = mb_all[at[s]]
        mg = boot_knn_cells(pus, pool_t, gcodes, gmult, k)
        with np.errstate(invalid="ignore"):
            med = np.median(mb / (mg + mb + EPS), axis=0)
        boots = med[np.isfinite(med)]
        if len(boots) < max(20, n_boot // 20):
            continue
        shift = points[s] - float(np.median(cell_scores[s][picks[s]]))
        lo, hi = np.percentile(boots, [2.5, 97.5]) + shift
        out[s] = (points[s], float(np.clip(lo, 0.0, 1.0)), float(np.clip(hi, 0.0, 1.0)))
    return out


def summarise_pixels(
    pix: pd.DataFrame, col: str, threshold: float | None = None
) -> pd.DataFrame:
    """Collapse per-pixel scores to a per-(stand, year) distribution.

    Median and IQR, not the mean (a stand with one clearcut corner is bimodal);
    `frac_poor` is the share of the stand's pixels below the threshold.
    """
    g = pix.groupby(["poly_id", "year"], sort=False)[col]
    # Grouped `quantile` instead of `agg` with a lambda per quantile (far
    # faster at millions of rows). It returns float64, so cast back to the
    # column dtype (float32 in the parquet).
    dt = pix[col].dtype
    out = pd.DataFrame(
        {
            "n_pixels": g.size(),
            "median": g.median(),
            "p25": g.quantile(0.25).astype(dt),
            "p75": g.quantile(0.75).astype(dt),
            "p10": g.quantile(0.10).astype(dt),
        }
    )
    if threshold is not None:
        below = (
            pix[col]
            .lt(threshold)
            .groupby([pix["poly_id"], pix["year"]], sort=False)
            .mean()
        )
        out["frac_poor"] = below.reindex(out.index).astype(float)
    return out.add_prefix(f"{col}_").reset_index()


def calibrate_threshold(
    scores: pd.Series, labels: pd.Series, grid: np.ndarray | None = None
) -> dict:
    """Pick the DoR threshold that best separates labelled good from labelled bad.

    Maximises Youden's J over a grid. `scores` and `labels` must be aligned, with
    labels in {"good", "bad"}, scored leave-one-out and in the year the label was
    assigned. Returns the threshold plus J and the two rates at it; J near 0
    means the pools do not separate and everything should be `indistinguishable`.
    """
    m = labels.isin(["good", "bad"]) & np.isfinite(scores)
    s, y = scores[m].to_numpy(), (labels[m] == "good").to_numpy()
    if not y.any() or y.all():
        raise ValueError("need both labelled good and labelled bad polygons")
    if grid is None:
        grid = np.linspace(np.nanmin(s), np.nanmax(s), 401)

    # One pass over the sorted scores instead of one per grid point: this runs
    # ~24,000 times in `threshold_interval_blocked`. Same arithmetic in the same
    # order: `lo` counts scores strictly below t, so `n_good - cum_good[lo]` is
    # `(s >= t) & y`, and `argmax` keeps the first maximum on ties.
    order = np.argsort(s, kind="stable")
    s_sorted, y_sorted = s[order], y[order]
    cum_good = np.concatenate(([0], np.cumsum(y_sorted)))
    cum_bad = np.concatenate(([0], np.cumsum(~y_sorted)))
    lo = np.searchsorted(s_sorted, grid, side="left")
    tpr = (cum_good[-1] - cum_good[lo]) / cum_good[-1]
    tnr = cum_bad[lo] / cum_bad[-1]
    j = tpr + tnr - 1.0
    i = int(np.argmax(j))
    return {
        "threshold": float(grid[i]),
        "youden_j": float(j[i]),
        "sensitivity": float(tpr[i]),
        "specificity": float(tnr[i]),
        "n_good": int(cum_good[-1]),
        "n_bad": int(cum_bad[-1]),
    }


def calibrate_threshold_blocked(
    scores: pd.Series,
    labels: pd.Series,
    blocks: pd.Series,
    grid: np.ndarray | None = None,
) -> dict:
    """A threshold per spatial block, each fitted on the stands OUTSIDE that block.

    Avoids reporting a fit on the labelled stands as a validation on them.
    Returns the pooled threshold (for stands whose block holds no labels, and
    for reporting) plus `by_block`, which is what the classification should use.
    """
    m = labels.isin(["good", "bad"]) & np.isfinite(scores)
    s, y, b = scores[m], labels[m] == "good", blocks[m]
    pooled = calibrate_threshold(scores, labels, grid)
    by_block = {}
    for blk in blocks.dropna().unique():
        keep = b != blk
        if keep.sum() < 10 or y[keep].nunique() < 2:
            by_block[blk] = pooled["threshold"]
            continue
        by_block[blk] = calibrate_threshold(s[keep], labels[m][keep], grid)["threshold"]
    return {
        **pooled,
        "by_block": {str(k): float(v) for k, v in by_block.items()},
        "n_blocks": len(by_block),
    }


def classify_condition(row, threshold: float) -> str:
    """Vendored CI-vs-threshold rule, renamed into condition vocabulary."""
    return CONDITION_CLASS[classify(row["dor"], row["ci_lo"], row["ci_hi"], threshold)]


Z95 = 1.959963985


def threshold_interval_blocked(
    scores: pd.Series,
    labels: pd.Series,
    blocks: pd.Series,
    n_boot: int = 300,
    seed: int = 42,
    grid: np.ndarray | None = None,
) -> dict:
    """A block-bootstrap interval on each block's leave-block-out threshold.

    The label-sample half of the threshold's error only: the resample runs at
    FIXED scores, so it misses the part from which reference pixels were drawn.
    Reported as a diagnostic; nothing in the pipeline classifies with it
    (METHODS.md section 7). Returns `by_block` -> {lo, hi, se}, plus the pooled
    fallback for blocks that hold no labels.
    """
    m = labels.isin(["good", "bad"]) & np.isfinite(scores)
    s, y, b = scores[m], labels[m], blocks[m]
    rng = np.random.default_rng(seed)
    uniq = pd.unique(b.dropna())
    out, pooled = {}, []
    for blk in list(uniq) + [None]:
        keep = b != blk if blk is not None else pd.Series(True, index=b.index)
        tr_b = b[keep]
        groups = pd.unique(tr_b.dropna())
        if len(groups) < 3 or labels[m][keep].nunique() < 2:
            continue
        by = {g: np.flatnonzero((tr_b == g).to_numpy()) for g in groups}
        s_tr, l_tr = (
            s[keep].reset_index(drop=True),
            labels[m][keep].reset_index(drop=True),
        )
        draws = []
        for _ in range(n_boot):
            pick = rng.choice(groups, len(groups), replace=True)
            idx = np.concatenate([by[g] for g in pick])
            if l_tr.iloc[idx].nunique() < 2:
                continue
            draws.append(
                calibrate_threshold(s_tr.iloc[idx], l_tr.iloc[idx], grid)["threshold"]
            )
        if len(draws) < 20:
            continue
        d = np.asarray(draws, float)
        rec = {
            "lo": float(np.nanpercentile(d, 2.5)),
            "hi": float(np.nanpercentile(d, 97.5)),
            "se": float(np.nanstd(d)),
        }
        if blk is None:
            pooled = rec
        else:
            out[str(blk)] = rec
    return {
        "by_block": out,
        "pooled": pooled or {"lo": np.nan, "hi": np.nan, "se": np.nan},
        "n_boot": n_boot,
    }
