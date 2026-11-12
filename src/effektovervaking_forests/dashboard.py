"""Bake the dashboard's JSON from the published alert and condition parquet.

The app (app/) is static and reads only what this writes: no parquet reader
in the browser. Inputs are the hand-published products under the project's
S3 prefix (alerts/parquet, condition/parquet), or a local copy laid out the
same way. One alert row is a break event on a 10 m pixel; only rows the site
would publish are kept (`alerts.publishable_mask`), minus reviewer-rejected
ones, and areas count each pixel once. The C2C loss pixels (c2c/parquet), taken
as likely forest loss, give the forest area remaining each year.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import shapely

from . import alerts as alerts_mod

AOIS = ("olivinskog", "kalklindeskog")
KINDS = ("loss", "gain")
PIXEL_HA = 0.01
# The app's Certainty choices (all, medium and high, high): monthly areas per choice.
CONF_TIERS = (0.0, 0.34, 0.67)
SCHEMA = 8  # bump when a file's shape changes; the app refuses other versions
ALERT_COLS = [
    "pix_id",
    "lon",
    "lat",
    "break_date",
    "kind",
    "conf_score",
    "status",
    "magnitude",
    "gate",
]
# Per-stand condition joined onto the polygons (condition/parquet).
STAND_COLS = [
    "poly_id",
    "tilstand",
    "survey_year",
    "condition_local",
    "condition_global",
    "threshold_global",
    "trajectory",
    *[f"dor_global_{y}" for y in range(2017, 2026)],
]
YEARS = list(range(2017, 2026))
# C2C: a loss is an NBR drop of at least the cut-off tuned on labelled cells (as
# the report and the app's map). C2C dates a loss to the first yearly composite
# after it, so loss years run 2018-2025.
C2C_COLS = ["forest_type", "pix_id", "change_year", "magnitude"]
C2C_GATE = 0.175
C2C_YEARS = list(range(2018, 2026))


def default_source() -> str | None:
    """FORESTMON_DASHBOARD_SOURCE: the project's public S3 prefix URL, or a folder."""
    return os.environ.get("FORESTMON_DASHBOARD_SOURCE") or None


def _local(source: str, rel: str, tmp: Path) -> Path:
    if not source.startswith(("http://", "https://")):
        return Path(source) / rel
    out = tmp / rel.replace("/", "_")
    with requests.get(f"{source.rstrip('/')}/{rel}", stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return out


def _month(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s).dt.strftime("%Y-%m")


def pixels(events: pd.DataFrame) -> pd.DataFrame:
    """One row per (pixel, kind): first break (for area) and latest (for the map)."""
    ev = events.assign(break_date=pd.to_datetime(events["break_date"]))
    g = ev.sort_values("break_date").groupby(["aoi", "kind", "pix_id"], sort=False)
    out = g.agg(
        lon=("lon", "last"),
        lat=("lat", "last"),
        first=("break_date", "first"),
        last=("break_date", "last"),
        conf=("conf_score", "last"),
        mag=("magnitude", "last"),
        n_events=("break_date", "size"),
    )
    return out.reset_index()


def monthly(px: pd.DataFrame) -> list[dict]:
    """New area per month by first break, one value per CONF_TIERS (as the map)."""
    # To the latest break, so the range matches index.date_range.
    months = pd.period_range(px["first"].min(), px["last"].max(), freq="M")
    month = px["first"].dt.to_period("M")
    conf = px["conf"].fillna(0).round(2)  # as points() hands it to the map filter
    rows = []
    for aoi in AOIS:
        new = {}
        for kind in KINDS:
            sel = (px["aoi"] == aoi) & (px["kind"] == kind)
            for t in CONF_TIERS:
                new[kind, t] = month[sel & (conf >= t)].value_counts()
        for m in months:
            row = {"period": str(m), "forest_type": aoi}
            for kind in KINDS:
                row[f"{kind}_ha"] = [
                    round(int(new[kind, t].get(m, 0)) * PIXEL_HA, 2) for t in CONF_TIERS
                ]
            rows.append(row)
    return rows


def points(px: pd.DataFrame) -> dict:
    """Columnar, rounded: ~1 m at 5 decimals; the latest break as YYYYMMDD."""
    return {
        "lon": px["lon"].round(5).tolist(),
        "lat": px["lat"].round(5).tolist(),
        "date": px["last"].dt.strftime("%Y%m%d").astype(int).tolist(),
        "conf": px["conf"].fillna(0).round(2).tolist(),  # unscored -> 0
        # Size of the latest break (|dB08 - dB12|), for the dot size.
        "mag": px["mag"].abs().fillna(0).round(3).tolist(),
        "n": px["n_events"].tolist(),
    }


def forest_area(polys: gpd.GeoDataFrame, c2c: pd.DataFrame) -> dict:
    """Mapped area per type (overlapping polygons once) and its C2C loss each year."""
    loss = c2c[c2c["magnitude"] <= -C2C_GATE].drop_duplicates(["forest_type", "pix_id"])
    # change_year X.996 is the end of year X: the loss shows in year X+1.
    year = np.floor(loss["change_year"]).astype(int) + 1
    per = loss.groupby(["forest_type", year]).size()
    out = {"years": C2C_YEARS}
    for aoi in AOIS:
        mapped = polys.loc[polys["forest_type"] == aoi].geometry.union_all().area / 1e4
        out[aoi] = {
            "mapped_ha": round(mapped, 2),
            "loss_ha": [round(int(per.get((aoi, y), 0)) * PIXEL_HA, 2) for y in C2C_YEARS],
        }
    return out


def _c2c(source: str, tmp: Path) -> pd.DataFrame | None:
    """The C2C pixels, or None when the source has none (the chart is then hidden)."""
    try:
        path = _local(source, "c2c/parquet/c2c_pixels.parquet", tmp)
        return pd.read_parquet(path, columns=C2C_COLS)
    except (FileNotFoundError, requests.HTTPError):
        return None


def _write(obj, path: Path) -> str:
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(obj, separators=(",", ":"), allow_nan=False)
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path.name


def bake(source: str, out_dir: Path | str) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="forestmon_dash_") as t:
        tmp = Path(t)
        events = pd.concat(
            [
                pd.read_parquet(
                    _local(source, f"alerts/parquet/{a}_alerts_all.parquet", tmp),
                    columns=ALERT_COLS,
                ).assign(aoi=a)
                for a in AOIS
            ],
            ignore_index=True,
        )
        keep = alerts_mod.publishable_mask(events) & (events["status"] != "rejected")
        events = events[keep]
        polys = gpd.read_parquet(
            _local(source, "condition/parquet/polygons_geo.parquet", tmp)
        )
        stands = pd.read_parquet(
            _local(source, "condition/parquet/condition_stands.parquet", tmp),
            columns=STAND_COLS,
        )
        c2c = _c2c(source, tmp)

    polys = polys[polys["forest_type"].isin(AOIS)]
    if polys.crs is None or not polys.crs.is_projected:
        raise ValueError("polygons_geo.parquet must be in a projected CRS")
    polys = polys.assign(area_ha=polys.geometry.area / 1e4)
    missing = [a for a in AOIS if a not in set(polys["forest_type"])]
    if missing:
        raise ValueError(f"no polygons for {missing}")

    if events.empty:
        raise ValueError(f"no publishable alerts in {source}")
    px = pixels(events)
    files = {"points": {}}
    for aoi in AOIS:
        files["points"][aoi] = {}
        for kind in KINDS:
            sel = px[(px["aoi"] == aoi) & (px["kind"] == kind)]
            files["points"][aoi][kind] = _write(
                points(sel), out_dir / f"points_{aoi}_{kind}.json"
            )

    web = polys[["poly_id", "forest_type", "layer", "area_ha", "geometry"]]
    web = web.merge(stands, on="poly_id", how="left", validate="one_to_one")
    web["survey_year"] = web["survey_year"].astype("Int64")  # stays int if unmatched
    # A point inside each stand, for finding small stands at overview zoom.
    inside = web.geometry.representative_point().to_crs(4326)
    yearly = [f"dor_global_{y}" for y in YEARS]
    web = web.assign(
        area_ha=web["area_ha"].round(2),
        threshold_global=web["threshold_global"].round(3),
        # The stand's condition each year, for the click panel's trajectory.
        series=web[yearly]
        .round(3)
        .astype(object)
        .where(web[yearly].notna(), None)
        .values.tolist(),
        lon=inside.x.round(5),
        lat=inside.y.round(5),
    ).to_crs(4326)
    web = web.drop(columns=yearly[:-1]).assign(
        dor_global_2025=web["dor_global_2025"].round(3)
    )
    geom = web.geometry.simplify(1e-5, preserve_topology=True)
    web = web.assign(geometry=shapely.set_precision(geom.values, 1e-5))
    # na="null": the app reads a missing score as "no score".
    feats = json.loads(web.to_json(drop_id=True, na="null"))
    files["polygons"] = _write(feats, out_dir / "polygons.geojson")
    files["monthly"] = _write(monthly(px), out_dir / "monthly.json")

    counts = px.groupby(["aoi", "kind"]).size()
    index = {
        "schema": SCHEMA,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "source": source if source.startswith(("http://", "https://")) else "local",
        "forest_types": list(AOIS),
        "polygon_count": int(len(polys)),
        "polygon_counts": {
            t: int(n) for t, n in polys["forest_type"].value_counts().items()
        },
        "pixels": {a: {k: int(counts.get((a, k), 0)) for k in KINDS} for a in AOIS},
        "date_range": [f"{px['first'].min():%Y-%m-%d}", f"{px['last'].max():%Y-%m-%d}"],
        "years": YEARS,
        "conf_tiers": list(CONF_TIERS),
        "forest_area": None if c2c is None else forest_area(polys, c2c),
        "files": files,
    }
    _write(index, out_dir / "index.json")  # last: the app reads it first
    return index
