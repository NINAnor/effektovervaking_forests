"""Compiled inner loops for `condition.score` (numba).

Compiled per process (no on-disk cache: it needs a writable source tree). Each
kernel reproduces the numpy expression it replaces operation for operation;
`tests/condition/test_score.py` checks them against numpy references.
"""

import numpy as np
from numba import njit, prange

__all__ = ["boot_walk_cells", "topk_mean_rows"]


@njit(parallel=True, nogil=True)
def boot_walk_cells(d, own, mult, k):
    """Per cell and draw: the kNN mean of the resampled pool, by a prefix walk.

    `d` (n_cells, T) holds each cell's T smallest distances, ascending, and
    `own` the cluster each came from; `mult` (n_boot, n_clusters) is how
    often each cluster was drawn. A drawn-twice cluster contributes its
    references twice, so walking the sorted distances and counting each by
    its cluster's multiplicity until k are reached is the kNN mean of the
    resampled pool exactly -- whenever the prefix is long enough. Returns
    (out (n_cells, n_boot) float32, ok (n_cells,) False where some draw ran
    off the end of the prefix; the caller redoes those cells on full rows).
    """
    n, T = d.shape
    n_boot = mult.shape[0]
    out = np.empty((n, n_boot), dtype=np.float32)
    ok = np.ones(n, dtype=np.bool_)
    for i in prange(n):
        for b in range(n_boot):
            cum = 0
            s = 0.0
            hit = False
            for t in range(T):
                w = mult[b, own[i, t]]
                if w == 0:
                    continue
                if cum + w >= k:
                    out[i, b] = np.float32((s + (k - cum) * d[i, t]) / k)
                    hit = True
                    break
                cum += w
                s += w * d[i, t]
            if not hit:
                out[i, b] = np.nan
                ok[i] = False
    return out, ok


@njit(parallel=True, nogil=True)
def topk_mean_rows(g, k):
    """Mean of `1 - g` over each row's k largest `g` (= k smallest distances).

    Selection by a k-slot insertion buffer, then the k values summed in
    ascending-distance order in float64 and cast back to float32.
    """
    n, m = g.shape
    out = np.empty(n, dtype=np.float32)
    for i in prange(n):
        buf = np.empty(k, dtype=np.float32)  # descending g
        for j in range(k):
            buf[j] = -np.inf
        for j in range(m):
            v = g[i, j]
            if v > buf[k - 1]:
                p = k - 1
                while p > 0 and buf[p - 1] < v:
                    buf[p] = buf[p - 1]
                    p -= 1
                buf[p] = v
        acc = 0.0
        for j in range(k):
            acc += np.float32(1.0) - buf[j]
        out[i] = np.float32(acc / k)
    return out
