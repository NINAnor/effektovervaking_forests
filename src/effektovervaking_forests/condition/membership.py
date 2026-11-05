"""Overlap-safe pixel model: a pixel belongs to a *set* of polygons.

The mapped layers overlap, so embeddings are stored once per `pixel_id` =
(utm_zone, x, y) and polygon membership is a separate many-to-many table. This
lets a polygon keep all its pixels, exposes role conflicts between overlapping
labels, and makes "exclude the probe from its own pool" exact (by `pixel_id`,
not `poly_id`). METHODS.md section 2.
"""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd

from effektovervaking_forests.condition.paths import BANDS

__all__ = [
    "dedupe_pixels",
    "build_membership",
    "polygon_overlaps",
    "pixel_roles",
]

PIXEL_KEY = ["utm_zone", "x", "y"]


def dedupe_pixels(px: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split the long polygon-pixel table into (pixels, sites).

    `pixels`: one row per (pixel_id, year) with the 64 bands. `sites`: one row
    per pixel_id with `utm_zone, x, y`. Deduplication is on pixel identity, not
    read order (annual tiles of a UTM zone share one grid, so duplicates agree).
    """
    sites = (
        px[PIXEL_KEY]
        .drop_duplicates()
        .sort_values(PIXEL_KEY, kind="stable")
        .reset_index(drop=True)
    )
    sites.insert(0, "pixel_id", np.arange(len(sites), dtype=np.int32))

    keyed = px.merge(sites, on=PIXEL_KEY, how="left", validate="many_to_one")
    pixels = (
        keyed[["pixel_id", "year", *BANDS]]
        .drop_duplicates(subset=["pixel_id", "year"])
        .sort_values(["pixel_id", "year"], kind="stable")
        .reset_index(drop=True)
    )
    for c in BANDS:
        pixels[c] = pixels[c].astype("float32")
    return pixels, sites


def build_membership(
    sites: pd.DataFrame, polygons: gpd.GeoDataFrame, poly_crs_epsg: int = 25833
) -> pd.DataFrame:
    """Every (pixel_id, poly_id) pair whose pixel centre falls in the polygon.

    Many-to-many. Done per UTM zone because `sites` holds coordinates in each
    pixel's own zone. `within` on the pixel centre matches the extractor's
    `all_touched=False` rule.
    """
    poly = polygons[["poly_id", "geometry"]]
    out = []
    for zone, grp in sites.groupby("utm_zone", sort=False):
        epsg = 32600 + int(zone[:-1]) if zone.endswith("N") else 32700 + int(zone[:-1])
        pts = gpd.GeoDataFrame(
            grp[["pixel_id"]],
            geometry=gpd.points_from_xy(grp["x"], grp["y"]),
            crs=f"EPSG:{epsg}",
        )
        hit = pts.sjoin(poly.to_crs(epsg), how="inner", predicate="within")
        out.append(hit[["pixel_id", "poly_id"]])
    mem = (
        pd.concat(out, ignore_index=True)
        if out
        else pd.DataFrame(columns=["pixel_id", "poly_id"])
    )
    return mem.drop_duplicates().reset_index(drop=True)


def polygon_overlaps(
    polygons: gpd.GeoDataFrame, buffer_m: float = 20.0
) -> pd.DataFrame:
    """Ordered pairs of polygons that touch, with the shared area fraction.

    `buffer_m` widens the test past strict intersection so near neighbours are
    also kept out of a probe's reference pool. Returns `poly_id, poly_id2,
    frac`; `frac` is the share of the FIRST polygon's area covered by the
    second, so the table is asymmetric.
    """
    g = polygons[["poly_id", "geometry"]].reset_index(drop=True)
    probe = g.assign(geometry=g.geometry.buffer(buffer_m)) if buffer_m else g
    right = g.rename(
        columns={"poly_id": "poly_id2", "geometry": "geometry2"}
    ).set_geometry("geometry2")
    pairs = probe.sjoin(right, how="inner", predicate="intersects").reset_index(
        drop=True
    )
    pairs = pairs[pairs["poly_id"] != pairs["poly_id2"]]
    if pairs.empty:
        return pd.DataFrame(columns=["poly_id", "poly_id2", "frac"])

    geo = g.set_index("poly_id").geometry
    a = geo.loc[pairs["poly_id"]].to_numpy()
    b = geo.loc[pairs["poly_id2"]].to_numpy()
    inter = np.array([x.intersection(y).area for x, y in zip(a, b)])
    area = np.array([x.area for x in a])
    pairs = pairs[["poly_id", "poly_id2"]].copy()
    pairs["frac"] = inter / np.where(area > 0, area, np.nan)
    return pairs.reset_index(drop=True)


def pixel_roles(membership: pd.DataFrame, polygons: pd.DataFrame) -> pd.DataFrame:
    """Resolve each pixel's reference role across all polygons that claim it.

    A pixel labelled differently by different polygons gets role `conflict` and
    is used as a reference by neither pool; an explicit label beats no label.
    Returns one row per pixel: `pixel_id, forest_type, role, n_polygons`
    (`forest_type` is `mixed` if a pixel is mapped as both types).
    """
    m = membership.merge(
        polygons[["poly_id", "forest_type", "ref_role"]], on="poly_id", how="left"
    )
    g = m.groupby("pixel_id", sort=True)

    def _role(vals: set[str]) -> str:
        labelled = vals - {"unlabelled"}
        if not labelled:
            return "unlabelled"
        if len(labelled) > 1:
            return "conflict"
        return labelled.pop()

    agg = g.agg(
        roles=("ref_role", lambda s: _role(set(s))),
        types=("forest_type", lambda s: set(s)),
        n_polygons=("poly_id", "size"),
    )
    agg["forest_type"] = agg["types"].map(lambda s: s.pop() if len(s) == 1 else "mixed")
    return (
        agg.drop(columns="types")
        .rename(columns={"roles": "role"})
        .reset_index()[["pixel_id", "forest_type", "role", "n_polygons"]]
    )
