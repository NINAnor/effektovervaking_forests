"""The two block readers must return the same bytes, and the dequantisation must
be the one the parity check picked.

`condition.aef_backend` exists to make the AEF download faster, not different.
The only thing that makes that safe is a test that the GDAL path and the
`aef_loader_plus` path hand back the same int8 for the same block, so the check
here is on the raw values rather than on anything derived from them.

The network tests are skipped when `aef_loader_plus` is not importable or when
the mirror is unreachable, because this package is meant to run without either.
"""

from __future__ import annotations

import numpy as np
import pytest

from effektovervaking_forests.condition import aef, paths
from effektovervaking_forests.condition.aef import Block, dequantise
from effektovervaking_forests.condition.aef_backend import loader_available


def test_dequantise_linear_reproduces_the_superseded_scale(monkeypatch):
    monkeypatch.setattr(paths, "AEF_DEQUANT", "linear")
    monkeypatch.setattr(aef, "AEF_DEQUANT", "linear")
    got = dequantise(np.array([[-128, -127, 0, 64, 127]], dtype=np.int8))
    assert np.isnan(got[0, 0])  # nodata
    assert got.dtype == np.float32
    np.testing.assert_allclose(
        got[0, 1:], np.array([-127, 0, 64, 127]) / 127.0, rtol=0, atol=1e-7
    )


def test_dequantise_signed_square_matches_earth_engine_convention(monkeypatch):
    """(raw/127.5)^2 * sign(raw) -- the deployed rule, and the one that is right.

    `scripts/09_check_gee_parity.py` measured this at cosine 1.000000 against
    GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL where the linear read gives 0.9617.
    """
    monkeypatch.setattr(paths, "AEF_DEQUANT", "signed_square")
    monkeypatch.setattr(aef, "AEF_DEQUANT", "signed_square")
    raw = np.arange(-128, 128, dtype=np.int8).reshape(1, -1)
    got = dequantise(raw)
    want = (raw.astype(np.float64) / 127.5) ** 2 * np.sign(raw)
    assert np.isnan(got[0, 0])
    np.testing.assert_allclose(got[0, 1:], want[0, 1:], rtol=0, atol=1e-7)


def test_dequantise_rejects_an_unknown_rule(monkeypatch):
    monkeypatch.setattr(aef, "AEF_DEQUANT", "sqrt")
    with pytest.raises(ValueError, match="AEF_DEQUANT"):
        dequantise(np.zeros((1, 4), dtype=np.int8))


def test_signed_square_is_the_one_that_gives_unit_norm_vectors(monkeypatch):
    """AlphaEarth embeddings are unit-norm; only one of the two rules returns that.

    This is the offline half of the parity argument: it needs no Earth Engine
    account, only the fact that the published embedding lies on the unit sphere.
    """
    rng = np.random.default_rng(0)
    # a plausible unit vector, quantised the way the mirror stores it
    v = rng.normal(size=(200, 64))
    v /= np.linalg.norm(v, axis=1, keepdims=True)
    raw = np.rint(np.sign(v) * np.sqrt(np.abs(v)) * 127.5).astype(np.int8)

    monkeypatch.setattr(aef, "AEF_DEQUANT", "signed_square")
    n_sq = np.linalg.norm(dequantise(raw), axis=1)
    monkeypatch.setattr(aef, "AEF_DEQUANT", "linear")
    n_lin = np.linalg.norm(dequantise(raw), axis=1)

    assert abs(np.median(n_sq) - 1.0) < 0.01
    assert np.median(n_lin) > 2.0


# The same 32N tile the reader tests use, present in every year of the archive.
TILE = {
    "path": (
        "s3://us-west-2.opendata.source.coop/tge-labs/aef/v1/annual/2024/32N/"
        "x64tikbxbqfh3zkih-0000000000-0000008192.tiff"
    ),
    "year": 2024,
    "utm_zone": "32N",
    "crs": "EPSG:32632",
    "utm_west": 581920.0,
    "utm_south": 6717440.0,
    "utm_east": 663840.0,
    "utm_north": 6799360.0,
}


@pytest.mark.network
@pytest.mark.skipif(not loader_available(), reason="aef_loader_plus not installed")
def test_both_backends_return_the_same_block():
    """The point of the whole module: same block, same values, either reader.

    Compared after dequantisation because both backends run the same
    `dequantise`, so this covers the bytes and the placement together -- a flip
    or an off-by-one block would show up here as a whole-array mismatch.
    """
    from effektovervaking_forests.condition.aef import tile_uri

    tile = dict(TILE, uri=tile_uri(TILE["path"]))
    blk = Block(brow=2, bcol=5)
    with aef.open_tile(tile, "gdal") as g:
        a, ta = g.read_block(blk)
    with aef.open_tile(tile, "loader") as ldr:
        b, tb = ldr.read_block(blk)
    assert ta == tb, "the two backends disagree about where the block is"
    assert a.shape == b.shape == (64, 1024, 1024)
    np.testing.assert_array_equal(np.isnan(a), np.isnan(b))
    np.testing.assert_array_equal(a[~np.isnan(a)], b[~np.isnan(b)])


@pytest.mark.network
@pytest.mark.skipif(not loader_available(), reason="aef_loader_plus not installed")
def test_the_chunk_profile_changes_speed_and_not_bytes():
    """`all-bands` is a performance profile, so prove it is only that.

    The backend asks for `chunks="all-bands"` because it reads whole blocks with
    every band: that issues a block's 64 per-band ranges as one task instead of
    64, and on a 16-block tile it is 35.9 s against 4.9 s. The profile only
    regroups Dask tasks -- spatial chunks stay aligned to the COG block grid --
    but the pipeline now depends on it, so pin that the values do not move.
    """
    import asyncio

    import numpy as np

    from effektovervaking_forests.condition.aef_backend import (
        _ensure_on_path,
        _tile_info,
    )

    _ensure_on_path()
    from aef_loader import VirtualTiffReader

    from effektovervaking_forests.condition.aef import tile_uri

    tile = dict(TILE, uri=tile_uri(TILE["path"]))
    info = _tile_info(tile)

    def read(profile):
        async def _open():
            async with VirtualTiffReader() as r:
                return await r.open_tiles_by_zone([info], chunks=profile)

        da = asyncio.run(_open())[TILE["utm_zone"]].ds["embeddings"]
        west = TILE["utm_west"] + 5 * 10240.0
        south = TILE["utm_south"] + 2 * 10240.0
        sub = da.sel(
            x=slice(west + 5, west + 10240 - 5), y=slice(south + 5, south + 10240 - 5)
        )
        return np.asarray(sub.compute()).squeeze(axis=0)

    np.testing.assert_array_equal(read(None), read("all-bands"))
