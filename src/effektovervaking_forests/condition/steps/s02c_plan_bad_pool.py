"""Step 2c -- choose the AlphaEarth blocks the transformed bad pool needs.

The bad pool is cropland and built-up pixels in a 3-8 km ring around each stand
(`condition/worldcover.py`). Taking that literally -- every transformed pixel in
every ring -- is unaffordable: the rings touch 329 AlphaEarth blocks per year
against the 87 the stands themselves need, and a block costs ~40 MB and 6-10 s
whether you read one pixel of it or a million. Naively that is ~118 GB.

But no stand needs its whole ring; it needs a few hundred reference pixels from
it. So which blocks get read is a **set-cover problem**, and solving it is the
difference between 118 GB and a run measured in minutes:

    choose the fewest blocks such that every stand has at least
    `--min-bad` transformed pixels available inside its own annulus

Greedy, by marginal deficit reduction -- blocks near towns serve dozens of
stands at once, so the first few picks do most of the work. Stands still short
after the pass get their outer radius widened (8 -> 16 -> 32 -> 64 km) and the
search repeats, because an outer-coast olivinskog stand can have no cropland or
built-up within 8 km at all; the widened radius is recorded per stand rather
than hidden.

This step downloads no AlphaEarth data. It reads WorldCover (a few MB per
block, uint8) and writes the plan, so the cost of step 2d is known before it is
paid.

Outputs `bad_pool_plan.parquet` (blocks to read) and `bad_candidates.parquet`
(the chosen pixels and the stands each serves).

Run:  forestmon condition step 02c
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import geopandas as gpd
import numpy as np
import pandas as pd

from effektovervaking_forests.condition.aef import (
    blocks_for_geometries,
    tiles_for_geometries,
)
from effektovervaking_forests.condition.paths import (
    ANNULUS_INNER_M,
    ANNULUS_OUTER_M,
    BAD_POOL_PLAN,
    OUT_DATA,
    TRANSFORMED_CLASSES,
)
from effektovervaking_forests.condition.worldcover import annuli, transformed_in_block

POLY_GEO = OUT_DATA / "polygons_geo.parquet"
BAD_CANDIDATES = OUT_DATA / "bad_candidates.parquet"
MB_PER_BLOCK, SECONDS_PER_BLOCK = 40.0, 8.0


def candidate_blocks(ring: gpd.GeoDataFrame, year: int) -> pd.DataFrame:
    """(tile, block) pairs the rings touch, with the block's UTM bounds."""
    tiles = tiles_for_geometries(ring[["poly_id", "geometry"]], [year])
    rows = []
    for t in tiles.itertuples():
        td = t._asdict()
        local = ring.to_crs(t.crs)
        tb = (t.utm_west, t.utm_south, t.utm_east, t.utm_north)
        sub = local[
            local.geometry.apply(
                lambda g: (
                    not (
                        g.is_empty
                        or g.bounds[2] <= tb[0]
                        or g.bounds[0] >= tb[2]
                        or g.bounds[3] <= tb[1]
                        or g.bounds[1] >= tb[3]
                    )
                )
            )
        ]
        if sub.empty:
            continue
        for b in blocks_for_geometries(sub, td):
            w, s, e, n = b.bounds(td)
            # Keyed by the TILE ORIGIN, not by tile_id: the mirror re-hashes
            # every tile filename per year, so tile_id identifies a file, not a
            # place. (utm_zone, utm_west, utm_south) is what is stable, and it
            # is what step 2d joins the annual index back on.
            rows.append(
                {
                    "utm_zone": t.utm_zone,
                    "crs": t.crs,
                    "utm_west": t.utm_west,
                    "utm_south": t.utm_south,
                    "brow": b.brow,
                    "bcol": b.bcol,
                    "w": w,
                    "s": s,
                    "e": e,
                    "n": n,
                }
            )
    return pd.DataFrame(rows).drop_duplicates(
        subset=["utm_zone", "utm_west", "utm_south", "brow", "bcol"]
    )


def scan(
    blocks: pd.DataFrame,
    ring: gpd.GeoDataFrame,
    workers: int,
    max_points: int,
    seed: int,
) -> pd.DataFrame:
    """What each candidate block could contribute, per stand. WorldCover only."""

    def one(i, row):
        epsg = int(str(row.crs).split(":")[-1])
        df = transformed_in_block(
            (row.w, row.s, row.e, row.n),
            epsg,
            ring,
            classes=TRANSFORMED_CLASSES,
            max_points=max_points,
            rng=np.random.default_rng(seed + i),
        )
        if len(df):
            df["block"] = i
            df["utm_zone"] = row.utm_zone
        return df

    out, t0 = [], time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, i, r): i for i, r in enumerate(blocks.itertuples())}
        for done, f in enumerate(as_completed(futs), 1):
            try:
                df = f.result()
            except Exception as exc:  # noqa: BLE001 - one bad block must not sink the scan
                print(f"  ! block {futs[f]}: {type(exc).__name__}: {exc}")
                continue
            if len(df):
                out.append(df)
            if done % 25 == 0:
                print(
                    f"  scanned {done}/{len(blocks)} blocks "
                    f"({(time.time() - t0) / 60:.1f} min)",
                    flush=True,
                )
    return (
        pd.concat(out, ignore_index=True)
        if out
        else pd.DataFrame(
            columns=["x", "y", "poly_id", "wc_class", "block", "utm_zone"]
        )
    )


def greedy_cover(cand: pd.DataFrame, stands: list[str], min_bad: int) -> list[int]:
    """Fewest blocks such that every stand reaches `min_bad` candidate pixels.

    Marginal-gain greedy: the gain of a block is how much it reduces the *unmet*
    deficit, so a block offering a stand its 400th pixel when 150 are wanted
    counts for nothing. That is what keeps a city block from being picked twice
    over for stands it has already satisfied.
    """
    by_block: dict[int, dict[str, int]] = defaultdict(dict)
    for blk, grp in cand.groupby("block", sort=False):
        by_block[blk] = grp.groupby("poly_id").size().to_dict()

    need = {s: min_bad for s in stands}
    chosen: list[int] = []
    remaining = dict(by_block)
    while remaining:
        best, best_gain = None, 0
        for blk, contrib in remaining.items():
            gain = sum(min(v, need.get(p, 0)) for p, v in contrib.items())
            if gain > best_gain:
                best, best_gain = blk, gain
        if best is None or best_gain == 0:
            break
        for p, v in remaining.pop(best).items():
            if p in need:
                need[p] = max(0, need[p] - v)
        chosen.append(best)
    return chosen


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--min-bad",
        type=int,
        default=150,
        help="transformed pixels each stand must have available in "
        "its own annulus. The pool the scorer actually uses is "
        "capped at or below this; asking for more here only "
        "buys blocks.",
    )
    ap.add_argument("--inner-m", type=float, default=ANNULUS_INNER_M)
    ap.add_argument("--outer-m", type=float, default=ANNULUS_OUTER_M)
    ap.add_argument(
        "--expand",
        type=float,
        nargs="*",
        default=[16_000, 32_000, 64_000],
        help="outer radii to retry with, for stands with no "
        "transformed land inside the default ring",
    )
    ap.add_argument("--max-points-per-block", type=int, default=20_000)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument(
        "--year",
        type=int,
        default=2025,
        help="which annual tile set to resolve blocks against; the "
        "grid is identical across years, so this only picks "
        "which index rows are read",
    )
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    poly = gpd.read_parquet(POLY_GEO)
    outer = pd.Series(args.outer_m, index=poly["poly_id"])
    unmet = list(poly["poly_id"])
    all_cand, all_blocks = [], []

    for rnd, radius in enumerate([args.outer_m, *args.expand]):
        if not unmet:
            break
        if rnd:
            print(
                f"\n{len(unmet)} stands still short -- widening their ring to "
                f"{radius / 1000:g} km"
            )
            outer.loc[unmet] = radius
        sub = poly[poly["poly_id"].isin(unmet)]
        ring = annuli(
            sub, inner_m=args.inner_m, outer_m=radius, outer_scale=outer.loc[unmet]
        )
        blocks = candidate_blocks(ring, args.year)
        print(
            f"round {rnd}: {len(sub)} stands, ring {args.inner_m / 1000:g}-"
            f"{radius / 1000:g} km, {len(blocks)} candidate blocks to scan"
        )

        cand = scan(blocks, ring, args.workers, args.max_points_per_block, args.seed)
        if cand.empty:
            print("  no transformed pixels found anywhere in these rings")
            continue
        # Block ids are per-round; make them global before they are stored.
        offset = sum(len(b) for b in all_blocks)
        cand["block"] += offset
        keep = greedy_cover(cand, list(sub["poly_id"]), args.min_bad)
        cand = cand[cand["block"].isin(keep)]
        got = cand.groupby("poly_id").size()
        print(
            f"  {len(keep)} blocks cover {int((got >= args.min_bad).sum())} of "
            f"{len(sub)} stands to >={args.min_bad} pixels "
            f"(median {int(got.median()) if len(got) else 0} per stand)"
        )

        all_blocks.append(
            blocks.assign(block=np.arange(len(blocks)) + offset, round=rnd).loc[
                lambda d: d.block.isin(keep)
            ]
        )
        all_cand.append(cand)
        unmet = [p for p in sub["poly_id"] if got.get(p, 0) < args.min_bad]

    plan = pd.concat(all_blocks, ignore_index=True)
    cand = pd.concat(all_cand, ignore_index=True)
    cand = cand.merge(outer.rename("outer_m"), left_on="poly_id", right_index=True)
    plan.to_parquet(BAD_POOL_PLAN, index=False)
    cand.to_parquet(BAD_CANDIDATES, index=False, compression="zstd")

    px = cand[["utm_zone", "x", "y"]].drop_duplicates()
    print(f"\n{len(plan)} blocks -> {BAD_POOL_PLAN.name}")
    print(
        f"{len(cand):,} (pixel, stand) candidate pairs over {len(px):,} distinct "
        f"pixels -> {BAD_CANDIDATES.name}"
    )
    print("\nAlphaEarth cost, per year of bad references requested:")
    print(
        f"  {len(plan)} block reads  ~{len(plan) * MB_PER_BLOCK / 1000:.1f} GB  "
        f"~{len(plan) * SECONDS_PER_BLOCK / 60:.0f} min serial, "
        f"~{len(plan) * SECONDS_PER_BLOCK / 60 / 6:.0f} min at 6 workers"
    )
    per = cand.groupby("poly_id").size()
    print(
        f"\ncandidate pixels per stand: median {per.median():.0f}, "
        f"min {per.min()}, {int((per < args.min_bad).sum())} stands below "
        f"--min-bad, {len(poly) - len(per)} with none at all"
    )
    if unmet:
        print(
            f"  these {len(unmet)} stands have no transformed land even at "
            f"{max(args.expand) / 1000:g} km and will have no bad pool; they "
            "are scored as unscoreable rather than against a substitute"
        )
    print("\nouter radius actually used:")
    print(outer.value_counts().sort_index().to_string())


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("02c", sys.argv[1:])
