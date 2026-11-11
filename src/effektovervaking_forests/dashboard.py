"""Bake the dashboard's JSON from the published alert and condition parquet.

The app (app/) is static and reads only what this writes: no parquet reader
in the browser. Inputs are the hand-published products under the project's
S3 prefix (alerts/parquet, condition/parquet), or a local copy laid out the
same way. One alert row is a break event on a 10 m pixel; only rows the site
would publish are kept (`alerts.publishable_mask`), minus reviewer-rejected
ones, and areas count each pixel once.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
import shapely

from . import alerts as alerts_mod

AOIS = ("olivinskog", "kalklindeskog")
KINDS = ("loss", "gain")
PIXEL_HA = 0.01
SCHEMA = 3  # bump when a file's shape changes; the app refuses other versions
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
# Optional: RADD alerts sampled inside the stands (`forestmon publish radd`).
RADD_PATH = "radd/parquet/radd_alerts.parquet"


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


def _optional(source: str, rel: str, tmp: Path) -> Path | None:
    """Like `_local`, but None when the file is not there."""
    try:
        path = _local(source, rel, tmp)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (403, 404):
            return None
        raise
    return path if path.exists() else None


def radd_points(df: pd.DataFrame) -> dict:
    """Columnar RADD pixels: date as YYYY-MM-DD, confidence 3 high / 2 low."""
    return {
        "lon": df["lon"].round(5).tolist(),
        "lat": df["lat"].round(5).tolist(),
        "date": pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d").tolist(),
        "conf": df["alert"].astype(int).tolist(),
    }


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
        n_events=("break_date", "size"),
    )
    return out.reset_index()


def monthly(px: pd.DataFrame, baseline_ha: dict[str, float]) -> list[dict]:
    """New pixels per month by first break, and the cover left after loss."""
    # To the latest break, so the range matches index.period_range.
    months = pd.period_range(px["first"].min(), px["last"].max(), freq="M")
    rows = []
    for aoi in AOIS:
        new = {}
        for kind in KINDS:
            sel = px[(px["aoi"] == aoi) & (px["kind"] == kind)]
            new[kind] = sel["first"].dt.to_period("M").value_counts()
        cum_loss = 0.0
        for m in months:
            loss = int(new["loss"].get(m, 0))
            gain = int(new["gain"].get(m, 0))
            cum_loss += loss * PIXEL_HA
            rows.append(
                {
                    "period": str(m),
                    "forest_type": aoi,
                    "loss_ha": round(loss * PIXEL_HA, 2),
                    "gain_ha": round(gain * PIXEL_HA, 2),
                    "cumulative_loss_ha": round(cum_loss, 2),
                    "area_ha": round(baseline_ha[aoi] - cum_loss, 2),
                }
            )
    return rows


def points(px: pd.DataFrame) -> dict:
    """Columnar, rounded: ~1 m at 5 decimals, and month as an index."""
    return {
        "lon": px["lon"].round(5).tolist(),
        "lat": px["lat"].round(5).tolist(),
        "month": _month(px["last"]).tolist(),
        "conf": px["conf"].fillna(0).round(2).tolist(),  # unscored -> 0
        "n": px["n_events"].tolist(),
    }


def _write(obj, path: Path) -> str:
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(obj, separators=(",", ":"), allow_nan=False)
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path.name


def bake(source: str, out_dir: Path | str, radd_path: Path | str | None = None) -> dict:
    """`radd_path` overrides the RADD table at `<source>/radd/parquet`."""
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
        radd_file = Path(radd_path) if radd_path else _optional(source, RADD_PATH, tmp)
        radd = pd.read_parquet(radd_file) if radd_file else None

    polys = polys[polys["forest_type"].isin(AOIS)]
    if polys.crs is None or not polys.crs.is_projected:
        raise ValueError("polygons_geo.parquet must be in a projected CRS")
    polys = polys.assign(area_ha=polys.geometry.area / 1e4)
    # The mapping layers overlap: dissolve, as the pixel grid does.
    baseline = {
        t: g.geometry.union_all().area / 1e4 for t, g in polys.groupby("forest_type")
    }
    missing = [a for a in AOIS if a not in baseline]
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

    web = polys[["poly_id", "forest_type", "layer", "naturtype", "area_ha", "geometry"]]
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
    files["monthly"] = _write(monthly(px, baseline), out_dir / "monthly.json")
    radd_counts = {}
    if radd is not None:
        files["radd"] = {}
        for aoi in AOIS:
            sel = radd[radd["aoi"] == aoi]
            radd_counts[aoi] = int(len(sel))
            files["radd"][aoi] = _write(radd_points(sel), out_dir / f"radd_{aoi}.json")

    counts = px.groupby(["aoi", "kind"]).size()
    index = {
        "schema": SCHEMA,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "source": source if source.startswith(("http://", "https://")) else "local",
        "pixel_ha": PIXEL_HA,
        "forest_types": list(AOIS),
        "polygon_count": int(len(polys)),
        "polygon_counts": {
            t: int(n) for t, n in polys["forest_type"].value_counts().items()
        },
        "baseline_ha": {a: round(baseline[a], 2) for a in AOIS},
        "pixels": {a: {k: int(counts.get((a, k), 0)) for k in KINDS} for a in AOIS},
        "period_range": [_month(px["first"]).min(), _month(px["last"]).max()],
        "years": YEARS,
        "radd": radd_counts or None,
        "files": files,
    }
    _write(index, out_dir / "index.json")  # last: the app reads it first
    return index
