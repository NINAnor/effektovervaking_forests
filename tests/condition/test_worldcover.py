"""The transformed bad pool: tiling, the annulus, and the zone trap."""

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point, box

from effektovervaking_forests.condition.paths import (
    ANNULUS_INNER_M,
    ANNULUS_OUTER_M,
    TRANSFORMED_CLASSES,
)
from effektovervaking_forests.condition.worldcover import (
    annuli,
    tile_url,
    tiles_for_bounds,
)


def test_only_cropland_and_built_up_count_as_transformed():
    """Grassland absorbs Norwegian fjell and pasture, bare ground absorbs scree."""
    assert set(TRANSFORMED_CLASSES) == {40, 50}
    assert 30 not in TRANSFORMED_CLASSES
    assert 60 not in TRANSFORMED_CLASSES


def test_tile_url_names_the_south_west_corner():
    assert tile_url(60, 6).endswith("N60E006_Map.tif")
    assert tile_url(-3, -6).endswith("S03W006_Map.tif")


def test_tiles_for_bounds_covers_every_tile_a_box_straddles():
    assert tiles_for_bounds((5.9, 59.9, 6.1, 60.1)) == [
        (57, 3),
        (57, 6),
        (60, 3),
        (60, 6),
    ]
    assert tiles_for_bounds((6.1, 60.1, 6.2, 60.2)) == [(60, 6)]


def _stand():
    return gpd.GeoDataFrame(
        {
            "poly_id": ["A"],
            "forest_type": ["olivinskog"],
            "geometry": [box(0, 0, 100, 100)],
        },
        crs="EPSG:32632",
    )


def test_the_annulus_excludes_the_stands_own_surroundings():
    ring = annuli(_stand()).geometry.iloc[0]
    c = _stand().geometry.iloc[0].centroid
    assert not ring.contains(Point(c.x + 1_000, c.y))  # inside the hole
    assert ring.contains(Point(c.x + 5_000, c.y))  # in the ring
    assert not ring.contains(Point(c.x + 20_000, c.y))  # past the outside


def test_widening_the_ring_moves_only_the_outer_edge():
    """A stand with no transformed land within 8 km gets a wider search, but the
    inner radius keeps doing its job -- excluding the stand's own edge."""
    import pandas as pd

    wide = annuli(_stand(), outer_scale=pd.Series({"A": 32_000.0}))
    ring = wide.geometry.iloc[0]
    c = _stand().geometry.iloc[0].centroid
    assert wide["outer_m"].iloc[0] == 32_000.0
    assert wide["inner_m"].iloc[0] == ANNULUS_INNER_M
    assert not ring.contains(Point(c.x + 1_000, c.y))
    assert ring.contains(Point(c.x + 20_000, c.y))


def test_the_default_ring_is_the_papers():
    assert (ANNULUS_INNER_M, ANNULUS_OUTER_M) == (3_000.0, 8_000.0)


@pytest.mark.network
def test_alphaearth_is_nodata_outside_a_tiles_natural_utm_zone():
    """Why `transformed_in_block` filters candidates by zone.

    A 31N tile carries valid coordinates for easting 662,000 -- which is 6.1 E,
    in zone 32 -- but every band there is nodata. Sampling a bad pool from the
    wrong zone's tile silently returns nothing, which cost this pipeline the
    entire bad pool of 18 stands before the filter was added.
    """
    import duckdb
    from pyproj import Transformer

    from effektovervaking_forests.condition.aef import Block, ensure_index, open_tile
    from effektovervaking_forests.condition.paths import AEF_INDEX

    ensure_index(AEF_INDEX)
    x, y = 662_135.0, 6_886_295.0
    lon, _ = Transformer.from_crs(32631, 4326, always_xy=True).transform(x, y)
    assert int((lon + 180) // 6) + 1 == 32, "this point's natural zone is 32, not 31"

    tile = (
        duckdb.connect()
        .execute(
            f"""SELECT * FROM read_parquet('{AEF_INDEX}')
            WHERE year=2025 AND utm_zone='31N'
              AND utm_west=581920 AND utm_south=6881280"""  # noqa: S608 - constant index path, test-only
        )
        .df()
        .iloc[0]
        .to_dict()
    )
    with open_tile(tile) as src:
        arr, tr = src.read_block(Block(0, 7))
    inv = ~tr
    c, r = int(np.floor(inv.a * x + inv.c)), int(np.floor(inv.e * y + inv.f))
    assert not np.isfinite(arr[0, r, c])
