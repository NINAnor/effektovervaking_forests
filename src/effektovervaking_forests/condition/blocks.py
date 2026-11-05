"""Spatial blocks and edge distance (METHODS.md sections 2-3).

Blocks: single-linkage clusters of stand centroids (default 5 km). Single
linkage because the *chain* of near-duplicate stands must be broken, not cut
into pieces. Intersecting stands are then merged, so one piece of ground always
has one set of exclusions.

Edge distance: every pixel carries its distance to its stand boundary, because
edge pixels mix stand and surroundings and small stands are mostly edge.
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
