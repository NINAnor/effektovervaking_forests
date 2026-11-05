"""Scoring: the vendored DoR maths, applied per pixel rather than per stand.

`condition.core` holds the published per-site primitives, untouched. This module
is the part specific to this project: how a stand's pixels become probes, how
they are kept out of their own reference pools, and how the score is computed
for 1.5 M pixel-years without spending a week on it.

**Per pixel, not per polygon.** A stand is not in one condition. A
kalklindeskog with a spruce plantation over its northern half averages to
something that describes neither half, and the polygon mean was hiding exactly
the sub-stand degradation this project exists to find. So the probe is the
pixel; the polygon summary is then a distribution over its pixels -- median,
IQR, and the fraction below the threshold -- which says more than the mean
embedding's single score ever did.

**The score has to be a matmul.** `cosine_dists_to_set` walks one probe against
one pool. Called per pixel-year that is ~1.5 M x 2 scopes x a 500-row pool of
Python-level calls. Cosine distance to a set is
`1 - normalise(X) @ normalise(P).T`, so a whole stand-year scores against a
whole pool in a single BLAS call, and the k smallest come from `np.partition`
along the row (`knn_mean_unit`).

**The bootstrap resamples donors, not reference pixels.** The published
`bootstrap_ci` resamples the distance vector row by row. That is right when the
pool is 150 independent reference points and wrong here, where the pool is 2,000
pixels drawn from about 115 donor stands: sixty pixels of one stand are one
stand seen sixty times, and treating them as sixty references produces an
interval roughly a third too narrow. Measured on this AOI, switching to a
bootstrap over whole donor stands (and whole 500 m localities on the transformed
side) moves the share of stands whose interval straddles the threshold from 20%
to 32% -- so about one stand in eight was carrying a condition class the
reference sampling never supported (`condition/research/`, experiment E09).

**No per-pixel bootstrap, on purpose.** The paper's CI resamples the reference
pool, so it measures *reference-pool sampling error*. Every pixel of a stand
shares one pool, so that error is shared too: it is a property of the stand's
reference set, not of the pixel, and putting it on each pixel would dress a
common-mode uncertainty up as independent per-pixel precision. The bootstrap is
therefore run once per stand-year -- 737 x 9 calls, affordable -- and reported
as the stand's CI, while the genuine per-pixel spread is reported as the
quantiles of the pixel scores. Both are in the output; they mean different
things.

**The stand score is the median of its cell scores.** Until 2026-10 it was the
score of the stand's mean embedding; the two separate god from poor stands
equally well (stand AUC 0.836 against 0.829 at survey year, difference inside
its block-bootstrap interval) and the median is the number a reader can check
against the cell map. Its interval is `median_cluster_ci`: one cluster draw is
shared by every cell of the stand-year -- the pool error is common-mode, so
drawing per cell would shrink the interval by about 1/sqrt(n) -- each cell is
rescored under that draw, and the draw's stand value is the median over cells.
The mean-embedding score is still written, as `dor_<scope>_probe`.
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

# The vendored `classify` names its outcomes for the recovery paper. For forest
# condition the same three outcomes mean something else, so they are renamed at
# the boundary rather than by editing the vendored file.
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
    """Mean of the k smallest distances in each row.

    `np.partition` rather than a sort: only the k-element boundary matters, and
    at 500-column rows partitioning is roughly twice as fast as sorting.
    """
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

    Row chunks keep the similarity block in cache instead of materialising the
    full (n_probes, n_pool) distance matrix. The k values are summed in float64,
    so this agrees with the float32 numpy mean to ~1e-7, not bit for bit.

    `pool_t` is `np.ascontiguousarray(pool_u.T)`, already computed -- pass it
    when the same pool is scored against many probe batches (e.g. a stand's
    good pool, reused across its nine years) so the transpose isn't redone on
    every call. Left as None, it is computed here exactly as before.
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

    The probe is averaged in the RAW embedding and mapped afterwards, so the
    stand's probe is the same piece of ground whichever metric is in use; only
    the ruler changes.
    """
    x = np.asarray(probes, dtype=float).mean(axis=0)
    if transform is not None:
        x = np.asarray(transform(x.reshape(1, -1))[0], dtype=float)
    return x


# Distances carried per cell into the bootstrap walk. A cell whose draw runs
# off the end of this prefix is redone on its full sorted row, so this is a
# speed knob and never an approximation (tests/test_score.py pins it at T = k).
_CELL_PREFIX = 256
# Stand-years with more cells than this get their INTERVAL from this many cells
# picked at evenly spaced ranks of their point scores; the point value always
# uses every cell. Odd, so the picked set's median is the stand's median cell.
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

    `mult[b, c]` is how often cluster `c` was drawn in resample `b`, the same
    resample of whole clusters, as counts rather than indices.
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

    `probes_u` are the stand-year's cells through the metric and `unit_rows`;
    `cell_scores[scope]` their point scores; `good[scope]` is
    `(pool_t, owner)` -- the scope's good pool as unit rows transposed, and the
    donor stand of each row -- and `bad` the same for the transformed pool,
    with the 500 m locality of each row.

    One draw of donor stands and one of 500 m localities is shared by every cell
    in the resample: the pool error is common to the stand, and drawing it per
    cell would understate it by about 1/sqrt(n). The transformed side is drawn
    once and shared by both scopes, so it is walked once.

    Above `max_cells` cells the interval is computed on `max_cells` of them at
    evenly spaced ranks of the point scores and shifted by the gap between their
    point median and the stand's, which is zero to the cell nearest the median;
    the point value always uses every cell.
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

    The median and IQR, not the mean: a stand with a clearcut in one corner has
    a bimodal pixel distribution, and the mean sits in the empty middle of it.
    `frac_poor` -- the share of the stand's pixels below the threshold -- is the
    number that actually answers "how much of this stand is degraded".
    """
    # `agg` with a lambda per quantile runs that lambda once per group -- 6,600
    # groups x three quantiles of Python-level work over two million rows. The
    # grouped `quantile` is the same computation done once in pandas.
    g = pix.groupby(["poly_id", "year"], sort=False)[col]
    # The grouped `quantile` returns float64 where the per-group lambda returned
    # the column's own dtype, so it is cast back: these land in a parquet column
    # and the score is carried as float32 everywhere else.
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

    The RECOVER paper carried per-class thresholds calibrated on its own
    land-cover classes; those are meaningless here, because the pools mean
    something else. Instead we calibrate on the surveyors' own labels, using
    Youden's J (sensitivity + specificity - 1) over a grid.

    `scores` and `labels` must be aligned, with labels in {"good", "bad"}. The
    scores must come from the leave-one-out scoring -- a threshold fitted to
    self-inclusive scores is fitted to an artefact -- and both sides must be
    scored in the year their label was assigned, or the threshold is fitted to
    stale labels.

    Returns the threshold plus the J statistic and the two rates at it, so a
    caller can see whether the separation is worth thresholding at all: a J near
    0 means the pools do not separate and every site should be reported as
    `indistinguishable` rather than forced into a class.
    """
    m = labels.isin(["good", "bad"]) & np.isfinite(scores)
    s, y = scores[m].to_numpy(), (labels[m] == "good").to_numpy()
    if not y.any() or y.all():
        raise ValueError("need both labelled good and labelled bad polygons")
    if grid is None:
        grid = np.linspace(np.nanmin(s), np.nanmax(s), 401)

    # One pass over the sorted scores rather than a pass over the labels per grid
    # point. `threshold_interval_blocked` calls this ~24,000 times -- 40 blocks x
    # 300 resamples x 2 scopes -- so the 401-step Python loop was a minute of the
    # run on its own. Counting `pred & y` for every t at once is the same
    # arithmetic in the same order: `lo` is how many scores fall strictly below
    # t, so `n_good - cum_good[lo]` is exactly `(s >= t) & y`, and ties are
    # broken the same way because `argmax` takes the first maximum just as
    # `j > best` kept the first.
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

    The shipped threshold was fitted by Youden's J to every labelled stand and
    then applied to those same stands, which is a fit reported as a validation.
    With 36 bad stands that is worth a couple of points of J -- measured here,
    pooled J falls from 0.411 to 0.331 once the leakage is removed
    (`condition/research/`, experiment E01/E09).

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

    `classify_condition` compares a stand's interval with a threshold treated as
    known. It is not known: it is a Youden cut fitted on 36 degraded stands in
    six localities, and research E39 measured its error bar at about the width
    of the stand's own -- here it comes out slightly WIDER (median se 0.014
    against the stand's 0.018 at the deployed pool). Resampling whole spatial
    blocks of the training set is the same unit the threshold is held out by.

    **This is the label-sample half of the threshold's error, and only that.**
    The block resample runs at FIXED scores, so it misses the part that comes
    from which reference pixels were drawn. Research E43 measured both halves
    jointly and put the total at sd 0.0216 against this estimator's ~0.0165, and
    E45 showed that combining this understated term with the stand interval in
    quadrature reproduces only 83.5% of the joint bootstrap's verdicts -- almost
    always by deciding a stand the joint rule abstains on. So this is reported
    as a diagnostic, and nothing in the pipeline classifies with it.

    Returns `by_block` -> {lo, hi, se}, plus the pooled fallback for blocks that
    hold no labels.
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
