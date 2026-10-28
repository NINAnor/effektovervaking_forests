"""The metric the distances are taken in: shrunk whitening of the ambient covariance.

The published scorer measures plain cosine distance in the raw 64-d embedding.
Over a coastal Norwegian forest AOI those 64 axes are strongly anisotropic --
a handful of directions carry most of the terrain, phenology and acquisition
variance -- and a plain cosine distance lets them dominate the good-vs-bad
contrast the score is supposed to measure. Whitening by the ambient covariance
removes that weighting:

    x  ->  normalise( Sigma_lambda^{-1/2} (x/||x|| - mu) )

Measured on this AOI (`condition/research/`, experiments E04, E06, E12, E14,
five seeds), that takes the pooled per-pixel separation of labelled good from
labelled bad stands from 0.649 to 0.713 AUC and olivinskog from 0.624 to 0.683,
under leave-spatial-block-out scoring.

**Two things about it are not optional.**

*The covariance is fitted on unlabelled pixels only.* `mu` and `Sigma` describe
the ambient spread of the forest type and must carry no information about which
stands a surveyor called `god`; fitting them on the reference pools would let the
class means shape the metric that is then used to measure distance to those
classes. Unlabelled stands of the same forest type give the ambient covariance
with no label in it at all.

*The covariance is shrunk before it is inverted.* Whitening divides by every
eigenvalue, so the smallest -- the least well estimated -- get the largest
weight, and the resulting score swings with whichever reference pixels happened
to be drawn. Unshrunk (`shrink = 0`) whitening gives the best ranking of stands
and the worst decisions about them: the honest donor-clustered interval widens
until 46% of stands cannot be classified at all, and the held-out `moderat`
class stops landing between the two anchors -- it falls below `dårlig`, which is
what overfitting the two reference pools looks like from outside them. At
`shrink = 0.2` the ordering is restored, the AUC gain is almost entirely kept,
and the share of unclassifiable stands drops below the baseline's. The default
is therefore 0.2 and it should not be moved without re-running E12.
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

    so `shrink = 1` is no whitening at all and `shrink = 0` is the full,
    unstable version. See the module docstring for why the default is neither.
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
    # A floor on the eigenvalues as well as the shrinkage: with shrink = 0 the
    # smallest eigenvalue can be numerically zero, and 0^-0.5 is not a metric.
    w = np.maximum(w, 1e-6 * w.max())
    W = V @ np.diag(w**-0.5) @ V.T
    return Whitener(mu, W, shrink, len(Z))
