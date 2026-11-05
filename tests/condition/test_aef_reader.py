"""AEF block reader: geolocation against the mirror.

Tests marked `network` hit data.source.coop (`pytest -m network`).
"""

import numpy as np
import pytest

from effektovervaking_forests.condition.aef import (
    Block,
    blocks_for_geometries,
    dequantise,
    open_tile,
)

# One 32N tile over southern Norway, present in every year of the archive.
TILE_PATH = (
    "s3://us-west-2.opendata.source.coop/tge-labs/aef/v1/annual/2024/32N/"
    "x64tikbxbqfh3zkih-0000000000-0000008192.tiff"
)
TILE = {
    "path": TILE_PATH,
    "year": 2024,
    "utm_zone": "32N",
    "utm_west": 581920.0,
    "utm_south": 6717440.0,
    "utm_east": 663840.0,
    "utm_north": 6799360.0,
    "crs": "EPSG:32632",
}
HTTPS = TILE_PATH.replace(
    "s3://us-west-2.opendata.source.coop/", "https://data.source.coop/"
)


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
def test_block_pixels_land_where_the_returned_transform_says_they_do():
    """A bottom-up file read with the north-up convention comes back mirrored:
    a valid array from the wrong place. Checked against rasterio's own
    georeferencing of the file, not against our maths."""
    import rasterio

    blk = Block(3, 3)
    with open_tile(TILE) as src:
        arr, transform = src.read_block(blk)
    with rasterio.open(HTTPS) as ds:
        assert ds.transform.e > 0, "the mirror's .tiff is bottom-up"
        for row, col in [(5, 5), (500, 300), (1000, 1020), (777, 123)]:
            x, y = transform * (col + 0.5, row + 0.5)
            r, c = ds.index(x, y)
            native = dequantise(ds.read(1, window=((r, r + 1), (c, c + 1))))[0, 0]
            mine = arr[0, row, col]
            assert (np.isnan(native) and np.isnan(mine)) or np.isclose(native, mine)
    assert transform.e < 0
    assert transform.c == pytest.approx(blk.bounds(TILE)[0])
    assert transform.f == pytest.approx(blk.bounds(TILE)[3])
