"""Step 2 -- download the AlphaEarth embeddings for every polygon pixel, 2017-2025.

Reads from the Source Cooperative mirror (no Earth Engine account needed); see
`condition.aef`. The unit of work and of resume is one (tile, year): each writes a
parquet shard under `data/cache/pixels/`, and a re-run skips existing shards.
Tiles run concurrently in threads (network-bound).

Reads `polygons_geo.parquet` (step 00). Output: `outputs/data/polygon_pixels.parquet`,
one row per (poly_id, year, pixel) with A00..A63 plus the pixel's UTM x/y.

Run:
    forestmon condition step 02
    ... --years 2024 --forest-type kalklindeskog --workers 8     # smoke run
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd
import pandas as pd

from effektovervaking_forests.condition.aef import (
    extract_from_tile,
    tiles_for_geometries,
)
from effektovervaking_forests.condition.paths import (
    AEF_YEARS,
    BANDS,
    CACHE,
    OUT_DATA,
    POLY_PIXELS,
)

POLY_GEO = OUT_DATA / "polygons_geo.parquet"
SHARDS = CACHE / "pixels"


def shard_path(tile) -> Path:
    return SHARDS / f"{int(tile.year)}_{tile.utm_zone}_{tile.tile_id}.parquet"


def run_tile(
    tile,
    poly: gpd.GeoDataFrame,
    all_touched: bool,
    attempts: int = 3,
) -> tuple[str, int, float]:
    out = shard_path(tile)
    if out.exists():
        return out.name, -1, 0.0
    t0 = time.time()
    # Retry the whole tile with backoff: GDAL retries single requests, but a
    # tile can still fail when the server sheds load.
    for attempt in range(1, attempts + 1):
        try:
            df = extract_from_tile(
                tile, poly, id_col="poly_id", all_touched=all_touched
            )
            break
        except Exception:  # noqa: BLE001
            if attempt == attempts:
                raise
            time.sleep(5 * 2 ** (attempt - 1))
    for c in BANDS:
        if c in df.columns:
            df[c] = df[c].astype("float32")
    tmp = out.with_suffix(".part")
    df.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(out)
    return out.name, len(df), time.time() - t0


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=int, nargs="*", default=list(AEF_YEARS))
    ap.add_argument("--forest-type", choices=["olivinskog", "kalklindeskog"])
    ap.add_argument(
        "--workers",
        type=int,
        default=6,
        help="concurrent tiles. Above ~8 the Cloudflare front end "
        "starts shedding requests (520/522/525); the retry "
        "absorbs that but throughput stops improving.",
    )
    ap.add_argument(
        "--all-touched",
        action="store_true",
        help="include pixels a polygon merely clips, not just those "
        "whose centre it contains (raises edge contamination)",
    )
    ap.add_argument("--limit", type=int, help="stop after N tiles (smoke test)")
    args = ap.parse_args(argv)

    SHARDS.mkdir(parents=True, exist_ok=True)
    poly = gpd.read_parquet(POLY_GEO)
    if args.forest_type:
        poly = poly[poly.forest_type == args.forest_type]
    poly = poly[["poly_id", "geometry"]]

    tiles = tiles_for_geometries(poly, args.years)
    if args.limit:
        tiles = tiles.head(args.limit)
    todo = [t for t in tiles.itertuples() if not shard_path(t).exists()]
    print(
        f"{len(poly)} polygons | {len(tiles)} (tile, year) shards, "
        f"{len(todo)} to fetch, {len(tiles) - len(todo)} cached"
    )

    t0 = time.time()
    done = 0
    failed: list[tuple[int, str, str]] = []
    if todo:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(run_tile, t, poly, args.all_touched): t for t in todo}
            for f in as_completed(futs):
                t = futs[f]
                try:
                    name, n, dt = f.result()
                except Exception as exc:  # noqa: BLE001 - one bad tile must not sink the run
                    failed.append(
                        (int(t.year), t.tile_id, f"{type(exc).__name__}: {exc}")
                    )
                    print(f"  ! {t.year} {t.tile_id}: {type(exc).__name__}: {exc}")
                    continue
                done += 1
                print(
                    f"  [{done}/{len(todo)}] {name} {n:>7} px {dt:5.1f}s "
                    f"({(time.time() - t0) / 60:.1f} min elapsed)",
                    flush=True,
                )

    if failed:
        print(
            f"\n{len(failed)} tiles failed after retries and wrote NO shard, so a "
            "re-run will pick them up:"
        )
        for yr, tid, err in failed:
            print(f"  {yr} {tid}: {err}")

    shards = sorted(SHARDS.glob("*.parquet"))
    if not shards:
        raise SystemExit("no shards written")
    df = pd.concat((pd.read_parquet(p) for p in shards), ignore_index=True)
    # A polygon on a tile seam is read from both tiles.
    df = df.drop_duplicates(subset=["poly_id", "year", "x", "y"])
    df.to_parquet(POLY_PIXELS, index=False, compression="zstd")

    print(
        f"\n{len(df):,} pixel-years for {df.poly_id.nunique()} polygons "
        f"-> {POLY_PIXELS}"
    )
    print(df.groupby("year").size().to_string())
    covered = df.groupby("poly_id")["year"].nunique()
    print(
        f"\nyears per polygon: median {covered.median():.0f}, "
        f"{int((covered < len(args.years)).sum())} polygons have gaps"
    )


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("02", sys.argv[1:])
