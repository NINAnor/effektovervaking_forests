"""Compiled inner loops for `condition.score`, with the arithmetic order kept.

numba is optional: `AVAILABLE` is False without it and `score` keeps its numpy
paths. Each kernel reproduces the numpy expression it replaces operation for
operation, so the bootstrap output is bit-identical (tests/test_kernels.py).
"""

from __future__ import annotations

import os
import types
from pathlib import Path

import numpy as np

# numba caches compiled kernels next to the source by default, and on the P:
# network share its writability probe fails intermittently, which makes the
# import itself raise. A local cache dir is read only at numba's import, so set
# it before that, unless the caller chose one.
os.environ.setdefault("NUMBA_CACHE_DIR", str(Path.home() / ".cache" / "numba"))

try:
    from numba import njit, prange

    AVAILABLE = True
except ImportError:  # pragma: no cover
    AVAILABLE = False

__all__ = [
    "AVAILABLE",
    "boot_counts",
    "boot_prefix",
    "boot_walk_cells",
    "topk_mean_rows",
    "use_serial",
]


def use_serial() -> None:
    """Swap in single-threaded builds, for callers that parallelise across stands."""
    global boot_counts, boot_prefix, boot_walk_cells, topk_mean_rows
    if AVAILABLE:
        boot_prefix, topk_mean_rows = _boot_prefix_serial, _topk_mean_rows_serial
        boot_counts = _boot_counts_serial
        boot_walk_cells = _boot_walk_cells_serial


if AVAILABLE:

    def _boot_prefix(idx, remap, Lr, V, k, n_ids):
        """Per draw: multiplicities of the prefix clusters, then walk the prefix.

        Same sums in the same order as `_boot_knn_mean`'s cumsum formulation:
        `s` is `cumsum(w * V)` and `cum` is `cumsum(w)`, left to right.
        Returns (out, ok); `ok` False where the prefix cannot reach k.
        """
        n_boot, c = idx.shape
        T = V.shape[0]
        out = np.full(n_boot, np.nan)
        ok = np.zeros(n_boot, dtype=np.bool_)
        for b in prange(n_boot):
            mult = np.zeros(n_ids, dtype=np.int64)
            for j in range(c):
                r = remap[idx[b, j]]
                if r >= 0:
                    mult[r] += 1
            cum = 0
            s = 0.0
            for t in range(T):
                w = mult[Lr[t]]
                if cum + w >= k:
                    out[b] = (s + (k - cum) * V[t]) / k
                    ok[b] = True
                    break
                cum += w
                s += w * V[t]
        return out, ok

    def _boot_counts(hits, picks, Lr, V, k, n_ids):
        """`_boot_prefix` for the thinned draw: draw b's prefix-cluster picks are
        `picks[starts[b]:starts[b] + hits[b]]`, ids in [0, n_ids).

        The walk is `_boot_prefix`'s, line for line, so for the same
        multiplicities it returns the same bits.
        """
        n_boot = hits.shape[0]
        T = V.shape[0]
        starts = np.zeros(n_boot + 1, dtype=np.int64)
        for b in range(n_boot):
            starts[b + 1] = starts[b] + hits[b]
        out = np.full(n_boot, np.nan)
        ok = np.zeros(n_boot, dtype=np.bool_)
        for b in prange(n_boot):
            mult = np.zeros(n_ids, dtype=np.int64)
            for j in range(starts[b], starts[b + 1]):
                mult[picks[j]] += 1
            cum = 0
            s = 0.0
            for t in range(T):
                w = mult[Lr[t]]
                if cum + w >= k:
                    out[b] = (s + (k - cum) * V[t]) / k
                    ok[b] = True
                    break
                cum += w
                s += w * V[t]
        return out, ok

    def _boot_walk_cells(d, own, mult, k):
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

    def _topk_mean_rows(g, k):
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

    boot_prefix = njit(parallel=True, nogil=True, cache=True)(_boot_prefix)
    topk_mean_rows = njit(parallel=True, nogil=True, cache=True)(_topk_mean_rows)
    boot_counts = njit(parallel=True, nogil=True, cache=True)(_boot_counts)
    boot_walk_cells = njit(parallel=True, nogil=True, cache=True)(_boot_walk_cells)

    def _serial(f):
        """`f` under its own name, so its serial build gets its own cache file.

        numba keys a cached kernel by signature and bytecode, not by
        `parallel=`, and names the file after the function: the parallel and
        serial builds of one function overwrote each other on disk, so a
        worker process could load the 8-thread binary (oversubscribing) or the
        main process the serial one.
        """
        g = types.FunctionType(
            f.__code__,
            f.__globals__,
            f.__name__ + "_serial",
            f.__defaults__,
            f.__closure__,
        )
        g.__qualname__ = f.__qualname__ + "_serial"
        return njit(nogil=True, cache=True)(g)

    _boot_walk_cells_serial = _serial(_boot_walk_cells)
    _boot_prefix_serial = _serial(_boot_prefix)
    _boot_counts_serial = _serial(_boot_counts)
    _topk_mean_rows_serial = _serial(_topk_mean_rows)
