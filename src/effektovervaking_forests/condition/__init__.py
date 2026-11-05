"""Condition assessment for olivinskog / kalklindeskog from AlphaEarth embeddings.

Step 1, trajectory classification (`trend`): Theil-Sen slope + Mann-Kendall.
Step 2, condition scoring (`core`, `score`): kNN cosine Degree of Recovery
against good/bad reference pools. See docs/condition/METHODS.md.
"""
