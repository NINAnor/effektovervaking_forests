"""Turning the alert table into the artefacts the public site and GIS users read.

* PMTiles (vector): what the web app reads; attributes travel with geometry.
* GeoJSON: the same features unbundled (dev, scripting, PMTiles fallback).
* GeoPackage: for QGIS/ArcGIS.
* COG: one band per variable on the native S2 10 m lattice, one per UTM zone.

The published feature is the PIXEL, not the alert: `first_loss_date` is onset,
`last_loss_date` is what is new, and `alert_id` is the most recent loss event
(the one a reviewer's verdict applies to). The AOI polygons are the forest
mask; no separate forest layer is rasterised.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

from . import alerts as alerts_mod
from . import confidence as conf_mod
from . import config, store

# Day 0 for the raster date bands (uint16 days since this date).
EPOCH = dt.date(2017, 1, 1)

# 0 means "no event" in every raster band and is also the nodata value.
NODATA = 0

PIXEL_M = 10.0

# Web payload (tiles and small context files) vs bulky GIS artefacts, which
# are kept off the site.
WEB_OUT = config.WEB_OUT
GIS_OUT = config.GIS_OUT
DEFAULT_OUT = WEB_OUT


def _gdal(tool: str) -> str:
    """Locate a GDAL CLI tool (`ogr2ogr` >= 3.8 for PMTiles).

    FORESTMON_GDAL_BIN names the directory; otherwise PATH.
    """
    bin_dir = os.environ.get("FORESTMON_GDAL_BIN")
    if bin_dir:
        return str(Path(bin_dir).expanduser() / tool)
    return shutil.which(tool) or tool


def _days(s: pd.Series) -> pd.Series:
    return (pd.to_datetime(s).dt.date.map(lambda d: (d - EPOCH).days)).astype("int32")


# ── per-pixel aggregation ────────────────────────────────────────────────────


def pixel_table(aoi_name: str, kind: str = "loss") -> pd.DataFrame:
    """Collapse the alert table to one row per pixel for one kind of change."""
    df = alerts_mod.publishable(aoi_name)
    if df.empty:
        return df
    df = df[df["kind"] == kind].copy()
    if df.empty:
        return df
    df["break_date"] = pd.to_datetime(df["break_date"])
    df = df.sort_values(["pix_id", "break_date"])

    last = df.groupby("pix_id").tail(1).set_index("pix_id")
    first = df.groupby("pix_id").head(1).set_index("pix_id")
    n = df.groupby("pix_id").size()

    out = pd.DataFrame(
        {
            "pix_id": last.index,
            "alert_id": last["alert_id"].to_numpy(),
            "poly_id": last["poly_id"].to_numpy(),
            "lon": last["lon"].to_numpy(),
            "lat": last["lat"].to_numpy(),
            "first_loss_date": first["break_date"].dt.strftime("%Y-%m-%d").to_numpy(),
            "last_loss_date": last["break_date"].dt.strftime("%Y-%m-%d").to_numpy(),
            "n_events": n.to_numpy(),
            "magnitude": last["magnitude"].to_numpy().round(4),
            "confidence": last["confidence"].to_numpy(),
            "conf_score": last["conf_score"].to_numpy(),
            "n_confirm": last["n_confirm"].to_numpy(),
            "season_flag": last["season_flag"].to_numpy(),
            "agree_opera": last["agree_opera"].to_numpy(),
            "status": last["status"].to_numpy(),
            "detected_at": pd.to_datetime(last["detected_at"])
            .dt.strftime("%Y-%m-%d")
            .to_numpy(),
        }
    ).reset_index(drop=True)
    out["conf_code"] = (
        out["confidence"].map(conf_mod.TIER_CODE).fillna(1).astype("int16")
    )
    out["year"] = pd.to_datetime(out["last_loss_date"]).dt.year.astype("int16")
    return out


# ── vector artefacts ─────────────────────────────────────────────────────────


def _to_wgs84(px: pd.DataFrame, geom: list, crs: np.ndarray):
    """Geometries built in each pixel's own UTM zone, reprojected to WGS84 per zone."""
    import geopandas as gpd

    px = px.reset_index(drop=True)
    if not len(px):
        return gpd.GeoDataFrame(px.copy(), geometry=geom, crs="EPSG:32632").to_crs(4326)
    geom = np.asarray(geom, dtype=object)
    parts = [
        gpd.GeoDataFrame(
            px[crs == c].copy(), geometry=list(geom[crs == c]), crs=c
        ).to_crs(4326)
        for c in pd.unique(crs)
    ]
    out = pd.concat(parts) if len(parts) > 1 else parts[0]
    return gpd.GeoDataFrame(out.sort_index(), geometry="geometry", crs=4326)


def _pixel_squares(px: pd.DataFrame, grid: pd.DataFrame):
    """Real 10 m pixel footprints, so a reviewer sees exactly which ground."""
    from shapely.geometry import box

    g = grid.set_index("pix_id")
    xs = g.loc[px["pix_id"], "utm_x"].to_numpy()
    ys = g.loc[px["pix_id"], "utm_y"].to_numpy()
    h = PIXEL_M / 2.0
    geom = [box(x - h, y - h, x + h, y + h) for x, y in zip(xs, ys)]
    return _to_wgs84(px, geom, g.loc[px["pix_id"], "utm_crs"].to_numpy())


def _pixel_points(px: pd.DataFrame, grid: pd.DataFrame):
    """Centres of those footprints, from the grid's UTM coordinates (not lon/lat)
    so they match the squares exactly."""
    from shapely.geometry import Point

    g = grid.set_index("pix_id")
    xs = g.loc[px["pix_id"], "utm_x"].to_numpy()
    ys = g.loc[px["pix_id"], "utm_y"].to_numpy()
    geom = [Point(x, y) for x, y in zip(xs, ys)]
    return _to_wgs84(px, geom, g.loc[px["pix_id"], "utm_crs"].to_numpy())


def write_vector(
    gdf, out_dir: Path, stem: str, layer: str, gis_dir: Path | None = None, points=None
) -> dict:
    """GeoJSON first, then PMTiles and GPKG derived from it by GDAL.

    SQLite-backed outputs are built on local disk and copied, because SQLite
    cannot lock files on a CIFS share.
    """
    import shutil
    import tempfile

    out_dir.mkdir(parents=True, exist_ok=True)
    gis_dir = Path(gis_dir) if gis_dir is not None else out_dir
    gis_dir.mkdir(parents=True, exist_ok=True)

    gj = gis_dir / f"{stem}.geojson"
    gdf.to_file(gj, driver="GeoJSON")
    made = {"geojson": str(gj), "n_features": int(len(gdf))}

    with tempfile.TemporaryDirectory(prefix="forestmon_publish_") as tmp:
        tmp = Path(tmp)
        local_gj = tmp / f"{stem}.geojson"
        shutil.copyfile(gj, local_gj)

        # Footprints only at z12+: MVT quantisation (EXTENT units per tile) makes
        # a 10 m square bloat and shift at lower zooms, and raising EXTENT does
        # not fix it. MAX_SIZE/MAX_FEATURES=0 only stop feature dropping.
        local_pm = tmp / f"{stem}.pmtiles"
        r = subprocess.run(
            [
                _gdal("ogr2ogr"),
                "-f",
                "PMTiles",
                str(local_pm),
                str(local_gj),
                "-dsco",
                "MINZOOM=12",
                "-dsco",
                "MAXZOOM=16",
                "-dsco",
                "MAX_SIZE=0",
                "-dsco",
                "MAX_FEATURES=0",
                "-dsco",
                f"NAME={layer}",
                "-nln",
                layer,
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0 and local_pm.exists():
            dest_pm = out_dir / f"{stem}.pmtiles"
            if dest_pm.exists():
                dest_pm.unlink()
            shutil.copyfile(local_pm, dest_pm)
            made["pmtiles"] = f"{stem}.pmtiles"
            made["pmtiles_mb"] = round(local_pm.stat().st_size / 1e6, 2)
        else:
            made["pmtiles_error"] = (r.stderr or "no output")[-400:]

        # Centres below z12 (z5-11): a point's quantisation error is invisible,
        # and the overview claims only a location, not an area.
        if points is not None and len(points):
            local_pts_gj = tmp / f"{stem}_pts.geojson"
            points.to_file(local_pts_gj, driver="GeoJSON")
            local_pts = tmp / f"{stem}_pts.pmtiles"
            rp = subprocess.run(
                [
                    _gdal("ogr2ogr"),
                    "-f",
                    "PMTiles",
                    str(local_pts),
                    str(local_pts_gj),
                    "-dsco",
                    "MINZOOM=5",
                    "-dsco",
                    "MAXZOOM=11",
                    "-dsco",
                    "MAX_SIZE=0",
                    "-dsco",
                    "MAX_FEATURES=0",
                    "-dsco",
                    f"NAME={layer}",
                    "-nln",
                    layer,
                ],
                capture_output=True,
                text=True,
            )
            if rp.returncode == 0 and local_pts.exists():
                dest_pts = out_dir / f"{stem}_pts.pmtiles"
                if dest_pts.exists():
                    dest_pts.unlink()
                shutil.copyfile(local_pts, dest_pts)
                made["pmtiles_points"] = f"{stem}_pts.pmtiles"
                made["pmtiles_points_mb"] = round(local_pts.stat().st_size / 1e6, 2)
            else:
                made["pmtiles_points_error"] = (rp.stderr or "no output")[-400:]

        local_gpkg = tmp / f"{stem}.gpkg"
        r = subprocess.run(
            [
                _gdal("ogr2ogr"),
                "-f",
                "GPKG",
                str(local_gpkg),
                str(local_gj),
                "-nln",
                layer,
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0 and local_gpkg.exists():
            dest = gis_dir / f"{stem}.gpkg"
            if dest.exists():
                dest.unlink()
            shutil.copyfile(local_gpkg, dest)
            made["gpkg"] = str(dest)
        else:
            made["gpkg_error"] = (r.stderr or "no output")[-400:]

    return made


# ── raster artefact ──────────────────────────────────────────────────────────


def write_cog(px: pd.DataFrame, grid: pd.DataFrame, out_path: Path) -> dict:
    """Four-band sparse COG on the native S2 lattice, for ONE UTM zone.

    Bands: 1 last_loss_date, 2 first_loss_date, 3 confidence (1/2/3),
    4 aoi_mask (1 inside a monitored polygon).

    `grid` must hold a single `utm_crs`; `publish` calls this once per zone.
    The array is >99.9% empty, so only the 512 px blocks holding a pixel are
    written (sparse GTiff, then COG with SPARSE_OK); it is never materialised.
    """
    import rasterio
    import rasterio.shutil
    from rasterio.transform import from_origin
    from rasterio.windows import Window

    crss = grid["utm_crs"].unique()
    if len(crss) != 1:
        raise ValueError(f"write_cog needs one CRS, got {list(crss)}")
    crs = str(crss[0])

    gx, gy = grid["utm_x"].to_numpy(), grid["utm_y"].to_numpy()
    x0, y1 = gx.min() - PIXEL_M / 2, gy.max() + PIXEL_M / 2
    width = int(round((gx.max() + PIXEL_M / 2 - x0) / PIXEL_M))
    height = int(round((y1 - (gy.min() - PIXEL_M / 2)) / PIXEL_M))
    transform = from_origin(x0, y1, PIXEL_M, PIXEL_M)
    blk = 512

    def rc(xs, ys):
        return (
            np.round((y1 - ys) / PIXEL_M - 0.5).astype(int),
            np.round((xs - x0) / PIXEL_M - 0.5).astype(int),
        )

    # Value table over grid pixels: mask=1 everywhere, alert bands where lossy.
    r, c = rc(gx, gy)
    vals = np.zeros((4, len(grid)), dtype="uint16")
    vals[3] = 1
    if len(px):
        pos = pd.Series(np.arange(len(grid)), index=grid["pix_id"].to_numpy())
        i = pos.reindex(px["pix_id"].to_numpy()).to_numpy()
        keep = ~np.isnan(i)  # alerts of other zones are not ours
        i = i[keep].astype(int)
        vals[0, i] = _days(px["last_loss_date"]).to_numpy()[keep]
        vals[1, i] = _days(px["first_loss_date"]).to_numpy()[keep]
        vals[2, i] = np.asarray(px["conf_code"].to_numpy()[keep], dtype="uint16")

    band_names = [
        "last_loss_date_days_since_2017",
        "first_loss_date_days_since_2017",
        "confidence_1low_2med_3high",
        "aoi_mask",
    ]

    staging = out_path.with_suffix(".staging.tif")
    stage_profile = dict(
        driver="GTiff",
        dtype="uint16",
        count=4,
        width=width,
        height=height,
        crs=crs,
        transform=transform,
        nodata=NODATA,
        tiled=True,
        blockxsize=blk,
        blockysize=blk,
        SPARSE_OK="TRUE",
        BIGTIFF="YES",
        interleave="band",
    )
    with rasterio.open(staging, "w", **stage_profile) as dst:
        dst.descriptions = tuple(band_names)
        dst.update_tags(EPOCH=EPOCH.isoformat(), NODATA_MEANS="no loss event")
        br, bc = r // blk, c // blk
        order = np.lexsort((bc, br))
        keys = np.c_[br[order], bc[order]]
        starts = np.flatnonzero(np.r_[True, (np.diff(keys, axis=0) != 0).any(axis=1)])
        for s0, s1 in zip(starts, np.r_[starts[1:], len(order)]):
            sel = order[s0:s1]
            row_off, col_off = int(br[sel[0]]) * blk, int(bc[sel[0]]) * blk
            win = Window(
                col_off, row_off, min(blk, width - col_off), min(blk, height - row_off)
            )
            a = np.zeros((4, int(win.height), int(win.width)), dtype="uint16")
            a[:, r[sel] - row_off, c[sel] - col_off] = vals[:, sel]
            dst.write(a, window=win)
    try:
        rasterio.shutil.copy(
            staging,
            out_path,
            driver="COG",
            COMPRESS="DEFLATE",
            PREDICTOR="2",
            BLOCKSIZE=str(blk),
            SPARSE_OK="YES",
            OVERVIEW_RESAMPLING="NEAREST",
            BIGTIFF="IF_SAFER",
        )
    finally:
        staging.unlink(missing_ok=True)

    return {
        "cog": out_path.name,
        "width": width,
        "height": height,
        "crs": crs,
        "bands": band_names,
        "size_mb": round(out_path.stat().st_size / 1e6, 2),
    }


# ── queue index ─────────────────────────────────────────────────────────────

QUEUE_FIELDS = [
    "alert_id",
    "pix_id",
    "lon",
    "lat",
    "first_loss_date",
    "last_loss_date",
    "n_events",
    "magnitude",
    "confidence",
    "conf_score",
    "n_confirm",
    "season_flag",
    "agree_opera",
    "detected_at",
    "year",
]


def write_queue_index(px: pd.DataFrame, out_dir: Path, aoi_name: str, kind: str) -> str:
    """Attributes only, no geometry: what the review queue reads.

    Fields are those the queue sorts by and the evidence panel shows, so queue
    selection and map clicks agree.
    """
    cols = [c for c in QUEUE_FIELDS if c in px.columns]
    recs = px[cols].copy()
    recs["agree_opera"] = recs["agree_opera"].astype(bool)
    for c in ("magnitude", "conf_score"):
        if c in recs:
            recs[c] = recs[c].astype(float).round(4)
    for c in ("n_confirm", "n_events", "year"):
        if c in recs:
            recs[c] = recs[c].fillna(0).astype(int)
    name = f"queue_{aoi_name}_{kind}.json"
    # Compact separators: machine-read only.
    (out_dir / name).write_text(
        json.dumps(recs.to_dict(orient="records"), separators=(",", ":")),
        encoding="utf-8",
    )
    return name


# ── AOI outline ──────────────────────────────────────────────────────────────


def write_aoi(aoi_name: str, out_dir: Path) -> dict:
    import geopandas as gpd

    src = config.aoi_path(aoi_name)
    if not Path(src).exists():
        return {}
    gdf = gpd.read_parquet(src).to_crs(4326)
    keep = [c for c in ("poly_id", "navn", "naturtype", "tilstand") if c in gdf.columns]
    gdf = gdf[keep + ["geometry"]]
    out = out_dir / f"aoi_{aoi_name}.geojson"
    gdf.to_file(out, driver="GeoJSON")
    return {"aoi_geojson": out.name, "n_polygons": int(len(gdf))}


# ── the entry point ──────────────────────────────────────────────────────────


def publish(
    aoi_name: str,
    out_dir: Path | str = WEB_OUT,
    gis_dir: Path | str = GIS_OUT,
    include_gain: bool = True,
    write_raster: bool = True,
) -> dict:
    out_dir, gis_dir = Path(out_dir), Path(gis_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    gis_dir.mkdir(parents=True, exist_ok=True)
    grid = pd.read_parquet(config.GRID_DIR / f"{aoi_name}_grid.parquet")

    result: dict = {"aoi": aoi_name, "outputs": {}}

    loss = pixel_table(aoi_name, "loss")
    if len(loss):
        gdf = _pixel_squares(loss, grid)
        result["outputs"]["loss"] = write_vector(
            gdf,
            out_dir,
            f"alerts_{aoi_name}_loss",
            "loss",
            gis_dir=gis_dir,
            points=_pixel_points(loss, grid),
        )
        result["outputs"]["loss"]["queue"] = write_queue_index(
            loss, out_dir, aoi_name, "loss"
        )
        result["n_loss_pixels"] = int(len(loss))
        result["loss_by_confidence"] = loss["confidence"].value_counts().to_dict()
        result["loss_date_range"] = [
            str(loss["last_loss_date"].min()),
            str(loss["last_loss_date"].max()),
        ]
    else:
        result["n_loss_pixels"] = 0

    if include_gain:
        gain = pixel_table(aoi_name, "gain")
        if len(gain):
            # Published so the "gain" verdict has something to attach to.
            gdf = _pixel_squares(gain, grid)
            result["outputs"]["gain"] = write_vector(
                gdf,
                out_dir,
                f"alerts_{aoi_name}_gain",
                "gain",
                gis_dir=gis_dir,
                points=_pixel_points(gain, grid),
            )
            # No queue index for gain: the queue labels the LOSS detector; gain
            # stays clickable on the map.
            result["n_gain_pixels"] = int(len(gain))

    if write_raster:
        # Build locally and copy: the COG driver seeks backwards, which fails on
        # CIFS (_tiffSeekProc I/O errors).
        import shutil
        import tempfile

        # One COG per UTM zone; a single-zone AOI keeps the plain name.
        zones = sorted(grid["utm_crs"].unique())
        infos = []
        with tempfile.TemporaryDirectory(prefix="forestmon_cog_") as tmp:
            for crs in zones:
                name = (
                    f"alerts_{aoi_name}.tif"
                    if len(zones) == 1
                    else f"alerts_{aoi_name}_{crs.split(':')[1]}.tif"
                )
                local = Path(tmp) / name
                infos.append(write_cog(loss, grid[grid["utm_crs"] == crs], local))
                dest = gis_dir / name
                if dest.exists():
                    dest.unlink()
                shutil.copyfile(local, dest)
        result["outputs"]["raster"] = infos[0] if len(infos) == 1 else infos

    result["outputs"].update(write_aoi(aoi_name, out_dir))
    result["n_grid_pixels"] = int(len(grid))
    result["bbox"] = [
        float(grid["lon"].min()),
        float(grid["lat"].min()),
        float(grid["lon"].max()),
        float(grid["lat"].max()),
    ]
    return result


def write_alert_ids(results: list[dict], out_dir: Path | str = WEB_OUT) -> Path:
    """The flat list of published alert ids, read by the feedback endpoint.

    The endpoint rejects unknown ids; generating the list with the tiles keeps
    it in step with what the site shows.
    """
    out_dir = Path(out_dir)
    ids: list[str] = []
    for r in results:
        aoi = r["aoi"]
        for kind in ("loss", "gain"):
            px = pixel_table(aoi, kind)
            if len(px):
                ids.extend(px["alert_id"].tolist())
    p = out_dir / "alert_ids.json"
    p.write_text(json.dumps(sorted(set(ids)), separators=(",", ":")), encoding="utf-8")
    return p


def write_manifest(results: list[dict], out_dir: Path | str = DEFAULT_OUT) -> Path:
    """What the app reads first: freshness, counts, and which files exist."""
    out_dir = Path(out_dir)
    now = dt.datetime.now(dt.UTC)
    # AOIs are published one at a time: keep manifest entries of AOIs not in
    # this run (their files are still on disk).
    results = list(results)
    done = {r["aoi"] for r in results}
    prev = out_dir / "manifest.json"
    if prev.exists():
        try:
            old = json.loads(prev.read_text(encoding="utf-8")).get("aois", [])
        except (ValueError, OSError):
            old = []
        results += [
            r
            for r in old
            if r.get("aoi") not in done and r.get("aoi") in config.AOI_PARQUET
        ]
    order = {a: i for i, a in enumerate(config.AOI_PARQUET)}
    results.sort(key=lambda r: order.get(r["aoi"], len(order)))
    manifest = {
        "generated_at": now.isoformat(),
        "data_version": now.strftime("%Y%m%dT%H%M%SZ"),
        "recipe_version": config.RECIPE_VERSION,
        "loss_gate": alerts_mod.LOSS_GATE,
        "epoch": EPOCH.isoformat(),
        "confidence": {
            "tiers": list(conf_mod.TIERS),
            "n_confirm_full": conf_mod.N_CONFIRM_FULL,
            "mag_ratio_full": conf_mod.MAG_RATIO_FULL,
            "season_cap": conf_mod.SEASON_CAP,
        },
        "aois": results,
    }
    p = out_dir / "manifest.json"
    store.write_json(manifest, p)
    write_alert_ids(results, out_dir)
    return p
