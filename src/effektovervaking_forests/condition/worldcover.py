"""ESA WorldCover as the "transformed" end of the condition axis.

The DegreeofRecovery paper anchored its bad pool on WorldCover, and this project
originally rejected that: an intact kalklindeskog and a spruce plantation on top
of one are both WorldCover class 10, so WorldCover cannot see *degradation*.
That objection still stands and is why the good pool comes from the surveyors'
`tilstand` labels. What WorldCover is used for here is the other end of the
axis, where it is not blind at all -- it can say with confidence where the
forest has been replaced outright.

Only **cropland (40) and built-up (50)** count. Grassland (30) is the tempting
one and is left out on purpose: in Norway it absorbs natural fjell vegetation
and semi-natural pasture as readily as it absorbs a clearcut, so including it
would put genuinely natural ground into the pool that defines "transformed".
Bare/sparse (60) is excluded for the same reason -- Norwegian bare ground is
overwhelmingly rock and scree.

Pixels are drawn from a 3-8 km **annulus** around each stand. The inner radius
keeps the stand's own edge and its immediate surroundings out of the pool; the
outer one keeps the reference in the same region, terrain and acquisition
history as the stand it is scoring. A national pool would let a Trøndelag
industrial estate define what "transformed" means for a Nordland stand, in an
embedding space that encodes climate and phenology as readily as land cover.

The tiles are 3x3 degree, 10 m, EPSG:4326 COGs served anonymously over HTTPS,
so this needs no credentials -- the same property that makes the AlphaEarth
Source Cooperative mirror usable without Earth Engine.
"""

from __future__ import annotations

import math
import os

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import rasterize
from rasterio.windows import from_bounds
from shapely.strtree import STRtree

from effektovervaking_forests.condition.paths import (
    ANNULUS_INNER_M,
    ANNULUS_OUTER_M,
    TRANSFORMED_CLASSES,
    WORLDCOVER_URL,
)

__all__ = [
    "tile_url",
    "tiles_for_bounds",
    "read_classes",
    "annuli",
    "transformed_in_block",
]

# WorldCover is served in 3x3 degree tiles named by their SOUTH-WEST corner.
TILE_DEG = 3

_WC_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MULTIRANGE": "YES",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_VERSION": "2",
    "GDAL_HTTP_MAX_RETRY": 5,
    "GDAL_HTTP_RETRY_DELAY": 2,
    "VSI_CACHE": True,
    "VSI_CACHE_SIZE": 32 * 1024 * 1024,
}


def wc_env(**overrides) -> rasterio.Env:
    cfg = dict(_WC_ENV)
    cfg.update(overrides)
    for k, v in cfg.items():
        os.environ.setdefault(k, str(v))
    return rasterio.Env(**cfg)


def tile_url(lat: int, lon: int) -> str:
    """URL of the tile whose south-west corner is (lat, lon), both multiples of 3."""
    return WORLDCOVER_URL.format(
        ns="N" if lat >= 0 else "S",
        lat=abs(lat),
        ew="E" if lon >= 0 else "W",
        lon=abs(lon),
    )


def tiles_for_bounds(
    bounds: tuple[float, float, float, float],
) -> list[tuple[int, int]]:
    """South-west corners of every 3x3 degree tile a lon/lat box touches."""
    minx, miny, maxx, maxy = bounds
    lons = range(
        int(math.floor(minx / TILE_DEG) * TILE_DEG),
        int(math.floor(maxx / TILE_DEG) * TILE_DEG) + 1,
        TILE_DEG,
    )
    lats = range(
        int(math.floor(miny / TILE_DEG) * TILE_DEG),
        int(math.floor(maxy / TILE_DEG) * TILE_DEG) + 1,
        TILE_DEG,
    )
    return [(la, lo) for la in lats for lo in lons]


def read_classes(
    bounds: tuple[float, float, float, float],
) -> tuple[np.ndarray, object]:
    """Read the WorldCover class raster over a lon/lat box.

    Mosaics across tile seams, since a 10 km block near a 3-degree boundary is
    split over two files. Pixels outside any tile come back as 0, WorldCover's
    own no-data, which is never a transformed class and so is simply never
    selected.
    """
    minx, miny, maxx, maxy = bounds
    tiles = tiles_for_bounds(bounds)
    out = None
    transform = None
    with wc_env():
        for lat, lon in tiles:
            try:
                with rasterio.open("/vsicurl/" + tile_url(lat, lon)) as ds:
                    if out is None:
                        # Snap the mosaic to the first tile's grid; all WorldCover
                        # tiles share one global 1/12000-degree grid, so a window
                        # cut from any of them aligns with a window cut from
                        # another.
                        win = from_bounds(minx, miny, maxx, maxy, ds.transform)
                        win = win.round_offsets().round_lengths()
                        transform = ds.window_transform(win)
                        out = np.zeros((int(win.height), int(win.width)), dtype="uint8")
                    win = from_bounds(minx, miny, maxx, maxy, ds.transform)
                    win = win.round_offsets().round_lengths()
                    arr = ds.read(1, window=win, boundless=True, fill_value=0)
                    out = np.where(out == 0, arr[: out.shape[0], : out.shape[1]], out)
            except rasterio.errors.RasterioIOError:
                continue  # tile does not exist (all ocean) -- leave those pixels 0
    if out is None:
        raise RuntimeError(f"no WorldCover tile covers {bounds}")
    return out, transform


def annuli(
    polygons: gpd.GeoDataFrame,
    inner_m: float = ANNULUS_INNER_M,
    outer_m: float = ANNULUS_OUTER_M,
    outer_scale: pd.Series | None = None,
) -> gpd.GeoDataFrame:
    """The reference ring around each stand, in the polygons' own (metric) CRS.

    `outer_scale` optionally gives a per-polygon outer radius, which is how a
    stand with no transformed land within 8 km gets its search widened rather
    than left with an empty bad pool -- an olivinskog stand on an outer-coast
    peninsula can have literally no cropland or built-up within the default
    ring. The inner radius is never widened: its job is to exclude the stand's
    own surroundings, and that job does not change with the outer radius.
    """
    g = polygons[["poly_id", "forest_type", "geometry"]].reset_index(drop=True)
    outer = (
        g["poly_id"].map(outer_scale).fillna(outer_m).to_numpy()
        if outer_scale is not None
        else np.full(len(g), outer_m)
    )
    ring = [
        geom.buffer(float(o)).difference(geom.buffer(inner_m))
        for geom, o in zip(g.geometry, outer)
    ]
    out = g.copy()
    out["geometry"] = ring
    out["outer_m"] = outer
    out["inner_m"] = inner_m
    return out.set_geometry("geometry")


def transformed_in_block(
    block_bounds: tuple[float, float, float, float],
    utm_epsg: int,
    ring: gpd.GeoDataFrame,
    classes: tuple[int, ...] = TRANSFORMED_CLASSES,
    max_points: int = 20_000,
    rng: np.random.Generator | None = None,
) -> pd.DataFrame:
    """Transformed AlphaEarth pixels inside one AEF block, and which stands they serve.

    Works on the **AlphaEarth** 10 m grid, not WorldCover's: the block's own
    pixel centres are generated, filtered to the annuli, reprojected to lon/lat
    and looked up in WorldCover by nearest neighbour. Going this way round means
    the coordinates that come out are exactly the pixels the extractor will
    later read, with no grid-snapping step to get wrong -- the two rasters are
    both 10 m but on different projections and do not share a grid.

    The annulus filter runs first because it is the cheap one and it typically
    removes most of a 1024x1024 block, leaving far fewer points to reproject.

    Returns long rows `x, y, poly_id, wc_class` -- many-to-many, since annuli of
    neighbouring stands overlap heavily and one industrial estate can legitimately
    serve a dozen stands.
    """
    rng = rng or np.random.default_rng(0)
    w, s, e, n = block_bounds
    nx = int(round((e - w) / 10.0))
    ny = int(round((n - s) / 10.0))
    if nx <= 0 or ny <= 0:
        return pd.DataFrame(columns=["x", "y", "poly_id", "wc_class"])

    local = ring.to_crs(utm_epsg)
    local = local[
        local.geometry.apply(
            lambda g: (
                not (
                    g.is_empty
                    or g.bounds[2] <= w
                    or g.bounds[0] >= e
                    or g.bounds[3] <= s
                    or g.bounds[1] >= n
                )
            )
        )
    ]
    if local.empty:
        return pd.DataFrame(columns=["x", "y", "poly_id", "wc_class"])

    # A boolean "inside some annulus" mask over the block, via one rasterise.
    from rasterio.transform import Affine

    tr = Affine(10.0, 0.0, w, 0.0, -10.0, n)
    mask = rasterize(
        ((g, 1) for g in local.geometry),
        out_shape=(ny, nx),
        transform=tr,
        fill=0,
        all_touched=False,
        dtype="uint8",
    ).astype(bool)
    rows, cols = np.nonzero(mask)
    if not len(rows):
        return pd.DataFrame(columns=["x", "y", "poly_id", "wc_class"])

    xs = w + (cols + 0.5) * 10.0
    ys = n - (rows + 0.5) * 10.0

    # Look the survivors up in WorldCover.
    from pyproj import Transformer

    to_ll = Transformer.from_crs(utm_epsg, 4326, always_xy=True)
    lon, lat = to_ll.transform(xs, ys)

    # AlphaEarth populates a piece of ground only in its NATURAL UTM zone. The
    # tiles carry valid coordinates well past their zone -- a 31N tile addresses
    # easting 662,000, which is 6.1 E, in zone 32 -- but every band there is
    # nodata. Measured: the 31N tile is 31% finite at that block while the 32N
    # tile covering the same ground is 100% finite. Candidates outside the
    # block's own zone are therefore silently unreadable, and dropping them here
    # is what makes the set-cover choose the tile that actually holds the data
    # instead of one that merely spans the coordinates.
    zone = utm_epsg - (32600 if utm_epsg < 32700 else 32700)
    natural = np.floor((lon + 180.0) / 6.0).astype(int) + 1
    in_zone = natural == zone
    if not in_zone.any():
        return pd.DataFrame(columns=["x", "y", "poly_id", "wc_class"])
    xs, ys, lon, lat = xs[in_zone], ys[in_zone], lon[in_zone], lat[in_zone]

    wc, wtr = read_classes((lon.min(), lat.min(), lon.max(), lat.max()))
    inv = ~wtr
    c = np.floor(inv.a * lon + inv.b * lat + inv.c).astype(int)
    r = np.floor(inv.d * lon + inv.e * lat + inv.f).astype(int)
    ok = (r >= 0) & (r < wc.shape[0]) & (c >= 0) & (c < wc.shape[1])
    cls = np.zeros(len(xs), dtype="uint8")
    cls[ok] = wc[r[ok], c[ok]]

    keep = np.isin(cls, classes)
    if not keep.any():
        return pd.DataFrame(columns=["x", "y", "poly_id", "wc_class"])
    xs, ys, cls = xs[keep], ys[keep], cls[keep]
    # A block over a town holds hundreds of thousands of built-up pixels, far
    # more than any pool needs. Thin here, before the per-stand join, so the
    # join stays small; the per-stand cap is applied later by the sampler.
    if len(xs) > max_points:
        pick = rng.choice(len(xs), size=max_points, replace=False)
        xs, ys, cls = xs[pick], ys[pick], cls[pick]

    pts = gpd.points_from_xy(xs, ys)
    tree = STRtree(local.geometry.to_numpy())
    left, right = tree.query(pts, predicate="within")
    if not len(left):
        return pd.DataFrame(columns=["x", "y", "poly_id", "wc_class"])
    return pd.DataFrame(
        {
            "x": xs[left],
            "y": ys[left],
            "poly_id": local["poly_id"].to_numpy()[right],
            "wc_class": cls[left],
        }
    )
