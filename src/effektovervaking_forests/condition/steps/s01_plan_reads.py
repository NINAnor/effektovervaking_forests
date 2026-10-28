"""Step 1 -- work out (and cost) exactly which AlphaEarth data the AOI needs.

The mirror's COGs are band-interleaved with 1024x1024 blocks, so the unit of
cost is the *block*, not the pixel: reading one block costs ~40 MB and 6-10 s
whether you want one pixel of it or a million (see `condition.aef`). This step
resolves the polygon set to (tile, year, block) triples, so the run is planned
rather than discovered halfway through.

Writes `outputs/data/read_plan.parquet` and prints the totals. Nothing here
touches pixel data -- it is index arithmetic and takes seconds.

Run:  forestmon condition step 01
"""

from __future__ import annotations

import argparse
import sys

import geopandas as gpd
import pandas as pd

from effektovervaking_forests.condition.aef import (
    blocks_for_geometries,
    tiles_for_geometries,
)
from effektovervaking_forests.condition.paths import AEF_YEARS, OUT_DATA

POLY_GEO = OUT_DATA / "polygons_geo.parquet"
PLAN = OUT_DATA / "read_plan.parquet"

# Measured on this workstation against data.source.coop: one 1024x1024x64 block
# read took 6-10 s and moved ~40 MB of compressed data. Used only to print an
# estimate, so being off by 2x still gives the right order of magnitude.
SECONDS_PER_BLOCK = 8.0
MB_PER_BLOCK = 40.0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=int, nargs="*", default=list(AEF_YEARS))
    ap.add_argument("--forest-type", choices=["olivinskog", "kalklindeskog"])
    ap.add_argument(
        "--workers",
        type=int,
        default=8,
        help="concurrency assumed when estimating wall-clock",
    )
    args = ap.parse_args(argv)

    poly = gpd.read_parquet(POLY_GEO)
    if args.forest_type:
        poly = poly[poly.forest_type == args.forest_type]
    print(f"{len(poly)} polygons, years {min(args.years)}-{max(args.years)}")

    tiles = tiles_for_geometries(poly, args.years)
    print(
        f"{len(tiles)} (tile, year) pairs across {tiles.utm_zone.nunique()} UTM zones"
    )

    rows = []
    for t in tiles.itertuples():
        td = t._asdict()
        local = poly.to_crs(t.crs)
        tb = (t.utm_west, t.utm_south, t.utm_east, t.utm_north)
        sub = local[
            local.geometry.apply(
                lambda g: (
                    not (
                        g.bounds[2] <= tb[0]
                        or g.bounds[0] >= tb[2]
                        or g.bounds[3] <= tb[1]
                        or g.bounds[1] >= tb[3]
                    )
                )
            )
        ]
        if sub.empty:
            continue
        for b in blocks_for_geometries(sub, td):
            rows.append(
                {
                    "year": int(t.year),
                    "utm_zone": t.utm_zone,
                    "tile_id": t.tile_id,
                    "uri": t.uri,
                    "crs": t.crs,
                    "brow": b.brow,
                    "bcol": b.bcol,
                    "n_polygons": int(
                        sub.geometry.apply(
                            lambda g, bb=b.bounds(td): (
                                not (
                                    g.bounds[2] <= bb[0]
                                    or g.bounds[0] >= bb[2]
                                    or g.bounds[3] <= bb[1]
                                    or g.bounds[1] >= bb[3]
                                )
                            )
                        ).sum()
                    ),
                }
            )

    plan = pd.DataFrame(rows)
    if plan.empty:
        raise SystemExit("no blocks resolved -- check the AOI and the index")
    plan.to_parquet(PLAN, index=False)

    n = len(plan)
    per_year = plan.groupby("year").size()
    print(f"\n{n} block reads ({per_year.mean():.0f} per year)")
    print(per_year.to_string())
    print(f"\nestimated transfer  ~{n * MB_PER_BLOCK / 1000:.1f} GB")
    print(
        f"estimated wall clock ~{n * SECONDS_PER_BLOCK / 3600:.1f} h serial, "
        f"~{n * SECONDS_PER_BLOCK / 3600 / args.workers:.1f} h at {args.workers} workers"
    )
    print(f"\nplan -> {PLAN}")
    print("\npolygons per block:")
    print(
        plan["n_polygons"]
        .describe()[["count", "mean", "50%", "max"]]
        .round(1)
        .to_string()
    )


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("01", sys.argv[1:])
