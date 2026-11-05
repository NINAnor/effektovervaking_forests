"""AOI polygons -> the fixed pixel grid that carries S-CCD state.

Everything downstream is keyed on `pix_id`, so the grid must never move:

1. The grid is the Sentinel-2 10 m lattice (coordinates snapped to multiples of
   10 m), so imagery is never resampled or reprojected.
2. One polygon, one CRS: each polygon is gridded in its own UTM zone and scenes
   are read only for pixels in their CRS. `pix_id` embeds the EPSG code.

Pixel centres are stored in UTM (reading imagery) and lon/lat (WGS84 joins).
"""

from __future__ import annotations

import hashlib

import geopandas as gpd
import numpy as np
import pandas as pd
from rasterio import features
from rasterio import transform as rtransform

from . import config


def mgrs_utm_zone(lon: float, lat: float) -> int:
    """UTM zone as the MGRS / Sentinel-2 tiling assigns it.

    Differs from the plain formula: MGRS widens zone 32V to 3-12 E between
    56 and 64 N (Svalbard exceptions are out of range).
    """
    if not 55 <= lat <= 72:  # sanity: this is a Norway tool
        raise ValueError(f"latitude {lat:.2f}N is outside the expected range")
    if 56 <= lat < 64 and 3 <= lon < 12:
        return 32
    return int((lon + 180) // 6) + 1


def polygon_crs(gdf: gpd.GeoDataFrame, tile_bounds: dict | None = None) -> pd.Series:
    """The UTM CRS each polygon is gridded in (per polygon, never split).

    Defaults to the MGRS zone of the representative point. With `tile_bounds`
    ({tile: (utm bounds, crs)}), a polygon keeps that zone only if some tile in
    it contains the polygon; otherwise it takes the zone of the tile where it
    sits most interior (some stands are imaged only by a neighbour zone's tile).
    """
    ll = gdf.to_crs(4326)
    rp = ll.geometry.representative_point()
    zones = [f"EPSG:{32600 + mgrs_utm_zone(p.x, p.y)}" for p in rp]
    if not tile_bounds:
        return pd.Series(zones, index=gdf.index)

    out = []
    for i, mgrs in enumerate(zones):
        best, best_margin = None, -np.inf
        for (left, bottom, right, top), crs in tile_bounds.values():
            minx, miny, maxx, maxy = gdf.iloc[[i]].to_crs(crs).total_bounds
            margin = min(minx - left, right - maxx, miny - bottom, top - maxy)
            if margin < 0:
                continue  # not wholly inside this tile
            if crs == mgrs:
                best = mgrs
                break
            if margin > best_margin:
                best, best_margin = crs, margin
        out.append(best or mgrs)  # no containing tile: assign-tiles reports it
    return pd.Series(out, index=gdf.index)


def _snap(v: float, res: int, up: bool = False) -> float:
    """Snap a UTM coordinate onto the S2 pixel-edge lattice."""
    return (np.ceil(v / res) if up else np.floor(v / res)) * res


def _grid_zone(gdf: gpd.GeoDataFrame, idx: np.ndarray, crs: str) -> pd.DataFrame:
    """Pixel centres of polygons `idx` (positions in `gdf`), gridded in `crs`."""
    g = gdf.iloc[idx].to_crs(crs)
    res = config.GRID_RES

    minx, miny, maxx, maxy = g.total_bounds
    minx, miny = _snap(minx, res), _snap(miny, res)
    maxx, maxy = _snap(maxx, res, up=True), _snap(maxy, res, up=True)
    width = int(round((maxx - minx) / res))
    height = int(round((maxy - miny) / res))
    tf = rtransform.from_origin(minx, maxy, res, res)

    # Burn AOI-wide polygon index+1 so 0 stays "outside".
    shapes = ((geom, int(i) + 1) for i, geom in zip(idx, g.geometry))
    burned = features.rasterize(
        shapes,
        out_shape=(height, width),
        transform=tf,
        fill=0,
        all_touched=False,
        dtype="int32",
    )

    rows, cols = np.nonzero(burned)

    # Pixel CENTRES, in UTM.
    x = minx + (cols + 0.5) * res
    y = maxy - (rows + 0.5) * res

    df = pd.DataFrame(
        {
            "poly_idx": burned[rows, cols] - 1,
            "utm_x": x.astype("float64"),
            "utm_y": y.astype("float64"),
        }
    )
    # pix_id from the snapped integer coordinate: independent of the AOI bbox.
    gx = np.floor(df["utm_x"] / res).astype("int64")
    gy = np.floor(df["utm_y"] / res).astype("int64")
    df["pix_id"] = [f"{crs.split(':')[1]}_{a}_{b}" for a, b in zip(gx, gy)]

    pts = gpd.GeoSeries(gpd.points_from_xy(df.utm_x, df.utm_y), crs=crs).to_crs(4326)
    df["lon"] = pts.x.values
    df["lat"] = pts.y.values
    df["aoi"] = ""  # filled by build_grid; keeps column order
    df["utm_crs"] = crs
    return df


def build_grid(aoi_name: str, tile_bounds: dict | None = None) -> pd.DataFrame:
    """Rasterise the AOI onto the S2 10 m lattice; one row per pixel centre.

    A pixel belongs to the AOI when its centre falls inside a polygon.
    """
    path = config.aoi_path(aoi_name)
    gdf = gpd.read_parquet(path).reset_index(drop=True)
    poly_crs = polygon_crs(gdf, tile_bounds)
    # One sub-grid per zone, each on its own S2 lattice.
    df = pd.concat(
        [
            _grid_zone(gdf, np.flatnonzero(poly_crs.to_numpy() == crs), crs)
            for crs in sorted(poly_crs.unique())
        ],
        ignore_index=True,
    )
    df["aoi"] = aoi_name
    if df.empty:
        raise ValueError(f"{aoi_name}: no pixel centres fell inside any polygon")

    # Carry the source polygon identifier for per-polygon reporting.
    for cand in ("poly_id", "id", "OBJECTID", "lokalitetid"):
        if cand in gdf.columns:
            df["poly_id"] = gdf[cand].to_numpy()[df.poly_idx.to_numpy()]
            break
    else:
        df["poly_id"] = df.poly_idx

    return df


def grid_path(aoi_name: str):
    return config.GRID_DIR / f"{aoi_name}_grid.parquet"


def save_grid(aoi_name: str, df: pd.DataFrame):
    df.to_parquet(grid_path(aoi_name), index=False)


def load_grid(aoi_name: str) -> pd.DataFrame:
    p = grid_path(aoi_name)
    if not p.exists():
        raise FileNotFoundError(
            f"no grid for {aoi_name}; run `grid build {aoi_name}` first"
        )
    return pd.read_parquet(p)


def aoi_bbox(aoi_name: str) -> list[float]:
    """WGS84 bbox of the AOI, for STAC search."""
    g = gpd.read_parquet(config.aoi_path(aoi_name)).to_crs(4326)
    return [float(v) for v in g.total_bounds]


def grid_fingerprint(df: pd.DataFrame) -> str:
    """Short hash of the pixel set, stamped into state so a silently changed
    grid cannot be paired with state fitted on the old one."""
    h = hashlib.sha256()
    for pid in sorted(df.pix_id):
        h.update(pid.encode())
    return h.hexdigest()[:12]


# ── tile assignment ──────────────────────────────────────────────────────────
# Overlapping MGRS tiles disagree radiometrically (Sen2Cor runs per tile), and
# the bias is tile-dependent, so a pixel fed by different tiles over time sees
# a spurious step. Each pixel is pinned to one home tile: the one in which it
# sits furthest from the tile edge. No observations are lost.


def assign_home_tiles(aoi_name: str, tile_bounds: dict[str, tuple]) -> pd.DataFrame:
    """Add a `home_tile` column to the grid. `tile_bounds` maps tile -> bounds."""
    df = load_grid(aoi_name)
    pix_crs = df["utm_crs"].to_numpy()
    x = df["utm_x"].to_numpy()
    y = df["utm_y"].to_numpy()

    best_name = np.full(len(df), "", dtype=object)
    best_margin = np.full(len(df), -np.inf)

    for tile, (bounds, tile_crs) in tile_bounds.items():
        # Only pixels gridded in the tile's own CRS (tiles overhang into zones).
        same = pix_crs == tile_crs
        if not same.any():
            continue
        left, bottom, right, top = bounds
        inside = same & (x >= left) & (x < right) & (y > bottom) & (y <= top)
        if not inside.any():
            continue
        # Distance to the nearest tile edge; bigger is more interior.
        margin = np.minimum.reduce([x - left, right - x, y - bottom, top - y])
        take = inside & (margin > best_margin)
        best_margin = np.where(take, margin, best_margin)
        best_name = np.where(take, tile, best_name)

    df["home_tile"] = best_name
    df["tile_margin_m"] = np.where(np.isfinite(best_margin), best_margin, np.nan)
    save_grid(aoi_name, df)
    return df
