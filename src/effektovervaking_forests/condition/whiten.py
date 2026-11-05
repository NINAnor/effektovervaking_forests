"""Shrunk whitening of the ambient covariance: the metric distances are taken in.

    x  ->  normalise( Sigma_lambda^{-1/2} (x/||x|| - mu) )

Invariants: the covariance is fitted on UNLABELLED pixels only (so no class
information leaks into the metric), and it is shrunk toward isotropy before
inversion (default 0.2; unshrunk whitening is unstable). METHODS.md section 4.
"""

from __future__ import annotations

import numpy as np

__all__ = ["Whitener", "fit_whitener", "SHRINK_DEFAULT"]

EPS = 1e-12
SHRINK_DEFAULT = 0.2


class Whitener:
    """`mu` and `Sigma_lambda^{-1/2}` for one forest type, and the map itself."""

    __slots__ = ("mu", "W", "shrink", "n_fit")

    def __init__(self, mu: np.ndarray, W: np.ndarray, shrink: float, n_fit: int):
        self.mu = np.asarray(mu, dtype=np.float32)
        self.W = np.asarray(W, dtype=np.float32)
        self.shrink = float(shrink)
        self.n_fit = int(n_fit)

    def __call__(self, X: np.ndarray) -> np.ndarray:
        """(n, 64) embeddings -> (n, 64) unit rows in the whitened space."""
        if len(X) == 0:
            return np.asarray(X, dtype=np.float32).reshape(0, self.W.shape[0])
        Z = unit_rows(X)
        return unit_rows((Z - self.mu) @ self.W)

    def to_dict(self) -> dict:
        return {
            "mu": self.mu.tolist(),
            "W": self.W.tolist(),
            "shrink": self.shrink,
            "n_fit": self.n_fit,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Whitener:
        return cls(
            np.asarray(d["mu"], np.float32),
            np.asarray(d["W"], np.float32),
            d["shrink"],
            d["n_fit"],
        )


def unit_rows(x: np.ndarray) -> np.ndarray:
    """L2-normalise each row, leaving all-zero rows at zero."""
    x = np.asarray(x, dtype=np.float32)
    return x / (np.linalg.norm(x, axis=1, keepdims=True) + EPS)


def fit_whitener(
    X: np.ndarray, shrink: float = SHRINK_DEFAULT, max_rows: int = 60_000, seed: int = 0
) -> Whitener:
    """Fit on `X`, which must be embeddings of UNLABELLED pixels of one forest type.

    `shrink` pulls the covariance toward isotropy before inversion:

        Sigma_lambda = (1 - lambda) Sigma + lambda (tr Sigma / p) I

    so `shrink = 1` is no whitening and `shrink = 0` the full, unstable version.
    """
    if not 0.0 <= shrink <= 1.0:
        raise ValueError(f"shrink must be in [0, 1], got {shrink}")
    X = np.asarray(X, dtype=np.float32)
    if len(X) < X.shape[1] + 2:
        raise ValueError(
            f"need more rows than dimensions to estimate a covariance; "
            f"got {len(X)} rows for {X.shape[1]} bands"
        )
    if len(X) > max_rows:
        X = X[np.random.default_rng(seed).choice(len(X), max_rows, replace=False)]
    Z = unit_rows(X).astype(np.float64)
    mu = Z.mean(axis=0)
    C = np.cov(Z - mu, rowvar=False)
    if shrink > 0:
        C = (1.0 - shrink) * C + shrink * (np.trace(C) / C.shape[0]) * np.eye(
            C.shape[0]
        )
    w, V = np.linalg.eigh(C)
    # Eigenvalue floor: with shrink = 0 the smallest can be ~0 and 0^-0.5 blows up.
    w = np.maximum(w, 1e-6 * w.max())
    W = V @ np.diag(w**-0.5) @ V.T
    return Whitener(mu, W, shrink, len(Z))
