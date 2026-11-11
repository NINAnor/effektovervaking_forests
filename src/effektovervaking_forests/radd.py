"""RADD Europe alerts inside the mapped stands, for the dashboard.

RADD (Wageningen; Sentinel-1, 10 m) is published as an Earth Engine image
collection. The old inspector drew it as Earth Engine map tiles, but map IDs
expire within hours, so a static site cannot hold them. Instead this samples
the alert pixels that fall inside the stands into a small table, which
`forestmon publish dashboard` bakes like the S-CCD alerts.

Bands, as in the RADD app: Alert 2 = unconfirmed, 3 = confirmed; Date = YYDOY.
Needs the `gee` extra and an authenticated Earth Engine account.
"""

from __future__ import annotations

import datetime as dt
import json

import geopandas as gpd
import pandas as pd

RADD_IC = "projects/wurnrt-raddeurope/assets/01_NRT/00_Operational/V1_IC"


def decode_yydoy(v: int) -> dt.date:
    """26031 -> 2026-01-31."""
    v = int(v)
    return dt.date(2000 + v // 1000, 1, 1) + dt.timedelta(days=v % 1000 - 1)


def latest_image(ee, geography: str = "europe"):
    ic = ee.ImageCollection(RADD_IC).filter(ee.Filter.eq("geography", geography))
    return ee.Image(ic.sort("system:time_start", False).first())


def sample(polys: gpd.GeoDataFrame, project: str | None = None) -> pd.DataFrame:
    """RADD alert pixels inside `polys` (needs `forest_type`), one row each."""
    import ee

    ee.Initialize(project=project)
    img = latest_image(ee)
    alert = img.select("Alert")
    pts = (
        img.select(["Alert", "Date"])
        .updateMask(alert.gt(0))
        .addBands(ee.Image.pixelLonLat())
    )
    rows = []
    wgs = polys.to_crs(4326)
    for aoi, g in wgs.groupby("forest_type"):
        # Simplified to keep the request small; 1e-5 deg is about 1 m.
        region = ee.FeatureCollection(
            json.loads(g[["geometry"]].simplify(1e-5).to_json(drop_id=True))
        )
        fc = pts.sampleRegions(
            collection=region, scale=10, projection=img.projection(), geometries=False
        )
        df = ee.data.computeFeatures(
            {"expression": fc, "fileFormat": "PANDAS_DATAFRAME"}
        )
        if len(df):
            rows.append(df.assign(aoi=aoi))
    if not rows:
        return pd.DataFrame(columns=["aoi", "lon", "lat", "alert", "date"])
    df = pd.concat(rows, ignore_index=True).rename(
        columns={"longitude": "lon", "latitude": "lat", "Alert": "alert"}
    )
    # A pixel in two overlapping stands is sampled twice.
    df = df.drop_duplicates(["aoi", "lon", "lat"])
    df["date"] = pd.to_datetime(df["Date"].map(decode_yydoy))
    df["image_id"] = img.id().getInfo()
    return df[["aoi", "lon", "lat", "alert", "date", "image_id"]].reset_index(drop=True)
