"""Pixels belong to a *set* of polygons, not to one. Everything else follows.

The six mapped layers overlap heavily. Measured on this AOI: 2,236.7 ha of
polygon area sits on 1,693.7 ha of ground, 353 of 737 polygons share more than
1% of their area with another polygon, and MI/HB13/NiN5k routinely map the same
stand three times.

`aef.extract_from_tile` rasterises every polygon into ONE integer label raster,
so a shared pixel is written once and the last polygon drawn wins. The
consequences were not subtle:

  * the pixel table held 168,856 rows per year -- the *union* area, not the
    polygon area, so ~55,000 pixel-memberships per year were simply absent;
  * 152 of 737 polygons kept under half their pixels;
  * several `god` reference stands of up to 5 ha kept exactly ONE pixel, because
    they lost every pixel to an overlapping neighbour and then tripped the
    sub-pixel centroid fallback. Those one-pixel stands were going straight into
    the good reference pool.

This module replaces that model. The embedding of a piece of ground is stored
once, keyed by `pixel_id` = (utm_zone, x, y); which polygons cover it is a
separate many-to-many table. Three things then become expressible that were not:

  * a polygon gets *all* its pixels back, however many neighbours overlap it;
  * a pixel that two polygons disagree about (one `god`, one `dårlig`) is
    visible as a role conflict and can be dropped from the pools rather than
    silently counted as whichever polygon was drawn last;
  * "exclude the probe from its own reference pool" becomes exact. Excluding by
    `poly_id` never worked across overlaps -- the probe's own ground came back
    into the pool wearing a neighbour's id. Excluding by `pixel_id` cannot.

**No re-download is needed to fix this.** The label collision lost memberships,
not pixels: every pixel of the union is in the existing table under *some*
polygon's id, and `all_touched=False` means the union of the rasterised pixels
is exactly the set of pixel centres inside any polygon. So the correct
membership is recoverable by re-running the point-in-polygon test on the
deduplicated pixel centres, which is seconds of work against ~31 GB of reads.
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

    Returns

    * `pixels` -- one row per (pixel_id, year) with the 64 bands, so a piece of
      ground carries its embedding once no matter how many polygons map it;
    * `sites` -- one row per pixel_id with `utm_zone, x, y`, the geometry needed
      to rebuild membership and to write results back out.

    Deduplication is on the pixel's own identity rather than on read order: the
    same ground read from two tiles agrees to the bit, since the annual tiles of
    a UTM zone share one grid.
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

    Many-to-many by construction. Done per UTM zone, because `sites` holds
    coordinates in each pixel's own zone and reprojecting the polygons once per
    zone is far cheaper than reprojecting a million points.

    `all_touched=False` in the extractor means membership is defined by the
    pixel *centre*, so `predicate="within"` here reproduces exactly the same
    rule -- the fix restores memberships, it does not change the definition.
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

    `buffer_m` widens the test past strict intersection: two stands separated by
    one 10 m pixel are not independent evidence about each other, and a donor
    that close to the probe is a leak whether or not the geometries technically
    meet. Used to keep near neighbours out of a probe's reference pool.

    Returns `poly_id, poly_id2, frac` where `frac` is the share of the FIRST
    polygon's area that the second covers, so the table is asymmetric and must
    be read in that direction.
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

    A pixel mapped `god` by MI and `dårlig` by NiN5k is not evidence for either
    pool. Rather than let draw order decide, such a pixel is given the role
    `conflict` and used as a reference by neither. The precedence when there is
    no contradiction is: an explicit label beats no label, so a pixel in a `god`
    polygon and an unlabelled HB13 polygon is `good`.

    Returns one row per pixel: `pixel_id, forest_type, role, n_polygons`.
    `forest_type` is `mixed` in the (currently empty) case of a pixel mapped as
    both forest types -- checked rather than assumed.
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
