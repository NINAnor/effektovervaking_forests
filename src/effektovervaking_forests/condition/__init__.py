"""Condition assessment for olivinskog / kalklindeskog from AlphaEarth embeddings.

Step 1, trajectory classification (`trend`): Theil-Sen slope + Mann-Kendall.
Step 2, condition scoring (`core`, `score`): kNN cosine Degree of Recovery
against good/bad reference pools. See docs/condition/METHODS.md.
"""

from effektovervaking_forests.condition.core import (
    EPS,
    bootstrap_ci,
    classify,
    cosine_dist,
    cosine_dists_to_set,
    knn_score,
    median_score,
)
from effektovervaking_forests.condition.trend import (
    classify_trend,
    theil_sen_mann_kendall,
)

__all__ = [
    "EPS",
    "bootstrap_ci",
    "classify",
    "cosine_dist",
    "cosine_dists_to_set",
    "knn_score",
    "median_score",
    "theil_sen_mann_kendall",
    "classify_trend",
]
