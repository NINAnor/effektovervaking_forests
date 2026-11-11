"""Degree-of-Recovery scoring primitives.

VENDORED VERBATIM from https://github.com/Geethen/DegreeofRecovery ->
`paper_release/src/degree_of_recovery/core.py`, itself a verbatim port of that
repo's v3/v4 scorers. Kept byte-identical below this docstring so the condition
scores here stay directly comparable to the published RECOVER ones; if upstream
changes, re-copy the file rather than editing it.

These functions operate on 1-D distance vectors `d_g` (distances to the "good"
reference pool) and `d_b` (distances to the "bad" pool) for a single probe:

    dor = mean_cos(x -> k nearest bad) / (mean_cos(x -> k nearest good)
                                          + mean_cos(x -> k nearest bad))

Direction: a score near 1 means the probe sits FAR from the bad pool and CLOSE
to the good pool -- for this project, good condition.

For this project the pools are re-defined for forest condition rather than
land-cover reversion (see `condition.refs` and `docs/METHODS.md`), but the
score, its bootstrap and its CI-vs-threshold classification are unchanged.
"""
from __future__ import annotations

from typing import Callable

import numpy as np

EPS = 1e-12


def cosine_dist(a: np.ndarray, b: np.ndarray, eps: float = EPS) -> float:
    """Cosine distance between two 1-D vectors."""
    return 1.0 - float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + eps))


def cosine_dists_to_set(
    x: np.ndarray, points: np.ndarray, eps: float = EPS
) -> np.ndarray:
    """Cosine distance from `x` to every row in `points` (vectorised)."""
    nx = np.linalg.norm(x) + eps
    np_pts = np.linalg.norm(points, axis=1) + eps
    return 1.0 - (points @ x) / (np_pts * nx)


def knn_score(d_g: np.ndarray, d_b: np.ndarray, k: int) -> float:
    """kNN-margin DoR: mean of k smallest distances to each cloud, ratio."""
    if len(d_g) < k or len(d_b) < k:
        return float("nan")
    m_g = float(np.mean(np.partition(d_g, k - 1)[:k]))
    m_b = float(np.mean(np.partition(d_b, k - 1)[:k]))
    return m_b / (m_g + m_b + EPS)


def median_score(d_g: np.ndarray, d_b: np.ndarray) -> float:
    """Median-pairwise DoR."""
    return float(np.median(d_b) / (np.median(d_g) + np.median(d_b) + EPS))


def bootstrap_ci(
    score_fn: Callable[[np.ndarray, np.ndarray], float],
    d_g: np.ndarray,
    d_b: np.ndarray,
    n_boot: int,
    rng: np.random.Generator,
) -> tuple[float, float, float]:
    """Bootstrap a (point, ci_lo, ci_hi) triple for any (d_g, d_b) -> float scorer.

    Resamples each class with replacement (preserving its size) `n_boot`
    times and reports the 2.5/97.5 percentile via `np.nanpercentile`.
    """
    point = score_fn(d_g, d_b)
    if not np.isfinite(point):
        return float("nan"), float("nan"), float("nan")
    n_g, n_b = len(d_g), len(d_b)
    ig = rng.integers(0, n_g, size=(n_boot, n_g))
    ib = rng.integers(0, n_b, size=(n_boot, n_b))
    boots = np.array([score_fn(d_g[ig[i]], d_b[ib[i]]) for i in range(n_boot)])
    return (
        float(point),
        float(np.nanpercentile(boots, 2.5)),
        float(np.nanpercentile(boots, 97.5)),
    )


def classify(score: float, ci_lo: float, ci_hi: float, threshold: float) -> str:
    """Classify a single site against a single threshold via its CI."""
    if not (np.isfinite(score) and np.isfinite(ci_lo) and np.isfinite(ci_hi)):
        return "no_data"
    if ci_lo > threshold:
        return "regenerating"
    if ci_hi < threshold:
        return "degraded"
    return "indistinguishable"
