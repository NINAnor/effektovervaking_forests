"""The condition step: direction of the score, the vectorised path, self-scoring."""

import numpy as np
import pandas as pd
import pytest

from effektovervaking_forests.condition.core import (
    classify,
    cosine_dists_to_set,
    knn_score,
)
from effektovervaking_forests.condition.score import (
    _boot_knn_mean,
    _cluster_knn_table,
    calibrate_threshold,
    cosine_dist_matrix,
    knn_mean,
    score_matrix,
    score_stand_year,
    summarise_pixels,
)


def _cloud(centre, n, rng, spread=0.05):
    return rng.normal(centre, spread, (n, 64))


def test_dor_is_high_near_the_good_pool_and_low_near_the_bad_pool():
    rng = np.random.default_rng(0)
    good = _cloud(np.r_[1.0, np.zeros(63)], 50, rng)
    bad = _cloud(np.r_[-1.0, np.zeros(63)], 50, rng)
    near_good = score_stand_year(good, good, bad, n_boot=200, rng=rng)
    near_bad = score_stand_year(bad, good, bad, n_boot=200, rng=rng)
    assert near_good["dor"] > 0.9
    assert near_bad["dor"] < 0.1
    assert near_good["ci_lo"] <= near_good["dor"] <= near_good["ci_hi"]


def test_the_vectorised_score_matches_the_vendored_scalar_path():
    """The whole per-pixel design rests on this being the same maths."""
    rng = np.random.default_rng(0)
    probes = rng.normal(size=(64, 64))
    good = _cloud(np.r_[1.0, np.zeros(63)], 120, rng)
    bad = _cloud(np.r_[-1.0, np.zeros(63)], 90, rng)

    dor, m_g, m_b = score_matrix(probes, good, bad, k=5)
    want = np.array(
        [
            knn_score(cosine_dists_to_set(x, good), cosine_dists_to_set(x, bad), 5)
            for x in probes
        ]
    )
    assert np.allclose(dor, want, atol=1e-6)
    assert np.all((m_b / (m_g + m_b)) == pytest.approx(dor, abs=1e-6))


def test_cosine_dist_matrix_matches_the_vendored_per_probe_distances():
    rng = np.random.default_rng(1)
    probes, pool = rng.normal(size=(7, 64)), rng.normal(size=(11, 64))
    got = cosine_dist_matrix(probes, pool)
    want = np.array([cosine_dists_to_set(x, pool) for x in probes])
    assert np.allclose(got, want, atol=1e-6)


def test_knn_mean_takes_the_k_smallest_not_the_first_k():
    d = np.array([[9.0, 1.0, 8.0, 2.0, 7.0]])
    assert knn_mean(d, 2)[0] == pytest.approx(1.5)


def test_unscoreable_when_a_pool_is_thinner_than_k():
    rng = np.random.default_rng(0)
    good = _cloud(np.r_[1.0, np.zeros(63)], 50, rng)
    bad = _cloud(np.r_[-1.0, np.zeros(63)], 3, rng)
    dor, _, _ = score_matrix(rng.normal(size=(4, 64)), good, bad, k=5)
    assert np.isnan(dor).all(), "must report NaN, never score on fewer than k refs"
    res = score_stand_year(good, good, bad, k=5, n_boot=50, rng=rng)
    assert np.isnan(res["dor"])


def test_classify_refuses_to_call_a_site_whose_ci_straddles_the_threshold():
    assert classify(0.6, 0.55, 0.65, 0.5) == "regenerating"
    assert classify(0.4, 0.35, 0.45, 0.5) == "degraded"
    assert classify(0.5, 0.4, 0.6, 0.5) == "indistinguishable"
    assert classify(np.nan, np.nan, np.nan, 0.5) == "no_data"


def test_calibrate_threshold_finds_a_perfect_split():
    cal = calibrate_threshold(
        pd.Series([0.1, 0.2, 0.8, 0.9]), pd.Series(["bad", "bad", "good", "good"])
    )
    assert cal["youden_j"] == pytest.approx(1.0)
    assert 0.2 < cal["threshold"] <= 0.8


def test_calibrate_threshold_reports_no_separation_as_such():
    s = pd.Series([0.5, 0.5, 0.5, 0.5])
    y = pd.Series(["bad", "good", "bad", "good"])
    assert calibrate_threshold(s, y)["youden_j"] == pytest.approx(0.0)


def test_the_stand_summary_is_a_distribution_not_a_mean():
    """Half a stand clearcut: the median must not land in the empty middle."""
    pix = pd.DataFrame(
        {
            "poly_id": ["A"] * 10,
            "year": [2025] * 10,
            "dor_local": [0.1] * 5 + [0.9] * 5,
        }
    )
    out = summarise_pixels(pix, "dor_local", threshold=0.5).iloc[0]
    assert out["dor_local_n_pixels"] == 10
    assert out["dor_local_frac_poor"] == pytest.approx(0.5)
    assert out["dor_local_p25"] == pytest.approx(0.1)
    assert out["dor_local_p75"] == pytest.approx(0.9)


def test_per_pixel_scores_expose_sub_stand_variation_a_polygon_mean_hides():
    rng = np.random.default_rng(3)
    good = _cloud(np.r_[1.0, np.zeros(63)], 60, rng)
    bad = _cloud(np.r_[-1.0, np.zeros(63)], 60, rng)
    # A stand that is half intact and half converted.
    stand = np.vstack(
        [
            _cloud(np.r_[1.0, np.zeros(63)], 30, rng),
            _cloud(np.r_[-1.0, np.zeros(63)], 30, rng),
        ]
    )
    dor, _, _ = score_matrix(stand, good, bad, k=5)
    assert dor[:30].min() > 0.8 and dor[30:].max() < 0.2
    mean_score = score_stand_year(stand, good, bad, n_boot=100, rng=rng)["dor"]
    assert 0.2 < mean_score < 0.8, "the polygon mean describes neither half"


# ---------------------------------------------------------------------------
# The 2026-09-09 speed work. Each of these pins a rewritten hot path against the
# implementation it replaced, because the rewrite was only worth doing if the
# numbers do not move: `04_condition.py` went from 48.7 min to 8.1 min and the
# shipped outputs have to come back unchanged.
# ---------------------------------------------------------------------------


def _cluster_knn_table_loop(d, cluster, k):
    """The original: one boolean mask per cluster."""
    u, inv = np.unique(cluster, return_inverse=True)
    out = np.full((len(u), k), np.inf)
    for j in range(len(u)):
        v = np.sort(np.asarray(d, float)[inv == j])[:k]
        out[j, : len(v)] = v
    return out


def _boot_knn_mean_gather(table, n_boot, k, rng):
    """The original: gather the whole resampled pool and partition it."""
    c = len(table)
    idx = rng.integers(0, c, size=(n_boot, c))
    pool = np.asarray(table, float)[idx].reshape(n_boot, c * k)
    return np.partition(pool, k - 1, axis=1)[:, :k].mean(axis=1)


def _calibrate_threshold_loop(scores, labels, grid=None):
    """The original: a pass over the labels per grid point."""
    m = labels.isin(["good", "bad"]) & np.isfinite(scores)
    s, y = scores[m].to_numpy(), (labels[m] == "good").to_numpy()
    if grid is None:
        grid = np.linspace(np.nanmin(s), np.nanmax(s), 401)
    best = {"threshold": np.nan, "youden_j": -np.inf}
    for t in grid:
        pred = s >= t
        tpr = float((pred & y).sum() / y.sum())
        tnr = float((~pred & ~y).sum() / (~y).sum())
        j = tpr + tnr - 1.0
        if j > best["youden_j"]:
            best = {
                "threshold": float(t),
                "youden_j": float(j),
                "sensitivity": tpr,
                "specificity": tnr,
            }
    best["n_good"], best["n_bad"] = int(y.sum()), int((~y).sum())
    return best


@pytest.mark.parametrize(
    "n_clusters,n_pool,k",
    [
        (1046, 2000, 25),  # the deployed transformed pool
        (32, 1138, 25),  # the deployed local good pool
        (143, 1757, 25),  # the deployed global good pool
        (3, 40, 5),
        (1, 10, 5),
        (400, 400, 1),
        (60, 300, 60),
    ],
)
def test_cluster_knn_table_matches_the_per_cluster_loop(n_clusters, n_pool, k):
    rng = np.random.default_rng(n_clusters + k)
    cluster = rng.integers(0, n_clusters, n_pool).astype(object)
    d = np.abs(rng.normal(size=n_pool))
    want = _cluster_knn_table_loop(d, cluster, k)
    got = _cluster_knn_table(d, cluster, k)
    np.testing.assert_array_equal(np.isinf(want), np.isinf(got))
    np.testing.assert_array_equal(want[np.isfinite(want)], got[np.isfinite(got)])


@pytest.mark.parametrize(
    "n_clusters,n_pool,k",
    [
        (1046, 2000, 25),
        (32, 1138, 25),
        (143, 1757, 25),
        (3, 40, 5),
        (2, 10, 5),
        (400, 400, 1),
        (250, 260, 25),
    ],
)
def test_boot_knn_mean_matches_the_gather_and_partition_path(n_clusters, n_pool, k):
    """Same RNG draw in, same numbers out -- the whole point of the rewrite.

    Only the summation order changes (a cumulative sum over sorted candidates
    rather than a mean over a partitioned block), so the tolerance is float64
    rounding and not an approximation budget.
    """
    rng = np.random.default_rng(n_clusters * 7 + k)
    cluster = rng.integers(0, n_clusters, n_pool).astype(object)
    table = _cluster_knn_table(np.abs(rng.normal(size=n_pool)), cluster, k)
    want = _boot_knn_mean_gather(table, 500, k, np.random.default_rng(99))
    got = _boot_knn_mean(table, 500, k, np.random.default_rng(99))
    np.testing.assert_array_equal(np.isfinite(want), np.isfinite(got))
    f = np.isfinite(want)
    np.testing.assert_allclose(want[f], got[f], rtol=0, atol=1e-12)


def test_boot_knn_mean_is_exact_when_the_candidate_prefix_is_too_short(monkeypatch):
    """The prefix is a speed knob, never an approximation.

    Crushing it to its floor forces the fallback on draws whose k smallest are
    not all inside the prefix; the answers must not move.
    """
    import effektovervaking_forests.condition.score as sc

    rng = np.random.default_rng(3)
    cluster = rng.integers(0, 900, 2400).astype(object)
    table = _cluster_knn_table(np.abs(rng.normal(size=2400)), cluster, 25)
    want = _boot_knn_mean_gather(table, 400, 25, np.random.default_rng(1))
    monkeypatch.setattr(sc, "_BOOT_CANDIDATES", 0)
    got = sc._boot_knn_mean(table, 400, 25, np.random.default_rng(1))
    np.testing.assert_allclose(want, got, rtol=0, atol=1e-12)


def test_calibrate_threshold_matches_the_grid_loop_including_ties():
    rng = np.random.default_rng(0)
    for _trial in range(40):
        n = int(rng.integers(6, 300))
        labels = pd.Series(np.where(rng.random(n) < 0.3, "bad", "good"))
        if labels.nunique() < 2:
            continue
        # rounded scores put many stands on the same value, which is where a
        # first-maximum tie-break can diverge from an argmax one
        scores = pd.Series(rng.normal(0.55, 0.06, n).round(2))
        assert calibrate_threshold(scores, labels) == _calibrate_threshold_loop(
            scores, labels
        )


def test_prebuilt_pools_give_the_same_stand_interval():
    """`04_condition.py` hands in the probe and the transformed side once per
    stand-year instead of rebuilding them once per scope. Same numbers."""
    from effektovervaking_forests.condition.score import bad_side, stand_probe
    from effektovervaking_forests.condition.whiten import fit_whitener

    rng = np.random.default_rng(0)
    whit = fit_whitener(rng.normal(size=(3000, 64)).astype(np.float32))
    probes = rng.normal(size=(120, 64)).astype(np.float32)
    good = rng.normal(size=(600, 64)).astype(np.float32)
    bad = rng.normal(size=(900, 64)).astype(np.float32)
    g_cl = np.repeat(np.arange(20), 30).astype(object)
    b_cl = rng.integers(0, 300, 900).astype(object)

    plain = score_stand_year(
        probes,
        good,
        bad,
        k=25,
        n_boot=400,
        rng=np.random.default_rng(5),
        transform=whit,
        good_cluster=g_cl,
        bad_cluster=b_cl,
    )
    probe = stand_probe(probes, whit)
    fast = score_stand_year(
        probes,
        good,
        bad,
        k=25,
        n_boot=400,
        rng=np.random.default_rng(5),
        transform=whit,
        good_cluster=g_cl,
        bad_cluster=b_cl,
        probe=probe,
        good_t=whit(good),
        bad_t=whit(bad),
        bad_knn=bad_side(probe, whit(bad), b_cl, 25),
    )
    for key in ("dor", "ci_lo", "ci_hi", "n_good", "n_bad"):
        assert plain[key] == pytest.approx(fast[key], abs=1e-12), key


# --- per-cell bootstrap (boot_knn_cells and the median interval) ---------------

from effektovervaking_forests.condition import _kernels, score  # noqa: E402


def _cell_setup(seed=0, n_probes=200, n_pool=800, n_clusters=40, dim=64):
    rng = np.random.default_rng(seed)
    probes_u = score.unit_rows(rng.normal(size=(n_probes, dim)))
    pool_u = score.unit_rows(rng.normal(size=(n_pool, dim)))
    owner = rng.integers(0, n_clusters, n_pool)
    return rng, probes_u, pool_u, np.ascontiguousarray(pool_u.T), owner


def _brute_boot(probes_u, pool_u, codes, mult, k):
    out = np.empty((len(probes_u), mult.shape[0]))
    dist = 1.0 - probes_u @ pool_u.T
    for b in range(mult.shape[0]):
        rep = np.repeat(np.arange(len(pool_u)), mult[b, codes])
        d = np.sort(dist[:, rep], axis=1)[:, :k]
        out[:, b] = d.mean(axis=1)
    return out


@pytest.mark.parametrize("prefix", ["k", 256, "pool"])
def test_boot_knn_cells_matches_a_brute_force_replicated_cluster_knn(prefix):
    k = 25
    rng, probes_u, pool_u, pool_t, owner = _cell_setup()
    codes, mult, _ = score._cluster_draws(owner, 30, rng)
    T = {"k": k, 256: 256, "pool": len(pool_u)}[prefix]
    got = score.boot_knn_cells(probes_u, pool_t, codes, mult, k, prefix=T)
    want = _brute_boot(probes_u, pool_u, codes, mult, k)
    assert np.nanmax(np.abs(got - want)) < 1e-6
    assert np.isfinite(got).all()


def test_boot_knn_cells_with_unit_multiplicity_is_the_plain_knn_mean():
    k = 25
    rng, probes_u, pool_u, pool_t, owner = _cell_setup(1)
    codes = np.unique(owner, return_inverse=True)[1].astype(np.int32)
    mult = np.ones((1, codes.max() + 1), dtype=np.int32)
    got = score.boot_knn_cells(probes_u, pool_t, codes, mult, k)[:, 0]
    want = score.knn_mean_unit(probes_u, pool_u, k)
    assert np.abs(got - want).max() < 1e-6


@pytest.mark.skipif(not _kernels.AVAILABLE, reason="numba not available")
@pytest.mark.parametrize("T", [25, 256])
def test_numba_walk_agrees_with_the_numpy_walk(T):
    k = 25
    rng, probes_u, pool_u, pool_t, owner = _cell_setup(2)
    codes, mult, _ = score._cluster_draws(owner, 30, rng)
    d, own = score._cell_prefix(probes_u, pool_t, codes, T)
    o_nb, ok_nb = _kernels.boot_walk_cells(d, own, mult, k)
    o_np, ok_np = score._walk_cells_numpy(d, own, mult, k)
    np.testing.assert_array_equal(ok_nb, ok_np)
    np.testing.assert_array_equal(o_nb, o_np)


def test_rank_pick_keeps_only_finite_cells_and_the_median_cell():
    rng = np.random.default_rng(3)
    s = rng.random(60).astype(np.float32)
    s[[3, 10, 40]] = np.nan
    s[7] = np.inf
    few = score._rank_pick(s, 100)
    assert np.isfinite(s[few]).all()
    assert len(few) == np.isfinite(s).sum()
    m = 11
    pick = score._rank_pick(s, m)
    assert len(pick) == m and np.isfinite(s[pick]).all()
    fin = np.flatnonzero(np.isfinite(s))
    order = fin[np.argsort(s[fin], kind="stable")]
    assert order[(len(order) - 1) // 2] in pick or order[len(order) // 2] in pick
    # the middle pick is the cell at the median rank
    assert pick[m // 2] == order[int(round((len(order) - 1) * (m // 2) / (m - 1)))]


def _median_setup(n_cells=300, seed=4, k=25):
    rng, probes_u, pool_u, pool_t, owner = _cell_setup(seed, n_probes=n_cells)
    bad_u = score.unit_rows(rng.normal(size=(300, 64)) + 0.3)
    bad_t = np.ascontiguousarray(bad_u.T)
    bad_cluster = rng.integers(0, 15, 300)
    scores = {}
    good = {}
    for i, s in enumerate(("local", "global")):
        pu = pool_u if i == 0 else score.unit_rows(rng.normal(size=(500, 64)))
        ow = owner if i == 0 else rng.integers(0, 30, 500)
        mg = score.knn_mean_unit(probes_u, pu, k)
        mb = score.knn_mean_unit(probes_u, bad_u, k)
        scores[s] = (mb / (mg + mb + score.EPS)).astype(np.float32)
        good[s] = (np.ascontiguousarray(pu.T), ow)
    return probes_u, scores, good, (bad_t, bad_cluster)


def test_median_cluster_ci_point_and_ordering():
    k = 25
    probes_u, scores, good, bad = _median_setup()
    res = score.median_cluster_ci(
        probes_u, scores, good, bad, k, 200, np.random.default_rng(0)
    )
    for s, (p, lo, hi) in res.items():
        assert p == pytest.approx(float(np.nanmedian(scores[s])))
        assert np.isfinite(lo) and np.isfinite(hi)
        assert lo <= p <= hi


def test_median_cluster_ci_subsampling_cells_barely_moves_the_interval():
    k = 25
    probes_u, scores, good, bad = _median_setup()
    small = score.median_cluster_ci(
        probes_u, scores, good, bad, k, 200, np.random.default_rng(5), max_cells=51
    )
    full = score.median_cluster_ci(
        probes_u, scores, good, bad, k, 200, np.random.default_rng(5), max_cells=10**9
    )
    for s in scores:
        assert small[s][0] == full[s][0]
        assert abs(small[s][1] - full[s][1]) < 0.002
        assert abs(small[s][2] - full[s][2]) < 0.002


def test_median_cluster_ci_scope_with_too_small_good_pool_has_no_interval():
    k = 25
    probes_u, scores, good, bad = _median_setup()
    pool_t, owner = good["global"]
    good["global"] = (np.ascontiguousarray(pool_t[:, : k - 1]), owner[: k - 1])
    res = score.median_cluster_ci(
        probes_u, scores, good, bad, k, 200, np.random.default_rng(0)
    )
    p, lo, hi = res["global"]
    assert p == pytest.approx(float(np.nanmedian(scores["global"])))
    assert np.isnan(lo) and np.isnan(hi)
    assert np.isfinite(res["local"][1])


def test_median_cluster_ci_all_nan_scope_is_all_nan():
    k = 25
    probes_u, scores, good, bad = _median_setup()
    scores["global"] = np.full_like(scores["global"], np.nan)
    res = score.median_cluster_ci(
        probes_u, scores, good, bad, k, 200, np.random.default_rng(0)
    )
    assert all(np.isnan(v) for v in res["global"])
    assert np.isfinite(res["local"][0])
