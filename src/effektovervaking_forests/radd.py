"""RADD Europe alert tiles over the mapped stands, for the dashboard.

RADD (Wageningen; Sentinel-1, 10 m) is an Earth Engine image collection. As in
the old inspector (Geethen_forests/scripts/radd_europe_tiles.py), the layers
are rendered the way the RADD app renders them and served as Earth Engine XYZ
tiles. Here they are also masked to the stands, so only alerts inside the
monitored forest are drawn.

Map IDs expire (they are bound to the minting credentials, within days), so
`forestmon publish radd` belongs in the nightly cycle; the app says when the
file it reads is old. Needs the `gee` extra and an authenticated account.
"""

from __future__ import annotations

import json
import time

import geopandas as gpd

RADD_IC = "projects/wurnrt-raddeurope/assets/01_NRT/00_Operational/V1_IC"
APP_URL = "https://wurnrt-raddeurope.projects.earthengine.app/view/radd-europe"
# Colours from the RADD app (App.paletteAlert / App.paletteAlertDate).
CONFIRMED, UNCONFIRMED, UNCONFIRMED_OPACITY = "EA7E7D", "00ffff", 0.75
DATE_PALETTE = ["ffffcc", "ffeda0", "fed976", "feb24c", "fd8d3c",
                "fc4e2a", "e31a1c", "bd0026", "800026"]  # fmt: skip
DATE_MIN_MILLIS = 1577836800000  # 2020-01-01, the app's ramp floor


def latest_image(ee, geography: str = "europe"):
    """Newest operational alert image for a geography, as the app picks it."""
    return ee.Image(
        ee.ImageCollection(RADD_IC)
        .filterMetadata("layer", "contains", "alert")
        .filterMetadata("geography", "equals", geography)
        .sort("system:time_end", False)
        .first()
    )


def alert_rgb(ee, img):
    """Confirmed (3) over unconfirmed (2), as the app draws them."""
    a = img.select("Alert").unmask(0, False)
    confirmed = a.eq(3).selfMask().visualize(min=0, max=1, palette=[CONFIRMED])
    unconfirmed = (
        a.eq(2)
        .selfMask()
        .visualize(min=0, max=1, palette=[UNCONFIRMED], opacity=UNCONFIRMED_OPACITY)
    )
    return ee.ImageCollection([confirmed, unconfirmed]).mosaic()


def alert_date_rgb(img, max_millis: int):
    """Alert date (YYDOY) ramped 2020 -> now, with the app's flat 365-day year."""
    date = img.select("Date")
    days = date.divide(1000).floor().add(2000).subtract(1970).multiply(365)
    millis = days.add(date.mod(1000)).multiply(86400).multiply(1000)
    return millis.selfMask().visualize(
        min=DATE_MIN_MILLIS, max=max_millis, palette=DATE_PALETTE
    )


def mint_tiles(polys: gpd.GeoDataFrame, project: str | None = None) -> dict:
    """XYZ templates for the alert and alert-date layers, inside `polys` only."""
    import ee

    ee.Initialize(project=project)
    img = latest_image(ee)
    # Simplified to keep the request small; 1e-5 deg is about 1 m.
    stands = ee.FeatureCollection(
        json.loads(
            polys.to_crs(4326)[["geometry"]].simplify(1e-5).to_json(drop_id=True)
        )
    )
    inside = ee.Image(0).paint(stands, 1).selfMask()
    props = img.toDictionary(["system:index", "version_date"]).getInfo()
    now = int(time.time() * 1000)
    out = {
        "source": RADD_IC,
        "app": APP_URL,
        "image_id": props.get("system:index"),
        "version_date": props.get("version_date"),
        "minted_at": now,
        "layers": {},
    }
    for name, vis in (
        ("alert", alert_rgb(ee, img)),
        ("alert_date", alert_date_rgb(img, now)),
    ):
        m = ee.data.getMapId({"image": vis.updateMask(inside)})
        out["layers"][name] = {"tiles": m["tile_fetcher"].url_format}
    return out
