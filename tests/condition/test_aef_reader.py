"""AEF block reader: geolocation against the mirror, and the Earth Engine backup.

Tests marked `network` hit data.source.coop (`pytest -m network`).
"""

import importlib.machinery
import sys
import types

import numpy as np
import pytest

from effektovervaking_forests import config
from effektovervaking_forests.condition import aef
from effektovervaking_forests.condition.aef import (
    BANDS,
    BLOCK,
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


@pytest.fixture
def fake_ee(monkeypatch: pytest.MonkeyPatch) -> list:
    """An `ee` module whose computePixels returns the chunk's row offset."""
    calls = []

    class Img:
        def __getattr__(self, name):
            return lambda *a, **k: self

    def compute_pixels(req):
        g = req["grid"]
        calls.append(g)
        r = round(
            (TILE["utm_south"] + 4 * BLOCK * 10 - g["affineTransform"]["translateY"])
            / 10
        )
        n = g["dimensions"]["height"]
        a = np.zeros((n, n), dtype=[(b, "f4") for b in BANDS])
        a["A00"] = r
        a["A01"][0, 0] = aef.EE_NODATA
        return a

    ee = types.SimpleNamespace(
        ImageCollection=lambda *_: Img(),
        Geometry=types.SimpleNamespace(Rectangle=lambda *a, **k: None),
        Filter=types.SimpleNamespace(eq=lambda *a: None),
        Initialize=lambda **k: None,
        data=types.SimpleNamespace(
            is_initialized=lambda: True, computePixels=compute_pixels
        ),
    )
    ee.__spec__ = importlib.machinery.ModuleSpec("ee", None)
    monkeypatch.setitem(sys.modules, "ee", ee)
    monkeypatch.setattr(config, "EE_PROJECT", "test-project")
    return calls


def test_ee_block_is_assembled_north_up_from_chunks(fake_ee):
    blk = Block(4, 0)  # north edge at utm_south + 5 blocks
    arr = aef._ee_block(TILE, blk)
    assert arr.shape == (len(BANDS), BLOCK, BLOCK)
    assert len(fake_ee) == (BLOCK // aef.EE_CHUNK) ** 2
    assert all(g["crsCode"] == TILE["crs"] for g in fake_ee)
    # each row holds its chunk's distance south of the block's north edge
    assert arr[0, 0, 0] == -1024 and arr[0, aef.EE_CHUNK, 0] == -1024 + aef.EE_CHUNK
    assert np.isnan(arr[1, 0, 0]), "EE_NODATA becomes NaN"


def test_failed_source_coop_read_falls_back_to_ee(fake_ee, monkeypatch):
    def broken(self, blk):
        raise OSError("520 from Cloudflare")

    monkeypatch.setattr(aef._Tile, "_fetch", broken)
    with open_tile(TILE) as src:
        arr, transform = src.read_block(Block(4, 0))
    assert fake_ee and arr.shape == (len(BANDS), BLOCK, BLOCK)
    assert transform.f == pytest.approx(Block(4, 0).bounds(TILE)[3])


def test_without_an_ee_project_the_read_error_propagates(monkeypatch):
    def broken(self, blk):
        raise OSError("520 from Cloudflare")

    monkeypatch.setattr(aef._Tile, "_fetch", broken)
    monkeypatch.setattr(config, "EE_PROJECT", None)
    with open_tile(TILE) as src, pytest.raises(OSError):
        src.read_block(Block(0, 0))


def test_a_failing_neighbour_does_not_fail_the_requested_block(monkeypatch):
    """A batch error is retried for the requested block alone."""
    import dask

    calls = []

    def compute(*wins, **_):
        calls.append(len(wins))
        if len(wins) > 1:
            raise OSError("520 on a neighbour")
        return (np.zeros((len(BANDS), BLOCK, BLOCK), dtype=np.int8),)

    monkeypatch.setattr(dask, "compute", compute)
    src = open_tile(TILE)
    src._da = types.SimpleNamespace(
        data=np.zeros((1, len(BANDS), 8 * BLOCK, 8 * BLOCK))
    )
    src.prefetch([Block(0, 0), Block(0, 1), Block(0, 2)])
    arr, _ = src.read_block(Block(0, 0))
    assert calls == [3, 1] and arr.shape == (len(BANDS), BLOCK, BLOCK)
    assert src._plan == [Block(0, 1), Block(0, 2)], "neighbours stay planned"
