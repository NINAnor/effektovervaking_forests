"""Condition assessment for olivinskog / kalklindeskog from AlphaEarth embeddings.

Two steps, run in order:

  step 1  trajectory classification  (condition.trend)
          z-score -> Theil-Sen slope + Mann-Kendall test on an annual scalar
          derived from the 64-d embedding. Ported from RECOVER
          (scripts/analysis/analyze_trajectories.py, test_trend_classification.py).

  step 2  condition scoring          (condition.core, condition.axis)
          Degree-of-Recovery kNN cosine score against good/bad reference pools.
          Ported from DegreeofRecovery/paper_release/src/degree_of_recovery.
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
