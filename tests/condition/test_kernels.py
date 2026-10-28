"""The compiled paths agree with the numpy paths they replace."""

import numpy as np
import pandas as pd
import pytest

from effektovervaking_forests.condition import _kernels, score
from effektovervaking_forests.condition.refs import _index_embeddings, _rows_for

pytestmark = pytest.mark.skipif(not _kernels.AVAILABLE, reason="numba not installed")


@pytest.mark.parametrize("c", [32, 1300, 2045])
def test_int32_draws_match_int64_stream(c):
    a, b = np.random.default_rng(7), np.random.default_rng(7)
    assert np.array_equal(
        a.integers(0, c, size=(50, c)), b.integers(0, c, size=(50, c), dtype=np.int32)
    )
    assert a.random() == b.random()


@pytest.mark.parametrize("c,pad", [(32, 0), (400, 0), (2045, 24)])
def test_boot_kernel_bit_identical(monkeypatch, c, pad):
    rng = np.random.default_rng(c)
    k = 25
    tbl = np.sort(rng.random((c, k)), axis=1)
    if pad:
        tbl[:, k - pad :] = np.inf
    fast = score._boot_knn_mean(tbl, 500, k, np.random.default_rng(1))
    monkeypatch.setattr(_kernels, "AVAILABLE", False)
    slow = score._boot_knn_mean(tbl, 500, k, np.random.default_rng(1))
    assert np.array_equal(fast, slow, equal_nan=True)


def test_boot_kernel_serial_build_matches():
    rng = np.random.default_rng(3)
    tbl = np.sort(rng.random((300, 25)), axis=1)
    par = score._boot_knn_mean(tbl, 400, 25, np.random.default_rng(1))
    old = (
        _kernels.boot_prefix,
        _kernels.topk_mean_rows,
        _kernels.boot_counts,
        _kernels.boot_walk_cells,
    )
    try:
        _kernels.use_serial()
        ser = score._boot_knn_mean(tbl, 400, 25, np.random.default_rng(1))
    finally:
        (
            _kernels.boot_prefix,
            _kernels.topk_mean_rows,
            _kernels.boot_counts,
            _kernels.boot_walk_cells,
        ) = old
    assert np.array_equal(par, ser)


def test_knn_mean_unit_matches_matrix_path():
    rng = np.random.default_rng(0)
    p = rng.standard_normal((3000, 64)).astype(np.float32)
    q = rng.standard_normal((700, 64)).astype(np.float32)
    ref = score.knn_mean(score.cosine_dist_matrix(p, q), 25)
    got = score.knn_mean_unit(score.unit_rows(p), score.unit_rows(q), 25, chunk=1000)
    assert got.dtype == np.float32
    assert np.max(np.abs(ref - got)) < 1e-6
    assert np.isnan(
        score.knn_mean_unit(score.unit_rows(p), score.unit_rows(q[:10]), 25)
    ).all()


def test_rows_for_matches_dict_semantics():
    px = pd.DataFrame(
        {"pixel_id": [5, 3, 5, 9, 3, 5], "year": [2020, 2020, 2021, 2020, 2020, 2020]}
    )
    for b in range(64):
        px[f"A{b:02d}"] = 0.0
    from effektovervaking_forests.condition import refs

    px = px.rename(columns=dict(zip([f"A{b:02d}" for b in range(64)], refs.BANDS)))
    _, index = _index_embeddings(px)
    ref = {k: i for i, k in enumerate(zip(px["pixel_id"], px["year"]))}
    q = np.array([9, 5, 7, 3, 5])
    want = [ref[(int(p), 2020)] for p in q if (int(p), 2020) in ref]
    assert _rows_for(index, q, 2020).tolist() == want  # duplicates -> last row
    assert _rows_for(index, q, 2021).tolist() == [2, 2]
    assert _rows_for(index, q, 1999).tolist() == []


def _ks(a, b):
    """Two-sample Kolmogorov-Smirnov statistic, NaN/inf-free inputs."""
    a, b = np.sort(a), np.sort(b)
    grid = np.concatenate([a, b])
    return np.max(
        np.abs(
            np.searchsorted(a, grid, "right") / len(a)
            - np.searchsorted(b, grid, "right") / len(b)
        )
    )


@pytest.mark.parametrize("c", [12, 300, 1500])
def test_thinned_kernel_matches_numpy_walk(monkeypatch, c):
    tbl = np.sort(np.random.default_rng(c).random((c, 25)), axis=1)
    fast = score._boot_knn_mean(tbl, 500, 25, np.random.default_rng(1), thinned=True)
    monkeypatch.setattr(_kernels, "AVAILABLE", False)
    slow = score._boot_knn_mean(tbl, 500, 25, np.random.default_rng(1), thinned=True)
    assert np.array_equal(fast, slow, equal_nan=True)


def test_thinned_serial_build_matches():
    tbl = np.sort(np.random.default_rng(3).random((900, 25)), axis=1)
    par = score._boot_knn_mean(tbl, 400, 25, np.random.default_rng(1), thinned=True)
    old = _kernels.boot_prefix, _kernels.topk_mean_rows, _kernels.boot_counts
    try:
        _kernels.use_serial()
        ser = score._boot_knn_mean(tbl, 400, 25, np.random.default_rng(1), thinned=True)
    finally:
        _kernels.boot_prefix, _kernels.topk_mean_rows, _kernels.boot_counts = old
    assert np.array_equal(par, ser)


@pytest.mark.parametrize("c", [40, 1300])
def test_thinned_draw_has_the_full_draws_distribution(c):
    # Clusters of unequal quality, so which ones are drawn matters.
    rng = np.random.default_rng(c)
    tbl = np.sort(rng.gamma(2.0, 0.05, (c, 25)) + rng.random((c, 1)) * 0.2, axis=1)
    n = 20_000
    full = score._boot_knn_mean(tbl, n, 25, np.random.default_rng(1))
    thin = score._boot_knn_mean(tbl, n, 25, np.random.default_rng(2), thinned=True)
    full2 = score._boot_knn_mean(tbl, n, 25, np.random.default_rng(3))
    # Same law: the thinned-vs-full distance is of the order of full-vs-full.
    assert _ks(full, thin) < 0.025
    assert _ks(full, full2) < 0.025
    q = [2.5, 50, 97.5]
    np.testing.assert_allclose(
        np.percentile(thin, q),
        np.percentile(full, q),
        atol=3 * np.max(np.abs(np.percentile(full2, q) - np.percentile(full, q)))
        + 1e-4,
    )


def test_thinned_fallback_has_the_full_draws_distribution(monkeypatch):
    # The fallback is rare by design, so force it: report every resample as
    # unsettled, which sends each one's rebuilt full draw to the exact path.
    real = _kernels.boot_counts

    def unsettled(*a):
        out, ok = real(*a)
        return out, np.zeros_like(ok)

    monkeypatch.setattr(_kernels, "boot_counts", unsettled)
    rng = np.random.default_rng(7)
    tbl = np.sort(rng.gamma(2.0, 0.05, (400, 25)) + rng.random((400, 1)) * 0.2, axis=1)
    tbl[::3, 5:] = np.inf  # short clusters too
    n = 20_000
    full = score._boot_knn_mean(tbl, n, 25, np.random.default_rng(1))
    thin = score._boot_knn_mean(tbl, n, 25, np.random.default_rng(2), thinned=True)
    full2 = score._boot_knn_mean(tbl, n, 25, np.random.default_rng(3))
    assert _ks(full, thin) < 0.025 and _ks(full, full2) < 0.025
