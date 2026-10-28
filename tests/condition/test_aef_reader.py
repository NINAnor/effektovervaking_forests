"""The reader's two load-bearing claims about the mirror, checked against it.

Marked `network`: these hit data.source.coop. Run the rest with
`pytest -m "not network"`.
"""

import numpy as np
import pytest
import rasterio
from rasterio.windows import from_bounds

from effektovervaking_forests.condition.aef import (
    BLOCK,
    Block,
    _read_block,
    blocks_for_geometries,
    dequantise,
    gdal_env,
    tile_uri,
)

# One 32N tile over southern Norway, present in every year of the archive.
TILE_PATH = (
    "s3://us-west-2.opendata.source.coop/tge-labs/aef/v1/annual/2024/32N/"
    "x64tikbxbqfh3zkih-0000000000-0000008192.tiff"
)
TILE = {
    "utm_west": 581920.0,
    "utm_south": 6717440.0,
    "utm_east": 663840.0,
    "utm_north": 6799360.0,
    "crs": "EPSG:32632",
}


def test_tile_uri_defaults_to_the_https_mirror_and_keeps_the_tiff():
    u = tile_uri(TILE_PATH)
    assert u.startswith("/vsicurl/https://data.source.coop/")
    assert u.endswith(".tiff"), (
        "the .vrt re-blocks to 128x512 and defeats block alignment"
    )
    assert tile_uri(TILE_PATH, via="s3").startswith("/vsis3/")


def test_blocks_for_geometries_deduplicates_and_stays_in_range():
    import geopandas as gpd
    from shapely.geometry import box

    # three small stands, two of them inside the same 1024-pixel block
    g = gpd.GeoDataFrame(
        geometry=[
            box(582000, 6717500, 582100, 6717600),
            box(582300, 6717800, 582400, 6717900),
            box(600000, 6750000, 600100, 6750100),
        ],
        crs=TILE["crs"],
    )
    blocks = blocks_for_geometries(g, TILE)
    assert len(blocks) == 2, "two stands in one block must cost one read"
    assert all(0 <= b.brow < 8 and 0 <= b.bcol < 8 for b in blocks)


@pytest.mark.network
def test_the_tile_really_is_bottom_up():
    """The premise of the flip. If the mirror ever reissues these north-up, the
    flip must stop -- so assert the orientation rather than assume it."""
    with gdal_env(), rasterio.open(tile_uri(TILE_PATH)) as ds:
        assert ds.transform.e > 0, "expected a positive y step (rows run S -> N)"
        assert ds.transform.f == pytest.approx(TILE["utm_south"]), (
            "expected the origin at the tile's SOUTH edge"
        )


@pytest.mark.network
def test_block_pixels_land_where_the_returned_transform_says_they_do():
    """The bug this pins: applying the north-up row convention to a bottom-up
    file reads the block mirrored about the tile's centre line. That is a silent
    wrong answer -- a valid array from the wrong place -- so it is checked
    against rasterio's own georeferencing (`ds.index`), not against our maths."""
    blk = Block(3, 3)
    with gdal_env(), rasterio.open(tile_uri(TILE_PATH)) as ds:
        arr, transform = _read_block(ds, blk)
        for row, col in [(5, 5), (500, 300), (1000, 1020), (777, 123)]:
            x, y = transform * (col + 0.5, row + 0.5)
            r, c = ds.index(x, y)
            # Dequantised by the reader's own rule, so this stays a test of
            # WHERE the block came from and never of how int8 becomes float --
            # `tests/test_aef_backend.py` owns that.
            native = dequantise(ds.read(1, window=((r, r + 1), (c, c + 1))))[0, 0]
            mine = arr[0, row, col]
            assert (np.isnan(native) and np.isnan(mine)) or np.isclose(native, mine), (
                f"block[{row},{col}] at ({x:.0f},{y:.0f}): {mine} != {native}"
            )

    # and the affine we hand back is north-up over the block's true ground
    assert transform.e < 0
    assert transform.c == pytest.approx(blk.bounds(TILE)[0])
    assert transform.f == pytest.approx(blk.bounds(TILE)[3])


@pytest.mark.network
@pytest.mark.slow
def test_flipped_block_matches_the_north_up_vrt_pixel_for_pixel():
    """Same check against the sibling warped .vrt, the mirror's own north-up
    view. Kept because it is fully independent of the .tiff's self-description,
    but marked slow: a warped-VRT read of this area takes minutes."""
    blk = Block(3, 3)
    with gdal_env():
        with rasterio.open(tile_uri(TILE_PATH)) as ds:
            arr, _ = _read_block(ds, blk)
        with rasterio.open(tile_uri(TILE_PATH).replace(".tiff", ".vrt")) as ds2:
            b = blk.bounds(TILE)
            patch = (b[0] + 3000, b[1] + 3000, b[0] + 3640, b[1] + 3640)
            win = from_bounds(*patch, ds2.transform)
            ref = ds2.read(window=win)
            rtr = ds2.window_transform(win)
    ref = dequantise(ref)

    col, row = (~_transform_of(arr, blk)) * (rtr.c + 5, rtr.f - 5)
    row, col = int(row), int(col)
    assert np.allclose(
        arr[:, row : row + ref.shape[1], col : col + ref.shape[2]], ref, equal_nan=True
    )


def _transform_of(arr, blk):
    from rasterio.transform import Affine

    b = blk.bounds(TILE)
    return Affine(10.0, 0.0, b[0], 0.0, -10.0, b[3])


@pytest.mark.network
def test_a_block_read_is_cheap_relative_to_a_tiny_window():
    """The premise of the whole design: window size is nearly free, block count
    is not. If this ever inverts, the block-aligned reader is over-engineering."""
    import time

    with gdal_env(), rasterio.open(tile_uri(TILE_PATH)) as ds:
        t0 = time.time()
        ds.read(window=((2048, 2048 + BLOCK), (2048, 2048 + BLOCK)))
        full = time.time() - t0
        t0 = time.time()
        ds.read(window=((5120, 5152), (5120, 5152)))
        tiny = time.time() - t0
    assert full < tiny * 8, (
        f"a full 1024x1024 block took {full:.1f}s vs {tiny:.1f}s for 32x32 -- "
        "block-aligned reading is only worth it while these are comparable"
    )
