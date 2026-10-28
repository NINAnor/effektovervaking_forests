"""The naturalness axis: turning a 64-d embedding into one condition number.

Mann-Kendall and Theil-Sen need a scalar per year. RECOVER had one for free
(GPP, vegetation height); AlphaEarth gives a 64-d vector instead, so a scalar
has to be defined -- and the choice determines what "improving" means.

We use the direction in embedding space that separates the two reference pools
that step 2 already needs:

    w = normalise( mean(good pixels, unit-normalised)
                 - mean(bad  pixels, unit-normalised) )

and project each pixel-year onto it:

    c(t) = <x(t)/||x(t)||, w>          in [-1, 1]

This ties the two steps together rather than bolting them side by side:
`c` is a signed, monotone reparameterisation of exactly the good-vs-bad contrast
the DoR score measures, so "trajectory" and "condition" are statements about the
same axis. A rising `c` means the pixel is moving toward the good reference
cloud and away from the bad one; the DoR score in step 2 says where it has
arrived.

Three properties worth stating explicitly:

  * **Scale-free.** Both `x` and `w` are unit-normalised, so the int8 -> float
    quantisation factor of the Source Cooperative mirror cancels. Nothing here
    depends on `AEF_INT8_SCALE` being exactly 1/127.
  * **Time-invariant by construction.** The axis is fitted once, from reference
    pixels pooled over all years, and then held fixed. Re-fitting per year would
    make a trend in `c` uninterpretable -- the ruler would move with the thing
    being measured.
  * **One axis per forest type.** Olivinskog and kalklindeskog have different
    good and bad states, so `fit_axis` is applied per type, never pooled.

The alternative scalars we did not use, and why: the year-to-year embedding
distance ||x(t) - x(t-1)|| detects disturbance but has no sign, so it cannot
distinguish recovery from decline; a PCA1 of the 64 bands (the RECOVER
GPP/height analogue) maximises variance, which in a forest stand is dominated
by phenology and terrain rather than condition.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["unit", "fit_axis", "project", "annual_matrix"]

EPS = 1e-12


def unit(x: np.ndarray, axis: int = -1) -> np.ndarray:
    """L2-normalise along `axis`, leaving all-zero rows at zero."""
    n = np.linalg.norm(x, axis=axis, keepdims=True)
    return x / (n + EPS)


def fit_axis(good: np.ndarray, bad: np.ndarray) -> np.ndarray:
    """Unit vector pointing from the bad reference cloud to the good one.

    `good` and `bad` are (n, 64) arrays of reference embeddings, pooled over all
    years. Rows containing NaN are dropped.
    """
    g = np.asarray(good, dtype=float)
    b = np.asarray(bad, dtype=float)
    g = g[np.isfinite(g).all(axis=1)]
    b = b[np.isfinite(b).all(axis=1)]
    if len(g) == 0 or len(b) == 0:
        raise ValueError(f"need both pools non-empty; got good={len(g)} bad={len(b)}")
    w = unit(g).mean(axis=0) - unit(b).mean(axis=0)
    norm = np.linalg.norm(w)
    if norm < 1e-8:
        raise ValueError(
            "good and bad reference means are collinear -- the pools do not "
            "separate in embedding space, so no naturalness axis exists"
        )
    return w / norm


def project(x: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Cosine similarity of each row of `x` (n, 64) with the axis `w`."""
    return unit(np.asarray(x, dtype=float)) @ np.asarray(w, dtype=float)


def annual_matrix(
    df: pd.DataFrame,
    value: str,
    years: list[int],
    keys: list[str],
) -> tuple[pd.DataFrame, np.ndarray]:
    """Pivot long (keys, year, value) rows into a dense (n, len(years)) matrix.

    Returns the key frame (one row per unit, in matrix order) and the matrix,
    with NaN for years a unit has no observation in -- which the Mann-Kendall
    code treats as missing rather than as zero.
    """
    wide = (
        df.pivot_table(index=keys, columns="year", values=value, aggfunc="mean")
        .reindex(columns=years)
        .reset_index()
    )
    mat = wide[years].to_numpy(dtype=float)
    return wide[keys].copy(), mat
