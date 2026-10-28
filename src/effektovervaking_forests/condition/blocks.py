"""Spatial blocks and edge distance: the two things that made the numbers honest.

**Blocks.** The six mapped layers cover the same localities repeatedly and `god`
stands cluster, so a probe's nearest eligible donor is routinely a few hundred
metres away -- same hillside, same orbit, same acquisition date. Excluding only
the probe and the stands that overlap it leaves that in, and it is worth about a
third of the reported stand-grain separation: measured on this AOI, pooled
stand AUC falls 0.761 -> 0.695 and Youden J 0.411 -> 0.331 once a probe cannot
see its own 5 km locality (`condition/research/`, experiment E01).

Clustering is **single linkage**, not k-means, and the reason matters: what has
to be broken is the *chain* of near-duplicate stands. A line of stands each 1 km
from the next is one locality; k-means would cut it into several and let the
probe's own neighbourhood back into its reference pool. The result is stable
between 3 and 10 km (experiment E08), and 5 km is the default.

The clustering runs on centroids, which on its own is not quite enough: a 211 ha
olivinskog stand and a 0.3 ha stand drawn on top of it can have centroids more
than 5 km apart and land in different blocks, and then the same piece of ground
is scored against two different reference pools. So intersecting stands are
merged into one block afterwards. That can only make blocks larger, i.e. the
exclusion stricter, and it guarantees the invariant the pixel model rests on:
one piece of ground, one set of exclusions.

**Edge distance.** The bad-labelled stands are systematically smaller than the
good ones (median 0.28 ha against 0.53 ha), and stand area alone separates the
field labels at AUC 0.652 -- 0.736 on kalklindeskog. A small stand is mostly
edge and an edge pixel is a mixture of the stand and whatever is next to it, so
"degraded" and "small" are confounded through the sensor as well as through the
surveyor. On pixels at least 20 m inside their own stand the baseline score
loses 0.04 AUC (0.645 -> 0.604) while the whitened one does not, so part of what
the shipped per-pixel map reported as degradation was edge mixture (experiment
E10). Every pixel therefore carries its distance to its stand's boundary, and
anything published per pixel should say which side of that line it is on.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "spatial_blocks",
    "assign_blocks",
    "edge_distance",
    "BLOCK_KM_DEFAULT",
    "INTERIOR_M_DEFAULT",
]

BLOCK_KM_DEFAULT = 5.0
INTERIOR_M_DEFAULT = 20.0


def spatial_blocks(
    cx: np.ndarray, cy: np.ndarray, km: float = BLOCK_KM_DEFAULT
) -> np.ndarray:
    """Single-linkage clusters of projected centroids, cut at `km`."""
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import pdist

    cx, cy = np.asarray(cx, float), np.asarray(cy, float)
    if len(cx) < 2:
        return np.zeros(len(cx), dtype=int)
    d = pdist(np.column_stack([cx, cy]))
    return fcluster(
        linkage(d, method="single"), t=km * 1000.0, criterion="distance"
    ).astype(int)


def assign_blocks(
    poly_geo, km: float = BLOCK_KM_DEFAULT, merge_touching: bool = True
) -> pd.DataFrame:
    """`poly_id, cx, cy, block` from a projected GeoDataFrame of stands.

    With `merge_touching` (the default) any two stands whose geometries
    intersect end up in the same block, whatever their centroids say.
    """
    cent = poly_geo.geometry.centroid
    out = pd.DataFrame(
        {
            "poly_id": poly_geo["poly_id"].to_numpy(),
            "cx": cent.x.to_numpy(),
            "cy": cent.y.to_numpy(),
        }
    )
    blk = spatial_blocks(out["cx"].to_numpy(), out["cy"].to_numpy(), km)
    if merge_touching:
        blk = _merge_intersecting(poly_geo, blk)
    out["block"] = blk
    return out


def _merge_intersecting(poly_geo, blk: np.ndarray) -> np.ndarray:
    """Union-find over the blocks of every intersecting pair of stands."""
    from shapely import STRtree

    parent = {int(b): int(b) for b in np.unique(blk)}

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    geoms = poly_geo.geometry.to_numpy()
    tree = STRtree(geoms)
    left, right = tree.query(geoms, predicate="intersects")
    for i, j in zip(left, right):
        if i == j:
            continue
        ra, rb = find(int(blk[i])), find(int(blk[j]))
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    return np.array([find(int(b)) for b in blk], dtype=int)


def edge_distance(
    membership: pd.DataFrame, sites: pd.DataFrame, poly_geo, working_crs: int = 25833
) -> pd.Series:
    """Metres from each (pixel, polygon) pair's pixel centre to that stand's boundary.

    Positive inside. Returned aligned to `membership`'s index. The pixel sites
    are stored in their own AlphaEarth UTM zone, so they are reprojected to the
    polygons' CRS first -- mixing 32N eastings with 33N geometry would put every
    pixel hundreds of kilometres out with no error raised.
    """
    import pyproj
    from shapely import distance, points

    xs = np.empty(len(sites), float)
    ys = np.empty(len(sites), float)
    idx = np.arange(len(sites))
    for zone, g in sites.groupby("utm_zone"):
        tr = pyproj.Transformer.from_crs(
            f"EPSG:{32600 + int(str(zone)[:-1])}", f"EPSG:{working_crs}", always_xy=True
        )
        a, b = tr.transform(g["x"].to_numpy(), g["y"].to_numpy())
        rows = idx[sites.index.get_indexer(g.index)]
        xs[rows], ys[rows] = a, b
    pt = pd.DataFrame({"pixel_id": sites["pixel_id"].to_numpy(), "px": xs, "py": ys})

    m = membership[["pixel_id", "poly_id"]].merge(pt, on="pixel_id", how="left")
    geom = poly_geo.set_index("poly_id").geometry
    out = np.full(len(m), np.nan)
    for poly_id, g in m.groupby("poly_id", sort=False):
        gm = geom.get(poly_id)
        if gm is None:
            continue
        out[g.index.to_numpy()] = distance(
            points(g["px"].to_numpy(), g["py"].to_numpy()), gm.boundary
        )
    return pd.Series(out, index=membership.index, name="edge_m")
