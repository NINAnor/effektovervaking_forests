"""Two ways to get a 1024x1024 block of an AEF tile, behind one interface.

`condition.aef` reads block-aligned because the COGs are band-interleaved: a
32x32 window costs the same 64 compressed blocks as the whole 1024x1024 one, so
the block count is everything and the window size is almost free. That decision
is unchanged here. What this module adds is a choice of *who fetches the block*.

`gdal`
    rasterio over `/vsicurl`, with `GDAL_HTTP_MULTIRANGE` so a block's 64
    per-band byte ranges are issued concurrently. This is what the pipeline
    shipped and it stays the default.

`loader`
    `aef_loader_plus` (VirtualiZarr + virtual-tiff over obstore), the fork at
    P:/154001_nyvest/landcover_Geethen/aef_loader_plus. Two things it does that
    the GDAL path cannot:

      * **the COG header is parsed once, ever.** Turning a tile's header into a
        VirtualiZarr `ManifestStore` costs 1.4-2.7 s and never changes, so it is
        cached to disk under `data/cache/aef_manifests/`. Measured here: 2.29 s
        cold, 0.07 s warm. There are 270 (tile, year) shards in this AOI, so a
        re-run stops paying about eight minutes of header parsing.
      * **a tile's blocks are fetched concurrently** rather than one after the
        next, in batches of `BATCH_BLOCKS` so a 16-block tile does not hold 16 x
        67 MB of int8 in memory at once. Measured on a 16-block tile: 44.2 s
        serial through GDAL against 36.8 s, so about 1.15x -- the transfer is
        bandwidth-bound at the Cloudflare front end, not round-trip-bound, and
        this is the smaller half of the gain.

Both backends are verified to return the **same int8**, block for block, by
`tests/test_aef_backend.py`. The bytes are not in question; what you do with
them afterwards is -- see `AEF_DEQUANT` in `condition.paths`.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from effektovervaking_forests.condition.aef import TILE_SIZE, ensure_index
from effektovervaking_forests.condition.paths import AEF_INDEX, BLOCK_M, CACHE

__all__ = ["BATCH_BLOCKS", "MANIFEST_CACHE", "LoaderTile", "loader_available"]

BLOCK = 1024
BATCH_BLOCKS = 8  # blocks computed in one concurrent pass; 8 x 67 MB int8
MAX_CONCURRENCY = 16  # shared limit on tile header work, across zones
MANIFEST_CACHE = CACHE / "aef_manifests"

# Where the fork lives. The `geo` env carries an `aef_loader_plus.pth` pointing
# at a checkout under $HOME, and that copy is OLDER than the one on the project
# share -- it is missing the STRtree index query and the uint8-view LUT gather.
# So the share is put in front of it rather than trusted to be the one that
# imports. Set AEF_LOADER_PATH to override, or point it at nothing to take
# whatever `import aef_loader` finds on its own.
AEF_LOADER_PATH = os.environ.get(
    "AEF_LOADER_PATH",
    "/data/P-Prosjekter2/154001_nyvest/landcover_Geethen/aef_loader_plus",
)


def _ensure_on_path() -> None:
    p = AEF_LOADER_PATH
    if p and Path(p, "aef_loader", "__init__.py").exists() and p not in sys.path:
        sys.path.insert(0, p)


def loader_available() -> bool:
    """Is `aef_loader_plus` importable in this environment?"""
    _ensure_on_path()
    try:
        # dask too: the loader imports without it, but prefetch() needs it, and
        # `--backend auto` must fall back to GDAL rather than fail mid-download.
        import aef_loader  # noqa: F401
        import dask  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure means "use GDAL"
        return False
    return True


def _tile_info(tile: dict):
    """One row of `tiles_for_geometries` -> the `AEFTileInfo` the loader wants.

    The WGS84 corners are not in that row -- `tiles_for_geometries` selects only
    the UTM ones -- so they are read back out of the same cached index the row
    came from, which is the file the loader would have read anyway.
    """
    _ensure_on_path()
    from aef_loader import AEFTileInfo, DataSource

    idx = pd.read_parquet(
        ensure_index(AEF_INDEX),
        columns=["path", "wgs84_west", "wgs84_south", "wgs84_east", "wgs84_north"],
    )
    row = idx.loc[idx["path"] == tile["path"]].iloc[0]
    crs = str(tile["crs"])
    # A tile is always TILE_SIZE x TILE_SIZE at 10 m, so the far corner can be
    # derived when a caller carries only the origin -- the bad-pool block plan
    # does, and a KeyError inside a worker thread is a poor way to find out.
    span = TILE_SIZE * BLOCK_M / BLOCK
    west, south = float(tile["utm_west"]), float(tile["utm_south"])
    return AEFTileInfo(
        id=str(tile.get("tile_id", tile["path"])),
        path=tile["path"],
        year=int(tile["year"]),
        bbox=(
            float(row.wgs84_west),
            float(row.wgs84_south),
            float(row.wgs84_east),
            float(row.wgs84_north),
        ),
        crs_epsg=int(crs.split(":")[1]) if ":" in crs else 4326,
        utm_zone=tile["utm_zone"],
        utm_bounds=(
            west,
            south,
            float(tile.get("utm_east", west + span)),
            float(tile.get("utm_north", south + span)),
        ),
        source=DataSource.SOURCE_COOP,
    )


class LoaderTile:
    """One open AEF tile, read through `aef_loader_plus`.

    Used as a context manager, mirroring `rasterio.open`, so `extract_from_tile`
    treats the two backends the same way:

        with LoaderTile(tile) as src:
            src.prefetch(blocks)              # optional, and where the win is
            arr, transform = src.read_block(blk)

    `read_block` returns the same `(64, 1024, 1024)` float32 array and north-up
    `Affine` as `condition.aef._read_block`, dequantised by the same function, so
    nothing downstream can tell which backend produced it.
    """

    def __init__(self, tile: dict, manifest_cache: Path | None = MANIFEST_CACHE):
        self.tile = tile
        self.manifest_cache = manifest_cache
        self._da = None
        self._cache: dict = {}

    def __enter__(self) -> LoaderTile:
        _ensure_on_path()
        from aef_loader import VirtualTiffReader

        if self.manifest_cache is not None:
            self.manifest_cache.mkdir(parents=True, exist_ok=True)
        info = _tile_info(self.tile)

        async def _open():
            async with VirtualTiffReader(
                manifest_cache_dir=self.manifest_cache
            ) as reader:
                # `all-bands` is the profile that matches how this pipeline reads:
                # one dask task per stored 1024x1024 block carrying all 64 bands,
                # so a block's 64 per-band byte ranges are issued together. That
                # is the same coalescing GDAL_HTTP_MULTIRANGE buys the rasterio
                # path, and without it the loader is SLOWER than GDAL, not faster.
                # Measured on a 16-block 32N tile: `native` (one chunk per band,
                # 4,096 per tile, 1,024 separate small reads) 35.9 s against
                # `all-bands` (64 per tile) 4.9 s, with GDAL at 32.6 s.
                # Spatial chunks stay block-aligned either way, so the decoded
                # bytes are identical -- checked in tests/test_aef_backend.py.
                return await reader.open_tiles_by_zone(
                    [info], chunks="all-bands", max_concurrency=MAX_CONCURRENCY
                )

        tree = asyncio.run(_open())
        self._da = tree[self.tile["utm_zone"]].ds["embeddings"]
        return self

    def __exit__(self, *exc) -> None:
        self._da, self._cache = None, {}

    def _window(self, blk):
        """The tile-relative slice for one block, in the file's own y direction.

        The `.tiff` is bottom-up -- y ascends south to north -- so the slice is
        ascending and the array comes back bottom-up, exactly as GDAL's does
        before `_read_block` flips it.
        """
        west = float(self.tile["utm_west"]) + blk.bcol * BLOCK_M
        south = float(self.tile["utm_south"]) + blk.brow * BLOCK_M
        half = BLOCK_M / BLOCK / 2.0
        return self._da.sel(
            x=slice(west + half, west + BLOCK_M - half),
            y=slice(south + half, south + BLOCK_M - half),
        )

    def prefetch(self, blocks) -> None:
        """Fetch several blocks in one concurrent pass, in batches.

        This is the loader backend's reason to exist: GDAL reads a tile's blocks
        one after the next, and these are 40 MB reads over the public mirror.
        """
        import dask

        todo = [b for b in blocks if b not in self._cache]
        for i in range(0, len(todo), BATCH_BLOCKS):
            batch = todo[i : i + BATCH_BLOCKS]
            got = dask.compute(
                *[self._window(b).data for b in batch], scheduler="threads"
            )
            for b, arr in zip(batch, got):
                self._cache[b] = np.asarray(arr).squeeze(axis=0)

    def read_block(self, blk):
        """(64, 1024, 1024) float32 north-up, and its north-up `Affine`."""
        from affine import Affine

        from effektovervaking_forests.condition.aef import dequantise

        if blk not in self._cache:
            self.prefetch([blk])
        raw = self._cache[blk]
        step = BLOCK_M / BLOCK
        west = float(self.tile["utm_west"]) + blk.bcol * BLOCK_M
        south = float(self.tile["utm_south"]) + blk.brow * BLOCK_M
        arr = dequantise(raw[:, ::-1, :])  # bottom-up -> north-up
        return arr, Affine(step, 0.0, west, 0.0, -step, south + BLOCK_M)
