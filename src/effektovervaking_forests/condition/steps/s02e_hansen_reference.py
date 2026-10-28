"""Step 2e -- the undisturbed reference the trajectory gate is calibrated on.

The condition side has field labels; the trajectory side has none, so "improving"
and "declining" have so far been unfalsifiable. Hansen's annual loss year is the
one label available here that is genuinely independent of AlphaEarth -- different
sensor (Landsat), different method, different producer -- and it is annual, which
is the grain the trend test works at.

It is a *coarse* truth and is treated as one: 30 m against 10 m, canopy loss only,
and it misses selective removal and understorey degradation entirely. So it is used
for two things only -- a sensitivity floor (of the pixels Hansen says were cleared,
how many does the trend test call `declining`?) and a timing check -- never as a
complete label. Absence of Hansen loss is NOT evidence of no degradation.

Sampling: AlphaEarth pixels are 10 m and Hansen cells are 30 m on a 1/4000-degree
grid, so the 168,832 pixel centres collapse to ~25k distinct Hansen cells. Snapping
first turns 34 `sampleRegions` calls into 6.

Two things downstream need it. `03_trajectory.py` calibrates its slope gate on
the pixels this step says were NOT cleared, which is what fixes the
false-positive rate of `declining` at a stated value instead of at whatever a
p-value happens to give. And `05_validate.py` reports recall against the pixels
it says were.

Needs an Earth Engine account (`ee.Initialize`); it is the only step in the
pipeline that does. Everything still runs without it -- `03_trajectory.py` falls
back to the significance rule and says so -- but the trajectory classes are then
uncalibrated.

Writes `outputs/data/hansen_pixels.parquet` (pixel_id, lon, lat, loss_year,
treecover2000, datamask).

Run:  forestmon condition step 02e
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import pyproj

from effektovervaking_forests import config
from effektovervaking_forests.condition.paths import HANSEN_PIXELS, OUT_DATA

ASSET = "UMD/hansen/global_forest_change_2025_v1_13"
GRID = 1.0 / 4000.0  # Hansen's native cell size in degrees
OUT = HANSEN_PIXELS
CHUNK = 5000


def lonlat(sites: pd.DataFrame) -> pd.DataFrame:
    """UTM (per zone, as the AlphaEarth tiles store it) -> WGS84 degrees."""
    out = np.full((len(sites), 2), np.nan)
    for zone, g in sites.groupby("utm_zone"):
        epsg = 32600 + int(zone[:-1])
        tr = pyproj.Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
        lo, la = tr.transform(g["x"].to_numpy(), g["y"].to_numpy())
        out[g.index.to_numpy(), 0] = lo
        out[g.index.to_numpy(), 1] = la
    return pd.DataFrame({"lon": out[:, 0], "lat": out[:, 1]}, index=sites.index)


def main(argv: list[str] | None = None) -> None:
    import argparse

    import ee

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--project",
        default=config.EE_PROJECT,
        required=config.EE_PROJECT is None,
        help="Earth Engine cloud project to bill the sampling to "
        "(default: FORESTMON_EE_PROJECT)",
    )
    ap.add_argument("--asset", default=ASSET)
    args = ap.parse_args(argv)
    ee.Initialize(project=args.project)

    sites = pd.read_parquet(OUT_DATA / "pixel_sites.parquet").reset_index(drop=True)
    sites = pd.concat([sites, lonlat(sites)], axis=1)
    # snap to the Hansen grid centre so co-located AEF pixels share one query
    sites["gx"] = np.floor(sites["lon"] / GRID).astype(np.int64)
    sites["gy"] = np.floor(sites["lat"] / GRID).astype(np.int64)
    cells = sites[["gx", "gy"]].drop_duplicates().reset_index(drop=True)
    cells["clon"] = (cells["gx"] + 0.5) * GRID
    cells["clat"] = (cells["gy"] + 0.5) * GRID
    print(f"{len(sites):,} AEF pixels -> {len(cells):,} distinct Hansen cells")

    img = ee.Image(args.asset).select(["lossyear", "treecover2000", "datamask"])
    recs = []
    for i in range(0, len(cells), CHUNK):
        part = cells.iloc[i : i + CHUNK]
        fc = ee.FeatureCollection(
            [
                ee.Feature(ee.Geometry.Point([float(a), float(b)]), {"i": int(j)})
                for j, a, b in zip(part.index, part["clon"], part["clat"])
            ]
        )
        got = img.sampleRegions(collection=fc, scale=30, geometries=False).getInfo()
        for f in got["features"]:
            recs.append(f["properties"])
        print(f"  {i + len(part):,}/{len(cells):,}", flush=True)

    got = pd.DataFrame(recs).set_index("i")
    cells = cells.join(got)
    px = sites.merge(
        cells[["gx", "gy", "lossyear", "treecover2000", "datamask"]],
        on=["gx", "gy"],
        how="left",
    )
    px["loss_year"] = np.where(
        px["lossyear"].fillna(0) > 0, 2000 + px["lossyear"], np.nan
    )
    px[["pixel_id", "lon", "lat", "loss_year", "treecover2000", "datamask"]].to_parquet(
        OUT, index=False
    )
    n = int(px["loss_year"].notna().sum())
    print(
        f"\n{n:,} of {len(px):,} AEF pixels carry a Hansen loss year "
        f"({n / len(px):.2%})"
    )
    print(
        px.loc[px["loss_year"].notna(), "loss_year"]
        .value_counts()
        .sort_index()
        .to_string()
    )
    print(f"-> {OUT}")


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("02e", sys.argv[1:])
