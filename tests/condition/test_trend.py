"""The trajectory step must give the right answer on series whose answer is known."""

import numpy as np
import pytest

from effektovervaking_forests.condition.trend import (
    MIN_VALID_YEARS,
    classify_trend,
    theil_sen_mann_kendall,
    zscore_by_group,
)

T = 9  # 2017-2025


def _series(**kw):
    rng = np.random.default_rng(0)
    return rng, kw


def test_monotone_series_are_detected_with_the_right_sign():
    rng = np.random.default_rng(0)
    up = np.arange(T) * 0.3 + rng.normal(0, 0.05, T)
    down = -np.arange(T) * 0.3 + rng.normal(0, 0.05, T)
    f = theil_sen_mann_kendall(np.stack([up, down]))
    assert f.sen_slope[0] == pytest.approx(0.3, abs=0.05)
    assert f.sen_slope[1] == pytest.approx(-0.3, abs=0.05)
    assert (f.mk_p < 0.05).all()
    assert list(classify_trend(f)) == ["improving", "declining"]


def test_flat_series_is_stable_not_fluctuating():
    rng = np.random.default_rng(1)
    f = theil_sen_mann_kendall(rng.normal(0, 0.02, (1, T)))
    assert classify_trend(f)[0] == "stable"


def test_alternating_series_is_fluctuating_and_survives_the_tie_correction():
    """A 0,2,0,2,... series is all ties. Without the tie term Var(S) is wrong;
    with it subtracted un-divided, Var(S) goes negative and the row is lost."""
    y = np.tile([0.0, 2.0], 5)[:T][None, :]
    f = theil_sen_mann_kendall(y)
    assert np.isfinite(f.mk_z[0]), "tie correction must not drive Var(S) negative"
    assert f.mk_p[0] > 0.05
    assert classify_trend(f)[0] == "fluctuating"


def test_too_few_observations_is_no_data_not_a_trend():
    y = np.full((1, T), np.nan)
    y[0, -3:] = [1.0, 2.0, 3.0]
    f = theil_sen_mann_kendall(y)
    assert f.n_valid[0] < MIN_VALID_YEARS
    assert classify_trend(f)[0] == "no_data"


def test_nan_years_are_skipped_not_treated_as_zero():
    """A rising series with a missing year must stay a rising series."""
    full = np.arange(T, dtype=float) * 0.3
    holed = full.copy()
    holed[4] = np.nan
    f = theil_sen_mann_kendall(np.stack([full, holed]))
    assert f.sen_slope[1] == pytest.approx(f.sen_slope[0], abs=1e-9)
    assert list(classify_trend(f)) == ["improving", "improving"]


def test_min_slope_gates_significant_but_negligible_trends():
    y = (np.arange(T) * 0.001)[None, :]
    f = theil_sen_mann_kendall(y)
    assert f.mk_p[0] < 0.05, "a perfectly monotone series is significant however small"
    assert classify_trend(f, min_slope=0.0)[0] == "improving"
    assert classify_trend(f, min_slope=0.01)[0] in {"stable", "fluctuating"}


def test_zscore_is_per_group():
    vals = np.vstack([np.full((2, T), 10.0), np.full((2, T), 100.0)])
    vals += np.tile(np.arange(T, dtype=float), (4, 1))
    z = zscore_by_group(vals, np.array(["a", "a", "b", "b"]))
    # each group is centred on its own mean, so the two groups' z-scores match
    assert np.allclose(z[:2], z[2:], atol=1e-5)
