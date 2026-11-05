"""Step 2b -- overlap-safe pixel <-> stand membership.

Deduplicates the step 02 pixel table to one embedding per piece of ground and
recomputes polygon membership as a many-to-many point-in-polygon join (see
`condition/membership.py`). No re-download is needed. Reads the step 02 pixel
table and the step 00 polygons.

Outputs
    pixels.parquet           pixel_id, year, A00..A63   (one row per ground-year)
    pixel_sites.parquet      pixel_id, utm_zone, x, y
    pixel_polygons.parquet   pixel_id, poly_id          (many-to-many)
    pixel_roles.parquet      pixel_id, forest_type, role, n_polygons
    polygon_overlap.parquet  poly_id, poly_id2, frac

Run:  forestmon condition step 02b
"""

from __future__ import annotations

import argparse
import sys

import geopandas as gpd
import pandas as pd

from effektovervaking_forests.condition.blocks import INTERIOR_M_DEFAULT, edge_distance
from effektovervaking_forests.condition.membership import (
    build_membership,
    dedupe_pixels,
    pixel_roles,
    polygon_overlaps,
)
from effektovervaking_forests.condition.paths import (
    OUT_DATA,
    PIXEL_POLYGONS,
    PIXELS,
    POLY_OVERLAP,
    POLY_PIXELS,
    POLYGONS,
)

SITES = OUT_DATA / "pixel_sites.parquet"
ROLES = OUT_DATA / "pixel_roles.parquet"
POLY_GEO = OUT_DATA / "polygons_geo.parquet"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--neighbour-buffer-m",
        type=float,
        default=20.0,
        help="how far past strict intersection two stands still "
        "count as neighbours, and so as ineligible to be each "
        "other's reference (default one 10 m pixel either side)",
    )
    args = ap.parse_args(argv)

    px = pd.read_parquet(POLY_PIXELS)
    poly = gpd.read_parquet(POLY_GEO)
    polygons = pd.read_parquet(POLYGONS)
    print(f"{len(px):,} pixel-polygon-year rows in, {len(poly)} polygons")

    pixels, sites = dedupe_pixels(px)
    sites.to_parquet(SITES, index=False)
    pixels.to_parquet(PIXELS, index=False, compression="zstd")
    print(
        f"{len(sites):,} distinct pixels, {len(pixels):,} pixel-years "
        f"-> {PIXELS.name}, {SITES.name}"
    )

    mem = build_membership(sites, poly)
    # Distance inside the claiming stand; bad stands are smaller, so edge and
    # degraded are confounded (METHODS.md, E10). Keep this column in per-pixel output.
    mem["edge_m"] = edge_distance(mem, sites, poly)
    mem.to_parquet(PIXEL_POLYGONS, index=False, compression="zstd")
    per_poly = mem.groupby("poly_id").size()
    n_multi = int((mem.groupby("pixel_id").size() > 1).sum())
    print(f"\n{len(mem):,} pixel-polygon memberships -> {PIXEL_POLYGONS.name}")
    print(
        f"  {n_multi:,} pixels ({n_multi / len(sites):.1%}) are claimed by more "
        "than one polygon; under the old label raster each of those was "
        "counted for exactly one of them"
    )
    interior = mem["edge_m"] >= INTERIOR_M_DEFAULT
    no_core = set(mem["poly_id"].unique()) - set(mem.loc[interior, "poly_id"].unique())
    print(
        f"  {int(interior.sum()):,} of {len(mem):,} memberships "
        f"({interior.mean():.1%}) are at least {INTERIOR_M_DEFAULT:g} m inside "
        f"their stand; {len(no_core):,} stands have no interior pixel at all "
        "and can only ever be scored on mixed ones"
    )

    old = px[px.year == px.year.max()].groupby("poly_id").size().rename("old")
    cmp_ = polygons[["poly_id", "ref_role", "approx_pixels"]].set_index("poly_id")
    cmp_ = cmp_.join(old).join(per_poly.rename("new")).fillna(0)
    gained = cmp_[cmp_["new"] > cmp_["old"]]
    print(
        f"\n{len(gained)} polygons gained pixels; "
        f"{int((cmp_['old'] <= 1).sum())} previously had <=1 pixel, "
        f"now {int((cmp_['new'] <= 1).sum())} do"
    )
    print("\nmedian pixels per polygon, before -> after, by reference role:")
    print(cmp_.groupby("ref_role")[["old", "new"]].median().astype(int).to_string())

    roles = pixel_roles(mem, polygons)
    roles.to_parquet(ROLES, index=False)
    print(f"\npixel role (after resolving polygons that disagree) -> {ROLES.name}:")
    print(pd.crosstab(roles["role"], roles["forest_type"]).to_string())
    n_conf = int((roles["role"] == "conflict").sum())
    if n_conf:
        print(
            f"  {n_conf} pixels are mapped with contradictory labels by "
            "different layers and are used as a reference by neither pool"
        )

    ov = polygon_overlaps(poly, buffer_m=args.neighbour_buffer_m)
    ov.to_parquet(POLY_OVERLAP, index=False)
    print(
        f"\n{len(ov)} ordered neighbour pairs within "
        f"{args.neighbour_buffer_m:g} m -> {POLY_OVERLAP.name} "
        f"({ov.poly_id.nunique()} of {len(poly)} polygons have a neighbour)"
    )


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("02b", sys.argv[1:])
