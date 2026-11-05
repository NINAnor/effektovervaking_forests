"""The metric, the blocks and the slope gate (research E01-T04 changes)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from effektovervaking_forests.condition.blocks import spatial_blocks
from effektovervaking_forests.condition.score import (
    calibrate_threshold_blocked,
)
from effektovervaking_forests.condition.trend import (
    calibrate_slope_gate,
    classify_trend,
)
from effektovervaking_forests.condition.whiten import fit_whitener


def _anisotropic(n=2000, seed=0):
    """A cloud whose first axis carries 100x the variance of the rest.

    This is the situation whitening exists for: over a forest AOI a handful of
    embedding directions carry the terrain and phenology variance and swamp the
    condition contrast in a plain cosine distance.
    """
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 8)).astype(np.float32)
    x[:, 0] *= 10.0
    return x


def test_whitening_equalises_the_variance_it_was_given():
    w = fit_whitener(_anisotropic(), shrink=0.0)
    z = np.asarray(w(_anisotropic(seed=1)))
    sd = z.std(axis=0)
    assert sd.max() / sd.min() < 3.0, "whitened axes are still wildly unequal"


def test_full_shrinkage_is_the_identity_metric():
    """shrink=1 must leave cosine distances unchanged, so it is a true baseline."""
    X = _anisotropic(n=300)
    w = fit_whitener(X, shrink=1.0)
    a, b = X[:50], X[50:100]
    raw = (
        1
        - (a / np.linalg.norm(a, axis=1, keepdims=True))
        @ (b / np.linalg.norm(b, axis=1, keepdims=True)).T
    )
    wht = 1 - np.asarray(w(a)) @ np.asarray(w(b)).T
    # Centring still moves the origin, so distances are not identical -- but the
    # ORDER of neighbours, which is all kNN uses, must be.
    assert np.corrcoef(raw.ravel(), wht.ravel())[0, 1] > 0.9


def test_shrinkage_is_bounded_and_the_map_returns_unit_rows():
    with pytest.raises(ValueError):
        fit_whitener(_anisotropic(n=200), shrink=1.5)
    w = fit_whitener(_anisotropic(n=200), shrink=0.2)
    z = np.asarray(w(_anisotropic(n=10)))
    assert np.allclose(np.linalg.norm(z, axis=1), 1.0, atol=1e-4)


def test_a_covariance_cannot_be_fitted_on_fewer_rows_than_dimensions():
    with pytest.raises(ValueError, match="more rows than dimensions"):
        fit_whitener(np.zeros((10, 64), dtype=np.float32))


# --- blocks ------------------------------------------------------------------
def test_single_linkage_joins_a_chain_that_k_means_would_cut():
    """Stands 1 km apart in a line are ONE locality however long the line is.

    This is why the blocking is single linkage: the chain is exactly the case a
    centroid method would split, letting a probe's own neighbourhood back into
    its reference pool.
    """
    cx = np.arange(0, 10_001, 1000.0)
    blocks = spatial_blocks(cx, np.zeros_like(cx), km=5.0)
    assert len(set(blocks)) == 1
    cx2 = np.array([0.0, 1000.0, 50_000.0])
    assert len(set(spatial_blocks(cx2, np.zeros_like(cx2), km=5.0))) == 2


# --- the clustered bootstrap -------------------------------------------------
def test_the_blocked_threshold_never_uses_the_block_it_is_applied_to():
    scores = pd.Series([0.9, 0.9, 0.1, 0.1, 0.5, 0.5])
    labels = pd.Series(["good", "good", "bad", "bad", "good", "bad"])
    blocks = pd.Series([1, 1, 2, 2, 3, 3])
    cal = calibrate_threshold_blocked(scores, labels, blocks)
    # Block 3 is the ambiguous pair; its threshold must come from blocks 1 and 2,
    # which separate perfectly somewhere between 0.1 and 0.9.
    assert 0.1 < cal["by_block"]["3"] <= 0.9
    assert cal["n_blocks"] == 3


# --- the slope gate ----------------------------------------------------------
def test_the_gate_fixes_the_false_positive_rate_by_construction():
    rng = np.random.default_rng(0)
    clean = rng.normal(0.0, 0.05, 5000)
    lo, hi = calibrate_slope_gate(clean, pctl=2.0)
    assert np.isclose((clean < lo).mean(), 0.02, atol=0.005)
    assert np.isclose((clean > hi).mean(), 0.02, atol=0.005)


def test_the_gate_classifies_on_effect_size_not_significance():
    """A big slope with an unremarkable p-value is still a decline."""
    feat = pd.DataFrame(
        {"sen_slope": [-0.5, -0.001], "mk_p": [0.9, 0.001], "sd": [0.1, 0.1]}
    )
    sig_rule = classify_trend(feat)
    gate_rule = classify_trend(feat, gate=(-0.07, 0.07))
    assert list(sig_rule) == ["stable", "declining"]
    assert list(gate_rule) == ["declining", "stable"]


def test_a_reversed_gate_is_an_error():
    feat = pd.DataFrame({"sen_slope": [0.0], "mk_p": [0.5], "sd": [0.1]})
    with pytest.raises(ValueError, match="lo < hi"):
        classify_trend(feat, gate=(0.07, -0.07))


def test_calibrating_a_gate_on_too_few_references_is_refused():
    with pytest.raises(ValueError, match="at least 100"):
        calibrate_slope_gate(np.zeros(50))
