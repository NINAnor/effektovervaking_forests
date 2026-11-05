"""Step 2d -- download the AlphaEarth embeddings of the transformed bad pool.

Reads only the blocks `02c_plan_bad_pool.py` selected (54, not the 329 the rings
touch) and cuts the planned pixels out of each. The unit of work and of resume
is one (block, year) shard, as in step 2.

The pixels are thinned per stand first: a stand whose ring contains 40,000
built-up pixels does not need 40,000 references, and the ones it does need
should be spread over the ring rather than concentrated wherever the town is
densest. `--max-per-stand` draws that sample once, reproducibly, and the union
of the retained pixels is what gets read. Thinning does not reduce the download
-- the cost is the block, not the pixel -- but it keeps the reference table and
every distance matrix built from it an order of magnitude smaller.

Output: `bad_pixels.parquet` (pixel_id, year, A00..A63 -- same schema as the
stand pixels) and `bad_membership.parquet` (which stands each pixel may serve).

Run:  forestmon condition step 02d
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from effektovervaking_forests.condition.aef import (
    Block,
    open_tile,
)
from effektovervaking_forests.condition.paths import (
    AEF_YEARS,
    BAD_MEMBERSHIP,
    BAD_PIXELS,
    BAD_POOL_PLAN,
    BANDS,
    CACHE,
    OUT_DATA,
)

BAD_CANDIDATES = OUT_DATA / "bad_candidates.parquet"
BAD_SITES = OUT_DATA / "bad_sites.parquet"
SHARD_ROOT = CACHE / "bad_pixels"


def fingerprint(sites: pd.DataFrame) -> str:
    """Short digest of the exact pixel set a run asks for.

    The shards are keyed by (block, year) but hold `pixel_id`, a surrogate
    assigned per run. Re-running with a different --max-per-stand or --seed
    therefore asks for a different pixel set while the shard-exists check
    happily reuses the old one, silently pairing this run's ids with the last
    run's embeddings. Keying the cache directory on the pixel set makes that
    impossible: a changed request is a different directory and is simply
    re-read.
    """
    key = sites[["utm_zone", "x", "y"]].to_csv(index=False).encode()
    return hashlib.blake2b(key, digest_size=6).hexdigest()


def thin(cand: pd.DataFrame, max_per_stand: int, seed: int) -> pd.DataFrame:
    """Draw at most `max_per_stand` reference pixels per stand, at random.

    Random rather than nearest-first on purpose: the bad pool should describe
    "transformed land in this region", and taking the closest pixels would
    describe "the nearest edge of the nearest town", which is a much narrower
    claim and one already partly captured by the inner radius.
    """
    rng = np.random.default_rng(seed)
    keep = []
    for _, grp in cand.groupby("poly_id", sort=False):
        if len(grp) <= max_per_stand:
            keep.append(grp)
        else:
            keep.append(grp.iloc[rng.choice(len(grp), max_per_stand, replace=False)])
    return pd.concat(keep, ignore_index=True)


def shard_path(shards: Path, blk: dict, year: int) -> Path:
    return shards / (
        f"{year}_{blk['utm_zone']}_{int(blk['utm_west'])}_"
        f"{int(blk['utm_south'])}_{blk['brow']}_{blk['bcol']}.parquet"
    )


def run_shard(
    shards: Path,
    blk: dict,
    year: int,
    sites: pd.DataFrame,
    attempts: int = 3,
):
    """Cut the planned pixels of one block out of one annual tile."""
    out = shard_path(shards, blk, year)
    if out.exists():
        return out.name, -1, 0.0
    here = sites[
        (sites.utm_zone == blk["utm_zone"])
        & (sites.x >= blk["w"])
        & (sites.x < blk["e"])
        & (sites.y >= blk["s"])
        & (sites.y < blk["n"])
    ]
    if here.empty:
        pd.DataFrame(columns=["pixel_id", "year", *BANDS]).to_parquet(out, index=False)
        return out.name, 0, 0.0

    t0 = time.time()
    for attempt in range(1, attempts + 1):
        try:
            with open_tile(blk) as src:
                arr, transform = src.read_block(
                    Block(int(blk["brow"]), int(blk["bcol"]))
                )
            break
        except Exception:  # noqa: BLE001 - one bad block must not sink the run
            if attempt == attempts:
                raise
            time.sleep(5 * 2 ** (attempt - 1))

    # `_read_block` returns a north-up affine for the block, so the pixel
    # lookup is plain index arithmetic against it -- no resampling, and the
    # coordinates are the same ones step 2c chose.
    inv = ~transform
    c = np.floor(inv.a * here.x.to_numpy() + inv.c).astype(int)
    r = np.floor(inv.e * here.y.to_numpy() + inv.f).astype(int)
    ok = (r >= 0) & (r < arr.shape[1]) & (c >= 0) & (c < arr.shape[2])
    vals = arr[:, r[ok], c[ok]]
    finite = np.isfinite(vals[0])
    df = pd.DataFrame(vals[:, finite].T.astype("float32"), columns=list(BANDS))
    df.insert(0, "year", year)
    df.insert(0, "pixel_id", here.pixel_id.to_numpy()[ok][finite])
    tmp = out.with_suffix(".part")
    df.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(out)
    return out.name, len(df), time.time() - t0


def resolve_uris(plan: pd.DataFrame, years: list[int]) -> pd.DataFrame:
    """Attach the per-year GDAL URI to every planned block.

    The mirror re-hashes each tile's filename every year, so a tile is
    identified across years by (utm_zone, utm_west, utm_south) -- its origin --
    and never by the filename step 2c happened to see.
    """
    import duckdb

    from effektovervaking_forests.condition.aef import ensure_index, tile_uri
    from effektovervaking_forests.condition.paths import AEF_INDEX

    ensure_index(AEF_INDEX)
    yrs = ", ".join(str(int(y)) for y in years)
    idx = (
        duckdb.connect()
        .execute(
            f"""SELECT path, year, utm_zone, utm_west, utm_south, utm_east, utm_north
            FROM read_parquet('{AEF_INDEX}') WHERE year IN ({yrs})"""
        )
        .df()
    )
    idx["uri"] = idx["path"].map(tile_uri)
    out = plan.merge(idx, on=["utm_zone", "utm_west", "utm_south"], how="left")
    missing = out["uri"].isna()
    if missing.any():
        print(
            f"  ! {int(missing.sum())} (block, year) pairs have no tile in the "
            "index and are skipped"
        )
    return out[~missing]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=int, nargs="*", default=list(AEF_YEARS))
    ap.add_argument(
        "--max-per-stand",
        type=int,
        default=400,
        help="reference pixels retained per stand. The scorer caps "
        "the pool below this again; the margin is so that "
        "dropping pixels a stand overlaps still leaves a pool.",
    )
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    plan = pd.read_parquet(BAD_POOL_PLAN)
    cand = pd.read_parquet(BAD_CANDIDATES)
    print(f"{len(plan)} blocks, {len(cand):,} candidate (pixel, stand) pairs")

    cand = thin(cand, args.max_per_stand, args.seed)
    # Sorted, so the id a pixel gets depends only on which pixels were selected
    # and not on row order; negative, so a bad reference can never be confused
    # with a stand pixel if the two tables are ever concatenated by mistake.
    sites = (
        cand[["utm_zone", "x", "y"]]
        .drop_duplicates()
        .sort_values(["utm_zone", "x", "y"], kind="stable")
        .reset_index(drop=True)
    )
    sites.insert(0, "pixel_id", -(np.arange(len(sites), dtype=np.int32) + 1))
    cand = cand.merge(sites, on=["utm_zone", "x", "y"], how="left")
    sites.to_parquet(BAD_SITES, index=False)

    shards_dir = SHARD_ROOT / fingerprint(sites)
    shards_dir.mkdir(parents=True, exist_ok=True)
    cand[["pixel_id", "poly_id", "wc_class", "outer_m"]].to_parquet(
        BAD_MEMBERSHIP, index=False, compression="zstd"
    )
    print(
        f"thinned to {len(cand):,} pairs over {len(sites):,} distinct pixels "
        f"(<= {args.max_per_stand} per stand)"
    )
    print(f"shard cache: {shards_dir.relative_to(CACHE.parent)}")

    work = resolve_uris(plan, args.years)
    todo = [
        row
        for row in work.to_dict("records")
        if not shard_path(shards_dir, row, int(row["year"])).exists()
    ]
    print(
        f"{len(work)} (block, year) shards, {len(todo)} to fetch, "
        f"{len(work) - len(todo)} cached"
    )

    t0, done = time.time(), 0
    if todo:
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {
                ex.submit(
                    run_shard,
                    shards_dir,
                    b,
                    int(b["year"]),
                    sites,
                ): b
                for b in todo
            }
            for f in as_completed(futs):
                b = futs[f]
                try:
                    name, n, dt = f.result()
                except Exception as exc:  # noqa: BLE001 - a lost block is a
                    # hole in one region's bad pool, not a reason to lose the run
                    print(
                        f"  ! {b['year']} {b['utm_zone']} {b['brow']},{b['bcol']}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                done += 1
                print(
                    f"  [{done}/{len(todo)}] {name} {n:>6} px {dt:5.1f}s "
                    f"({(time.time() - t0) / 60:.1f} min)",
                    flush=True,
                )

    shards = sorted(shards_dir.glob("*.parquet"))
    df = pd.concat((pd.read_parquet(p) for p in shards), ignore_index=True)
    df = df.drop_duplicates(subset=["pixel_id", "year"])
    df.to_parquet(BAD_PIXELS, index=False, compression="zstd")
    print(
        f"\n{len(df):,} bad pixel-years for {df.pixel_id.nunique():,} pixels "
        f"-> {BAD_PIXELS.name}"
    )
    print(df.groupby("year").size().to_string())
    got = cand.merge(df[["pixel_id"]].drop_duplicates(), on="pixel_id")
    per = got.groupby("poly_id").size()
    print(
        f"\nbad pool per stand after the read: median {per.median():.0f}, "
        f"min {per.min() if len(per) else 0}, "
        f"{len(cand.poly_id.unique()) - len(per)} stands lost their whole pool "
        "to nodata"
    )


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("02d", sys.argv[1:])
