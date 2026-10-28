"""Turning the alert table into the artefacts the public site and GIS users read.

WHAT THIS PUBLISHES, AND WHY EACH FORM EXISTS
---------------------------------------------
* **PMTiles (vector)** -- what the web app reads. Attributes travel with the
  geometry, so the browser filters by date, confidence and status and restyles
  without a server round trip and without re-tiling. A click yields `alert_id`
  directly, which is what the feedback path needs.
* **GeoJSON** -- the same features, unbundled. Used by `npm run dev`, by anyone
  scripting against the product, and as the fallback if PMTiles range requests
  are ever blocked by a host.
* **GeoPackage** -- for QGIS and ArcGIS. Full attributes, one layer per kind.
* **COG** -- one band per published variable, on the native Sentinel-2 10 m
  lattice in the AOI's own UTM zone. No warping: `aoi.py` pinned the grid to the
  real S2 lattice precisely so this artefact can be exact rather than resampled.

ONE FEATURE PER PIXEL, NOT PER ALERT
------------------------------------
342 pixels carry more than one loss break. A map that draws them twice is a map
that lies about how much has happened, so the published feature is the PIXEL,
carrying `first_loss_date` (onset -- what management asks for) and
`last_loss_date` (what is new -- what a monitoring product answers), with
`n_events` recording that the pixel has a history.

The `alert_id` on a published feature is the MOST RECENT loss event, because
that is the one the map is colouring and therefore the one a reviewer is looking
at. A reviewer's verdict is a statement about that event, not about the pixel's
whole history.

THE FOREST MASK IS THE AOI
--------------------------
No separate forest baseline is rasterised here, and that is deliberate rather
than missing. The AOI polygons ARE the forest mask for this project: they are
mapped kalklindeskog and olivinskog naturtype polygons, and the system only ever
looks inside them. Publishing a second, coarser forest layer derived from RADD
would imply a distinction the pipeline does not make. RADD's tcd50 baseline stays
available in the app as an external cross-reference layer, which is the honest
role for it.
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

# Day 0 for the raster date bands. Sentinel-2 L2A over these AOIs starts in 2017,
# so no published date can be negative and uint16 covers ~179 years.
EPOCH = dt.date(2017, 1, 1)

# 0 means "no event" in every raster band, which is also the nodata value -- the
# requirement that a pixel with no loss shows nothing, expressed in the format.
NODATA = 0

PIXEL_M = 10.0

# Two destinations, because they have different jobs and very different size
# budgets. The web payload is what every visitor downloads, so only the tiled
# form and the small context files go there. The GIS artefacts are bulky,
# redundant with the tiles, and wanted by a handful of people who will fetch
# them deliberately -- putting them on the site would multiply the page weight
# roughly twentyfold to serve nobody.
WEB_OUT = config.WEB_OUT
GIS_OUT = config.GIS_OUT
DEFAULT_OUT = WEB_OUT


def _gdal(tool: str) -> str:
    """Locate a GDAL command-line tool.

    The rasterio wheel bundles the GDAL library but not its CLI, so `ogr2ogr`
    (GDAL >= 3.8, for the PMTiles driver) comes from the system or a conda/pixi
    env. FORESTMON_GDAL_BIN names the directory explicitly; otherwise PATH.
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
    """Geometries built in each pixel's own UTM zone, reprojected to WGS84.

    A multi-zone AOI (olivinskog: 32-35) has coordinates on several lattices, so
    the reprojection is done per zone; tagging them all with the first pixel's
    CRS would put three of the zones hundreds of km from where they are.
    """
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
    """Real 10 m footprints, not points.

    A reviewer giving a verdict has to see exactly which pixel is being claimed
    against the orthophoto. A circle floating near a pixel invites a verdict
    about the wrong ground.
    """
    from shapely.geometry import box

    g = grid.set_index("pix_id")
    xs = g.loc[px["pix_id"], "utm_x"].to_numpy()
    ys = g.loc[px["pix_id"], "utm_y"].to_numpy()
    h = PIXEL_M / 2.0
    geom = [box(x - h, y - h, x + h, y + h) for x, y in zip(xs, ys)]
    return _to_wgs84(px, geom, g.loc[px["pix_id"], "utm_crs"].to_numpy())


def _pixel_points(px: pd.DataFrame, grid: pd.DataFrame):
    """The centres of those same footprints.

    Derived from the grid's UTM coordinates rather than the alert table's
    lon/lat so that a point is guaranteed to be the exact centre of the square
    published for the same pixel. Two independently-derived positions for one
    pixel is a discrepancy waiting to be found by a reviewer at high zoom.
    """
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

    Both PMTiles and GPKG are SQLite underneath -- GDAL's PMTiles writer stages
    into a temporary mbtiles -- and this repository lives on a CIFS share where
    SQLite cannot take the locks it needs. (The 0-byte `test_write.gpkg` and the
    stray `opera.pmtiles.tmp.mbtiles` in the repo root are both scars from this.)
    So anything SQLite-backed is built on local disk and copied to its final
    home, which is also faster than writing tiles over SMB.
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

        # SQUARES ONLY WHERE A SQUARE IS HONEST -- z12 and up.
        #
        # MVT quantises geometry to EXTENT (4096) units per tile at every zoom,
        # and below z12 a 10 m pixel is smaller than one unit. Measured against
        # this very output on 2026-08-21: a footprint that is 19.3 m at z16 comes
        # back 38.2 m at z8 and 305.7 m at z5, snapped to the quantisation grid,
        # so pixels both bloat and MOVE -- by up to half a cell, ~153 m at z5 --
        # as the reviewer zooms. That is a map telling a different story about
        # where the loss is at every scale, which is worse than showing nothing.
        #
        # MAX_SIZE/MAX_FEATURES=0 already stop feature DROPPING; they do nothing
        # about quantisation, and conflating the two is what let this survive.
        # Raising EXTENT does not rescue it either: at its 65536 maximum z5 still
        # quantises to 19 m. The only correct fix is to stop drawing footprints
        # at scales that cannot represent them.
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

        # ... and CENTRES below it, drawn as fixed-radius circles. A point
        # quantises too, but to a position error of at most half a cell, which
        # is 0.03 screen px at z5 -- invisible, where the same error applied to
        # a footprint is the bug above. The overview stops claiming an area it
        # cannot resolve and claims only a location, which is all it knows.
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

    `grid` must hold a single `utm_crs`; `publish` calls this once per zone,
    because one raster cannot hold two UTM lattices without warping.

    The AOI spans hundreds of km but holds only tens of thousands of pixels in a
    few hundred small clusters, so the array is >99.9% empty. It is therefore
    never materialised: only the 512 px blocks that hold a pixel are written,
    into a sparse tiled GTiff, which GDAL then copies to COG with SPARSE_OK so
    empty blocks stay absent. (Allocating the full extent cost 4.8 GB for
    kalklindeskog and would be ~17 GB for olivinskog's zone 32, 58-69 N.)
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

    # One value table over the grid pixels: mask=1 everywhere, alert bands where
    # the pixel has a loss event.
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
    """Attributes only, no geometry -- what the review queue reads.

    The tiles are indexed by space; a queue is ordered by date, confidence or
    magnitude and has to see every candidate at once, so it cannot be built from
    them. Shipping the published GeoJSON instead would work but costs ~3.5 MB of
    geometry to answer a question that needs none.

    The fields are exactly those the queue sorts by and the evidence panel shows,
    so selecting from the queue and clicking the map put identical objects into
    the same state -- which is what stops the two halves of the app disagreeing
    about an alert.
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
    # Separators without spaces: this file is machine-read only, and at a few
    # thousand rows the whitespace is a measurable share of the download.
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
            # Published because the review vocabulary lets a reviewer answer
            # "gain"; a verdict option with nothing on screen to attach it to is
            # a broken form. Default off in the app.
            gdf = _pixel_squares(gain, grid)
            result["outputs"]["gain"] = write_vector(
                gdf,
                out_dir,
                f"alerts_{aoi_name}_gain",
                "gain",
                gis_dir=gis_dir,
                points=_pixel_points(gain, grid),
            )
            # No queue index for gain. The review queue exists to produce an
            # unbiased label set for the LOSS detector; gain is context, and a
            # 22k-row worklist nobody is asked to work through would cost 8 MB
            # of everyone's bandwidth to sit unused. Gain pixels stay clickable
            # on the map, which is all the "gain" verdict option needs.
            result["n_gain_pixels"] = int(len(gain))

    if write_raster:
        # Same CIFS problem as the SQLite artefacts, different symptom: the COG
        # driver seeks backwards over its staging file to lay down the header
        # and overviews, and this share answers that with _tiffSeekProc I/O
        # errors. Build local, copy once.
        import shutil
        import tempfile

        # One COG per UTM zone. A single-zone AOI keeps its plain name, so
        # kalklindeskog's published file does not move.
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

    This is the cheapest and most effective control on an open write endpoint: a
    drive-by POST does not know any real alert id, so requiring one rejects
    almost all junk without asking a single reviewer to log in.

    It is generated here, from the same run that produces the tiles, so the
    endpoint validates against exactly what the site is currently showing rather
    than against a list that quietly went stale.
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
    """What the app reads first: freshness, counts, and which files exist.

    A public monitoring map whose age is unknowable is worse than no map, so the
    run date is a first-class part of the product rather than a footnote.
    """
    out_dir = Path(out_dir)
    now = dt.datetime.now(dt.UTC)
    # The cycle publishes one AOI at a time, so a manifest built from `results`
    # alone would drop every other AOI the moment the next one is published.
    # Keep the entries of AOIs not in this run (a stale entry is honest -- its
    # files are still on disk -- whereas a missing one takes the AOI off the map).
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
