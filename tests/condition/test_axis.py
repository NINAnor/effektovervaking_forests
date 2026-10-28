"""The naturalness axis: direction, scale-invariance, and refusal to fit noise."""

import numpy as np
import pandas as pd
import pytest

from effektovervaking_forests.condition.axis import annual_matrix, fit_axis, project


def test_axis_points_from_bad_to_good():
    rng = np.random.default_rng(0)
    good = rng.normal(np.r_[1.0, np.zeros(63)], 0.05, (50, 64))
    bad = rng.normal(np.r_[-1.0, np.zeros(63)], 0.05, (50, 64))
    w = fit_axis(good, bad)
    assert np.linalg.norm(w) == pytest.approx(1.0)
    assert project(good, w).mean() > project(bad, w).mean()


def test_projection_is_invariant_to_the_int8_scale_factor():
    """Nothing may depend on AEF_INT8_SCALE being exactly 1/127."""
    rng = np.random.default_rng(1)
    good = rng.normal(np.r_[1.0, np.zeros(63)], 0.1, (30, 64))
    bad = rng.normal(np.r_[-1.0, np.zeros(63)], 0.1, (30, 64))
    w = fit_axis(good, bad)
    x = rng.normal(0, 1, (10, 64))
    assert np.allclose(project(x, w), project(x * 127.0, w))
    assert np.allclose(w, fit_axis(good * 3.7, bad * 3.7))


def test_collinear_pools_raise_rather_than_return_a_meaningless_axis():
    v = np.ones((10, 64))
    with pytest.raises(ValueError, match="collinear"):
        fit_axis(v, v * 2.0)


def test_empty_pool_raises():
    with pytest.raises(ValueError, match="non-empty"):
        fit_axis(np.ones((5, 64)), np.empty((0, 64)))


def test_annual_matrix_leaves_missing_years_as_nan():
    df = pd.DataFrame(
        {
            "poly_id": ["A", "A", "B"],
            "forest_type": ["o"] * 3,
            "year": [2017, 2019, 2017],
            "c": [0.1, 0.3, 0.2],
        }
    )
    keys, mat = annual_matrix(df, "c", [2017, 2018, 2019], ["poly_id", "forest_type"])
    assert list(keys.poly_id) == ["A", "B"]
    assert np.isnan(mat[0, 1]) and np.isnan(mat[1, 1]) and np.isnan(mat[1, 2])
    assert mat[0, 0] == 0.1 and mat[0, 2] == 0.3
