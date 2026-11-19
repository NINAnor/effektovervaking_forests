"""Bake the dashboard's JSON from the alert, condition and C2C parquet.

The app (app/) is static and reads only what this writes: no parquet reader
in the browser. Inputs: the S-CCD alert tables (the nightly cycle's
operational/alerts, or sccd/operational/alerts under the project's S3 prefix),
C2C (c2c/parquet under the prefix) and condition (one folder, the public dms
dataset, which the app also reads its cells and downloads from). One alert
row is a break event on a 10 m pixel; only rows the site would publish are
kept (`alerts.publishable_mask`), minus reviewer-rejected ones, and areas
count each pixel once. The C2C loss pixels, taken as likely forest loss, give
the forest area remaining each year.

downloads/ holds the layers for GIS users, as the map shows them: stands and
alerts as GeoPackage, C2C as GeoParquet. Condition is downloaded from its bucket.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import requests
import shapely

from . import alerts as alerts_mod

AOIS = ("olivinskog", "kalklindeskog")
KINDS = ("loss", "gain")
PIXEL_HA = 0.01
# The app's Certainty choices (all, medium and high, high): monthly areas per choice.
CONF_TIERS = (0.0, 0.34, 0.67)
SCHEMA = 9  # bump when a file's shape changes; the app refuses other versions
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
# Per-stand condition joined onto the polygons, plus its score each year
# (dor_global_<year>: the years come from the data, so a new year needs no code).
STAND_COLS = [
    "poly_id",
    "tilstand",
    "survey_year",
    "condition_local",
    "condition_global",
    "threshold_global",
    "trajectory",
]
# C2C: a loss is an NBR drop of at least the cut-off tuned on labelled cells (as
# the report and the app's map). C2C dates a loss to the first yearly composite
# after it, so loss years run from 2018 (the 2017 composite is the first).
C2C_COLS = ["forest_type", "pix_id", "change_year", "magnitude"]
C2C_GATE = 0.175
C2C_FIRST = 2018


# The condition folder (dms dataset) must hold these, plus the blocks index.csv lists.
CONDITION_FILES = (
    "polygons_geo.parquet",
    "condition_stands.parquet",
    "condition_pixels.parquet",
    "condition_pixels.pmtiles",
    "index.csv",
)


def default_source() -> str | None:
    """FORESTMON_DASHBOARD_SOURCE: the project's public S3 prefix URL, or a folder."""
    return os.environ.get("FORESTMON_DASHBOARD_SOURCE") or None


def default_condition() -> str | None:
    """FORESTMON_CONDITION_SOURCE: the condition dataset (dms URL), or a folder."""
    return os.environ.get("FORESTMON_CONDITION_SOURCE") or None


def _is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def missing_condition_files(condition: str, tmp: Path) -> list[str]:
    """What the app needs from the condition folder and cannot find there."""
    blocks = pd.read_csv(_local(condition, "index.csv", tmp))["file"].tolist()
    missing = []
    for name in [*CONDITION_FILES, *blocks]:
        if _is_url(condition):
            r = requests.head(f"{condition.rstrip('/')}/{name}", timeout=30)
            ok = r.ok
        else:
            ok = (Path(condition) / name).is_file()
        if not ok:
            missing.append(name)
    return missing


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
    years = list(range(C2C_FIRST, int(np.floor(c2c["change_year"].max())) + 2))
    out = {"years": years}
    for aoi in AOIS:
        mapped = polys.loc[polys["forest_type"] == aoi].geometry.union_all().area / 1e4
        out[aoi] = {
            "mapped_ha": round(mapped, 2),
            "loss_ha": [round(int(per.get((aoi, y), 0)) * PIXEL_HA, 2) for y in years],
        }
    return out


def _c2c(source: str, tmp: Path) -> gpd.GeoDataFrame | None:
    """The C2C pixels, or None when the source has none (chart and download hidden)."""
    try:
        gdf = gpd.read_parquet(_local(source, "c2c/parquet/c2c_pixels.parquet", tmp))
    except (FileNotFoundError, requests.HTTPError):
        return None
    return gdf.drop(columns=["bbox", "__index_level_0__"], errors="ignore")


def _write(obj, path: Path) -> str:
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(obj, separators=(",", ":"), allow_nan=False) + "\n"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path.name


def _download(dl: Path, name: str, write) -> dict:
    """Write in a local temp dir (SQLite locking fails on CIFS). A GeoPackage is
    zipped (a quarter the size); both keep fixed timestamps, so a bake of
    unchanged data leaves the committed files unchanged."""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t) / name
        write(tmp)
        out = dl / (f"{name}.zip" if name.endswith(".gpkg") else name)
        if out.suffix == ".zip":
            info = zipfile.ZipInfo(name, date_time=(2017, 1, 1, 0, 0, 0))
            with zipfile.ZipFile(out, "w") as z:
                z.writestr(info, tmp.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
        else:
            shutil.copyfile(tmp, out)
    return {"file": f"downloads/{out.name}", "bytes": out.stat().st_size}


def _gpkg(gdf: gpd.GeoDataFrame, layer: str):
    def write(path: Path) -> None:
        pyogrio.set_gdal_config_options({"OGR_CURRENT_DATE": "2017-01-01T00:00:00Z"})
        try:
            gdf.to_file(path, layer=layer, engine="pyogrio")
        finally:
            pyogrio.set_gdal_config_options({"OGR_CURRENT_DATE": None})

    return write


def alert_points(px: pd.DataFrame) -> gpd.GeoDataFrame:
    """The map's alert pixels as points: one row per (pixel, kind)."""
    cols = ["aoi", "kind", "pix_id", "first", "last", "conf", "mag", "n_events"]
    out = px[cols].rename(
        columns={"aoi": "forest_type", "first": "first_break", "last": "last_break"}
    )
    geom = gpd.points_from_xy(px["lon"], px["lat"], crs=4326)
    return gpd.GeoDataFrame(out.assign(conf=px["conf"].round(2)), geometry=geom)


def bake(
    source: str, condition: str, out_dir: Path | str, alerts: str | None = None
) -> dict:
    """source: the S3 prefix (C2C, and the alerts unless `alerts` names their folder).
    condition: the condition folder; refused while a file the app needs is missing."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    alerts = alerts or f"{source.rstrip('/')}/sccd/operational/alerts"
    with tempfile.TemporaryDirectory(prefix="forestmon_dash_") as t:
        tmp = Path(t)
        missing = missing_condition_files(condition, tmp)
        if missing:
            raise ValueError(f"not in {condition}: {', '.join(missing)}")
        events = pd.concat(
            [
                pd.read_parquet(
                    _local(alerts, f"{a}_alerts.parquet", tmp), columns=ALERT_COLS
                ).assign(aoi=a)
                for a in AOIS
            ],
            ignore_index=True,
        )
        keep = alerts_mod.publishable_mask(events) & (events["status"] != "rejected")
        events = events[keep]
        polys = gpd.read_parquet(_local(condition, "polygons_geo.parquet", tmp))
        stands = pd.read_parquet(_local(condition, "condition_stands.parquet", tmp))
        c2c = _c2c(source, tmp)

    years = sorted(
        int(m[1]) for c in stands if (m := re.fullmatch(r"dor_global_(\d{4})", c))
    )
    if not years:
        raise ValueError("condition_stands.parquet has no dor_global_<year> columns")
    yearly = [f"dor_global_{y}" for y in years]
    stands = stands[STAND_COLS + yearly]
    polys = polys[polys["forest_type"].isin(AOIS)]
    if polys.crs is None or not polys.crs.is_projected:
        raise ValueError("polygons_geo.parquet must be in a projected CRS")
    polys = polys.assign(area_ha=polys.geometry.area / 1e4)
    missing = [a for a in AOIS if a not in set(polys["forest_type"])]
    if missing:
        raise ValueError(f"no polygons for {missing}")

    if events.empty:
        raise ValueError(f"no publishable alerts in {alerts}")
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
    # The last year's score stays a column: the map colours by it.
    web = web.drop(columns=yearly[:-1]).assign(**{yearly[-1]: web[yearly[-1]].round(3)})
    geom = web.geometry.simplify(1e-5, preserve_topology=True)
    web = web.assign(geometry=shapely.set_precision(geom.values, 1e-5))
    # na="null": the app reads a missing score as "no score".
    feats = json.loads(web.to_json(drop_id=True, na="null"))
    files["polygons"] = _write(feats, out_dir / "polygons.geojson")
    files["monthly"] = _write(monthly(px), out_dir / "monthly.json")

    dl = out_dir / "downloads"
    dl.mkdir(exist_ok=True)
    downloads = {
        "stands": {
            "gpkg": _download(
                dl, "stands.gpkg", _gpkg(web.drop(columns="series"), "stands")
            )
        },
        "alerts": {
            "gpkg": _download(dl, "alerts.gpkg", _gpkg(alert_points(px), "alerts"))
        },
    }
    if c2c is not None:
        downloads["c2c"] = {"geoparquet": _download(dl, "c2c.parquet", c2c.to_parquet)}
    # Condition is too big for the repo: linked where the app reads its cells.
    cond_url = condition.rstrip("/") + "/" if _is_url(condition) else None
    if cond_url:
        downloads["condition"] = {
            "geoparquet": {"url": cond_url + "condition_pixels.parquet"},
            "cogs": {"url": cond_url + "index.csv"},
        }

    counts = px.groupby(["aoi", "kind"]).size()
    index = {
        "schema": SCHEMA,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        # The condition folder the app reads its cells from; null when local.
        "condition": cond_url,
        "forest_types": list(AOIS),
        "polygon_count": int(len(polys)),
        "polygon_counts": {
            t: int(n) for t, n in polys["forest_type"].value_counts().items()
        },
        "pixels": {a: {k: int(counts.get((a, k), 0)) for k in KINDS} for a in AOIS},
        "date_range": [f"{px['first'].min():%Y-%m-%d}", f"{px['last'].max():%Y-%m-%d}"],
        "years": years,
        "conf_tiers": list(CONF_TIERS),
        "forest_area": None if c2c is None else forest_area(polys, c2c),
        "files": files,
        "downloads": downloads,
    }
    # Same data as the last bake: keep its time, so the nightly cycle commits nothing.
    try:
        last = json.loads((out_dir / "index.json").read_text())
        if {**last, "generated_at": None} == {**index, "generated_at": None}:
            index["generated_at"] = last["generated_at"]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    _write(index, out_dir / "index.json")  # last: the app reads it first
    return index
