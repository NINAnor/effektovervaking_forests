"""Optional check -- do the Source Cooperative COGs agree with Earth Engine?

The mirror is community-maintained and stores the embedding quantised to int8,
so it is worth confirming once that what we read matches
`GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL` at the same coordinates and year, the way
the RADD work in this repo gated its CDSE-vs-GEE parity before trusting it.

Two things are reported, and they answer different questions:

  * **cosine similarity** between the mirror vector and the GEE vector, per
    pixel. This is the one that matters: every downstream number here is
    cosine-based, so a similarity of ~1.0 means the pipeline is unaffected even
    if the absolute scale is off.
  * **the implied scale factor** (least-squares GEE/mirror ratio), which tells
    you whether `AEF_INT8_SCALE = 1/127` is the right de-quantisation. A ratio
    that is consistent across pixels but not 1.0 means only that the constant is
    wrong, which changes nothing but the units.

Needs an Earth Engine account and `earthengine-api`; everything else in this
package runs without one.

Run:  forestmon condition step 09 -- --project <ee-project>
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

from effektovervaking_forests import config
from effektovervaking_forests.condition.paths import BANDS, OUT_DATA, POLY_PIXELS

COLLECTION = "GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL"
OUT = OUT_DATA / "gee_parity.csv"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--project",
        default=config.EE_PROJECT,
        required=config.EE_PROJECT is None,
        help="Earth Engine cloud project (default: FORESTMON_EE_PROJECT)",
    )
    ap.add_argument("--n", type=int, default=200, help="pixels to compare")
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    import ee
    import geopandas as gpd

    ee.Initialize(
        project=args.project, opt_url="https://earthengine-highvolume.googleapis.com"
    )

    px = pd.read_parquet(POLY_PIXELS)
    px = px[px["year"] == args.year]
    if px.empty:
        raise SystemExit(f"no extracted pixels for {args.year}; run step 2 first")
    sample = px.sample(min(args.n, len(px)), random_state=args.seed)

    # x/y are UTM in the tile's zone; EE wants lon/lat.
    frames = []
    for zone, grp in sample.groupby("utm_zone"):
        epsg = 32600 + int(zone[:-1]) + (100 if zone.endswith("S") else 0)
        g = gpd.GeoSeries(gpd.points_from_xy(grp.x, grp.y), crs=f"EPSG:{epsg}").to_crs(
            4326
        )
        frames.append(grp.assign(lon=g.x.to_numpy(), lat=g.y.to_numpy()))
    sample = pd.concat(frames)

    img = (
        ee.ImageCollection(COLLECTION)
        .filterDate(f"{args.year}-01-01", f"{args.year + 1}-01-01")
        .mosaic()
    )
    fc = ee.FeatureCollection(
        [
            ee.Feature(ee.Geometry.Point([r.lon, r.lat]), {"i": int(i)})
            for i, r in enumerate(sample.itertuples())
        ]
    )
    got = ee.data.computeFeatures(
        {
            "expression": img.sampleRegions(collection=fc, scale=10, geometries=False),
            "fileFormat": "PANDAS_DATAFRAME",
        }
    )
    if got.empty:
        raise SystemExit("Earth Engine returned no samples")

    gee = got.set_index("i")[list(BANDS)].to_numpy(dtype=float)
    mine = sample.iloc[got["i"].to_numpy()][list(BANDS)].to_numpy(dtype=float)
    ok = np.isfinite(gee).all(1) & np.isfinite(mine).all(1)
    gee, mine = gee[ok], mine[ok]

    cos = (gee * mine).sum(1) / (
        np.linalg.norm(gee, axis=1) * np.linalg.norm(mine, axis=1) + 1e-12
    )
    scale = float((gee * mine).sum() / (mine * mine).sum())

    pd.DataFrame({"cosine": cos}).to_csv(OUT, index=False)
    print(f"{len(cos)} pixels compared for {args.year}")
    print(
        f"  cosine similarity: median {np.median(cos):.6f}, "
        f"min {cos.min():.6f}, frac < 0.999 = {np.mean(cos < 0.999):.4f}"
    )
    print(
        f"  implied scale (GEE / mirror): {scale:.4f} "
        f"(1.0 means AEF_INT8_SCALE = 1/127 is right)"
    )
    if np.median(cos) < 0.999:
        print(
            "  ! the mirror and Earth Engine disagree in DIRECTION, not just "
            "scale. That does affect every score here -- investigate before "
            "trusting the outputs."
        )
    else:
        print("  cosine agreement is what the pipeline depends on: OK.")
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("09", sys.argv[1:])
