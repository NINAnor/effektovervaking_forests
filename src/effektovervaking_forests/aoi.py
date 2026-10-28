"""AOI polygons -> the fixed pixel grid that carries S-CCD state.

Every downstream artefact (observations, state, alerts) is keyed on `pix_id`
from this grid, so the grid must be reproducible and must never move. Two rules
make that true:

1. The grid is the SENTINEL-2 GRID, not a grid of our own. S2 L2A tiles are
   laid out on UTM eastings/northings that are exact multiples of 10 m, so
   snapping to `floor(coord / 10) * 10` lands on real pixel edges. We therefore
   never resample or reproject imagery — we read the pixel that already exists.
2. One POLYGON, one CRS. Each polygon is gridded in the UTM zone Sentinel-2
   tiles it in, so an AOI may carry several zone sub-grids side by side
   (kalklindeskog: zone 32 only; olivinskog: 32, 33, 34, 35). Scenes are read
   only for pixels in their own CRS, never warped onto another zone's grid,
   since warping is exactly the kind of silent resampling rule 1 exists to
   avoid. `pix_id` embeds the EPSG code, so ids stay unique across zones.

Pixel centres are stored in both UTM and lon/lat: UTM for reading imagery,
lon/lat for joining to the label/inspector work, which is all WGS84.
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
    """UTM zone as the MGRS / Sentinel-2 tiling grid assigns it.

    NOT plain `floor((lon + 180) / 6) + 1`: MGRS widens zone 32V to 3-12 E
    between 56 and 64 N, and Sentinel-2 follows it. Western Norway at ~5 E is
    therefore tiled as 32VKL/32VKN — there are no 31V tiles over it, so gridding
    those stands in zone 31 would leave them with no scene in their CRS at all.
    (The Svalbard exceptions, 72-84 N, are outside this tool's range.)
    """
    if not 55 <= lat <= 72:  # sanity: this is a Norway tool
        raise ValueError(f"latitude {lat:.2f}N is outside the expected range")
    if 56 <= lat < 64 and 3 <= lon < 12:
        return 32
    return int((lon + 180) // 6) + 1


def polygon_crs(gdf: gpd.GeoDataFrame, tile_bounds: dict | None = None) -> pd.Series:
    """The UTM CRS each polygon is gridded in.

    Per polygon rather than per pixel so a stand is never split across two
    lattices. The default is the MGRS zone of its representative point, but MGRS
    is not the whole story: north of 64 N, Sentinel-2 has no zone-33 tile at
    12.6-12.9 E — two olivinskog stands (24.6k px) there are imaged ONLY by the
    overhang of 32WPS/32WPT. So when `tile_bounds` ({tile: (utm bounds, crs)},
    as `grid assign-tiles` reads them) is given, a polygon keeps its MGRS zone
    only if some tile in that zone contains it, and otherwise takes the zone of
    the tile in which it sits most interior.
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

    # Burn polygon index+1 so 0 stays "outside"; keeps the pixel -> polygon link
    # that per-polygon reporting will need. The index is the AOI-wide position,
    # not the position within this zone.
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
    # pix_id from the snapped integer grid coordinate: stable across rebuilds,
    # independent of the AOI bounding box, and directly comparable between AOIs
    # in the same zone.
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

    A pixel belongs to the AOI when its CENTRE falls inside a polygon
    (`all_touched=False`), which is the same convention Earth Engine's
    `reduceRegions`/`getRegion` use — so pixel membership matches the
    exploratory work rather than dilating it by a ring of edge pixels.
    """
    path = config.aoi_path(aoi_name)
    gdf = gpd.read_parquet(path).reset_index(drop=True)
    poly_crs = polygon_crs(gdf, tile_bounds)
    # One sub-grid per zone, each on its own zone's S2 lattice. For a single-zone
    # AOI this is exactly the old one-CRS build (same bbox, same pixels, same ids).
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

    # Carry whatever identifier the source polygons have, so alerts can be
    # reported per polygon without re-joining geometry later.
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
# Sentinel-2 MGRS tiles overlap by ~9.8 km, so a pixel near a seam is imaged by
# two tiles on the same acquisition. Those two granules do NOT agree: Sen2Cor
# runs its atmospheric correction per tile, so BOA reflectance in the overlap
# differs (measured on the 32VNL/32VNM seam: ~99% of band values differ, up to
# 2041 DN, while SCL agrees exactly — so it is radiometry, not geolocation).
#
# In NBR the difference largely cancels (max 0.0185, none above the 0.075 loss
# gate), so it will not fire an alert on its own. The reason to fix it anyway is
# that the bias is TILE-DEPENDENT, not random: a pixel whose series is fed
# mostly by one tile and later mostly by the other sees a systematic step, which
# is precisely the signal S-CCD exists to detect. Pinning each pixel to a single
# home tile removes the whole class of problem and costs nothing — both tiles
# see the same acquisitions, so no observation is lost.
#
# The home tile is the one in which the pixel sits FURTHEST FROM THE TILE EDGE.
# Tile edges are where resampling and swath-boundary effects are worst, so the
# most interior view is the better observation as well as a deterministic one.


def assign_home_tiles(aoi_name: str, tile_bounds: dict[str, tuple]) -> pd.DataFrame:
    """Add a `home_tile` column to the grid. `tile_bounds` maps tile -> bounds."""
    df = load_grid(aoi_name)
    pix_crs = df["utm_crs"].to_numpy()
    x = df["utm_x"].to_numpy()
    y = df["utm_y"].to_numpy()

    best_name = np.full(len(df), "", dtype=object)
    best_margin = np.full(len(df), -np.inf)

    for tile, (bounds, tile_crs) in tile_bounds.items():
        # Only pixels gridded in the tile's own CRS: a tile can overhang into the
        # next zone, but its coordinates mean nothing on that zone's lattice.
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
