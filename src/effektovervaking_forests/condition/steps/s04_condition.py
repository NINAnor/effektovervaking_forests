"""Step 4 (= the user-facing STEP 2) -- per-pixel condition scoring.

The scoring maths is the published RECOVER one, vendored unchanged in
`condition/core.py`: k nearest neighbours in cosine distance to a good and a bad
reference pool, and a CI-vs-threshold rule that refuses to classify a site whose
interval straddles the threshold.

    dor = mean_cos(x -> k nearest bad refs)
        / (mean_cos(x -> k nearest good refs) + mean_cos(x -> k nearest bad refs))

Six things are re-defined for this project. Each has its own module docstring;
in short:

  * **the probe is a pixel** (`condition/score.py`). A stand is not in one
    condition, and the polygon mean was averaging a clearcut in one corner
    together with intact forest in the other. The stand score is the MEDIAN of
    its pixel scores, with a donor-clustered bootstrap interval on that median
    (`score.median_cluster_ci`); the old mean-embedding score is kept as
    `dor_<scope>_probe` (`--stand-score probe` restores it as the score).
  * **the good pool comes from that forest type, at two scopes**
    (`condition/refs.py`). `local` = the nearest god-labelled stands, so the
    comparison is regionally matched; `global` = all of them, so scores are
    comparable AOI-wide. Both are reported for every pixel; they disagree
    exactly where a stand is unusual for its region but not for the type.
  * **labels are anchored to their survey year**. `tilstand` describes the day
    the surveyor stood there, and these labels span 2015-2025, so a donor
    contributes only its survey-year pixels and one surveyed before 2017 cannot
    contribute at all.
  * **the bad pool is transformed land from WorldCover**
    (`condition/worldcover.py`), cropland and built-up in a 3-8 km ring around
    each stand, contemporaneous with the probe year. Substituting the 36
    field-labelled bad stands for it was tried and costs almost all of the
    separation -- see `docs/METHODS.md`.
  * **the distances are taken in a shrunk-whitened metric**
    (`condition/whiten.py`), not in the raw embedding. A handful of AlphaEarth
    directions carry most of the terrain and phenology variance over this AOI
    and swamp the condition contrast in a plain cosine distance.
  * **a probe never sees a donor from its own 5 km spatial block**
    (`condition/blocks.py`), and the stand interval resamples donor stands
    rather than reference pixels. Both were leaks: together they were worth
    about a third of the reported stand-grain separation and about one stand in
    eight of the condition classifications.

Overlap is handled throughout by scoring pixels, not polygons: the layers share
24% of their area, so a pool excluded by `poly_id` still contains the probe's
own ground under a neighbour's id (`condition/membership.py`).

The defaults here are the configuration validated in `condition/research/`:
whitening at shrinkage 0.2, k = 25, leave-block-out pools and thresholds,
donor-clustered intervals (E01-E14, five seeds), the transformed pool pooled per
forest type (E38, corroborated on independent labels in E26), cleared donor
pixels dropped (E25), and a 32-stand local pool (E44, ten seeds). Pass
`--legacy` to reproduce the pre-research numbers instead.

Outputs `pixel_condition.parquet` (per pixel-year), `condition.parquet` (per
stand-year), `threshold.json` and `whitener.json`.

Run:  forestmon condition step 04
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import numpy as np
import pandas as pd

from effektovervaking_forests.condition.blocks import INTERIOR_M_DEFAULT
from effektovervaking_forests.condition.core import cosine_dists_to_set, knn_score
from effektovervaking_forests.condition.paths import (
    AEF_YEARS,
    BAD_MEMBERSHIP,
    BAD_PIXELS,
    BANDS,
    CONDITION,
    OUT_DATA,
    PIXEL_CONDITION,
    PIXEL_POLYGONS,
    PIXELS,
    POLY_OVERLAP,
    POLYGONS,
)
from effektovervaking_forests.condition.refs import (
    EXCLUDE_KM_DEFAULT,
    PoolBuilder,
    anchor_donors,
    survey_year,
)
from effektovervaking_forests.condition.score import (
    EPS,
    bad_side,
    calibrate_threshold_blocked,
    classify_condition,
    knn_mean_unit,
    median_cluster_ci,
    score_stand_year,
    stand_probe,
    summarise_pixels,
    threshold_interval_blocked,
    unit_rows,
)
from effektovervaking_forests.condition.whiten import SHRINK_DEFAULT, fit_whitener

THRESHOLD_JSON = OUT_DATA / "threshold.json"
WHITENER_JSON = OUT_DATA / "whitener.json"
BAD_CLUSTER_M = 500.0  # locality size for the transformed pool's bootstrap unit
POLY_GEO = OUT_DATA / "polygons_geo.parquet"
SITES = OUT_DATA / "pixel_sites.parquet"
ROLES = OUT_DATA / "pixel_roles.parquet"
SCOPES = ("local", "global")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--years",
        type=int,
        nargs="*",
        default=list(AEF_YEARS),
        help="probe years to score (default: the whole record, so "
        "the output is a condition time series, not a snapshot)",
    )
    ap.add_argument(
        "--k",
        type=int,
        default=25,
        help="neighbours averaged per pool. 5 is the published "
        "value; at 25 the score depends less on whichever "
        "single donor stand happens to be nearest, which "
        "narrows the donor-resampling interval by a third at "
        "no cost in separation (research E05, E11)",
    )
    ap.add_argument(
        "--shrink",
        type=float,
        default=SHRINK_DEFAULT,
        help="covariance shrinkage for the whitened metric; 1.0 is "
        "no whitening. 0.0 ranks best and decides worst and "
        "breaks the held-out ordering (research E12)",
    )
    ap.add_argument(
        "--no-whiten",
        action="store_true",
        help="plain cosine on the raw embedding, as published",
    )
    ap.add_argument(
        "--exclude-km",
        type=float,
        default=EXCLUDE_KM_DEFAULT,
        help="a god stand whose centroid is within this of the probe "
        "cannot be a donor. The default 3.0 is the transformed "
        "pool's own annulus inner edge, so both pools obey one "
        "rule; research E20 measured 0/1/2/3/5 km and a whole "
        "spatial block at this estimator and 3 km won on stand "
        "AUC, pixel AUC, olivinskog and Youden J, in both scopes",
    )
    ap.add_argument(
        "--exclude-block",
        action="store_true",
        help="exclude the probe's whole 5 km spatial block from the "
        "pool instead of an inner radius. This is what the "
        "pipeline shipped before E20; it removes contamination "
        "and regional matching together and costs the local "
        "scope most (stand 0.789 -> 0.750, pixel 0.769 -> 0.697)",
    )
    ap.add_argument(
        "--no-exclude-block",
        action="store_true",
        help="no spatial exclusion on the good pool at all: a donor "
        "may be the stand next door. Pre-research behaviour, "
        "and it inflates the stand-grain separation",
    )
    ap.add_argument(
        "--bad-pools",
        default="converted",
        help="which land makes the transformed end-member: a comma "
        "list of converted,cleared,grass,shrub,bare,wetland,moss. "
        "The default `converted` is WorldCover cropland+built-up, "
        "the shipped definition, read from the set-cover extract. "
        "Anything else reads bad_pixels_v2.parquet from "
        "02f_bad_pool_variants.py --all-stands. Research E15/E16: "
        "open vegetation separates better WITHIN a locality and "
        "worse across localities, and the two forest types "
        "disagree about it, so this is not a default to change "
        "lightly.",
    )
    ap.add_argument(
        "--annulus-bad",
        action="store_true",
        help="score against each stand's OWN 3-8 km transformed "
        "annulus instead of the annuli pooled per forest type. "
        "This is the pre-E38 behaviour. Pooling is the default "
        "because it is the change that moved the error-side "
        "numbers: at the deployed local scope, three seeds, "
        "degraded recall 0.533 -> 0.581 and degraded stands "
        "wrongly called good 0.105 -> 0.038 (research E38), "
        "corroborated on independent Hansen-cleared labels "
        "(E26). The per-stand annulus asks each probe a "
        "different question, because its transformed "
        "end-member is whatever land happens to surround it",
    )
    ap.add_argument(
        "--max-type-bad",
        type=int,
        default=2000,
        help="cap on the pooled transformed cloud, per forest type and year",
    )
    ap.add_argument(
        "--keep-cleared-donors",
        action="store_true",
        help="leave pixels Hansen records as cleared at or before "
        "their donor's survey year in the good pool. They are "
        "dropped by default: a clearcut in the good pool "
        "flatters exactly the probes that look like clearings. "
        "The measured cost is nil (stand AUC 0.8051 -> 0.8056, "
        "inside seed noise) but it is the state E26's "
        "independent corroboration was run in (research E25)",
    )
    ap.add_argument(
        "--legacy",
        action="store_true",
        help="reproduce the pre-research configuration exactly: "
        "k=5, no whitening, no block exclusion, row-wise "
        "bootstrap, pooled threshold",
    )
    ap.add_argument(
        "--n-donors",
        type=int,
        default=32,
        help="how many nearest god-labelled stands form the LOCAL "
        "pool. Was 8 until research E44. The case for 8 was "
        "that its degraded recall was the steadiest of the "
        "candidate pools, but that rested on three seeds; at "
        "ten it is 0.540 +-0.052 against 32's 0.563 +-0.065 "
        "-- the same, within estimation error. What separates "
        "them is within-locality AUC, 0.685 +-0.008 against "
        "0.758 +-0.005, about nine seed sd and the steadiest "
        "number in that table. Since telling condition apart "
        "WITHIN a locality is this project's binding "
        "limitation, that is the axis to buy. The cost is 0.6 "
        "more wrongly reassured degraded stands out of 35, "
        "which is inside the seed spread; the 20 km radius "
        "pool ranks better still but its +3.1 is not",
    )
    ap.add_argument(
        "--donor-km",
        type=float,
        default=0.0,
        help="draw the LOCAL pool by radius instead of by count: "
        "every eligible donor within this many km, with "
        "--n-donors as the floor where the radius is empty. "
        "0 keeps the nearest-n rule. Research E44/E59 rank "
        "20 km above every count pool on every decision axis "
        "(degraded stands found 20.0 -> 23.5 of 35, wrongly "
        "reassured 5.2 -> 3.9, stand AUC 0.789 -> 0.836) and "
        "E59 shows the sweep turning over at 20 km, so it is "
        "an interior maximum rather than the edge of what was "
        "tried. The gain is almost entirely olivinskog, whose "
        "median stand has six eligible donors within 3-20 km "
        "and is therefore handed 20-50 km references by the "
        "count rule (research E60)",
    )
    ap.add_argument(
        "--pool-by-type",
        default="olivinskog:8:20",
        help="per-forest-type LOCAL pool rules as "
        "`type:n_donors:km` entries, comma separated; a type "
        "not named here uses --n-donors/--donor-km. This is "
        "what separates v2 from v1. The two types differ in "
        "donor DENSITY, not ecology: a kalklindeskog stand has "
        "a median of 54 eligible donors within 3-20 km and an "
        "olivinskog stand has six, so one nearest-32 rule "
        "hands olivinskog references from 20-50 km away. "
        "Research E60: olivinskog degraded stands found 4.3 -> "
        "8.2 of 12, wrongly reassured 4.0 -> 0.9, with "
        "kalklindeskog left exactly as v1 scored it. Pass an "
        "empty string for the v1 single rule",
    )
    ap.add_argument(
        "--max-km",
        type=float,
        default=50.0,
        help="a donor further than this is not local to the probe; "
        "stands with no donor inside it get no local score "
        "rather than a mislabelled one",
    )
    ap.add_argument(
        "--max-per-donor",
        type=int,
        default=60,
        help="pixels taken per donor stand, so a 40 ha stand does "
        "not outvote thirty small ones",
    )
    ap.add_argument("--max-global", type=int, default=2000)
    ap.add_argument("--max-bad", type=int, default=300)
    ap.add_argument(
        "--stand-score",
        choices=("median", "probe"),
        default="median",
        help="`median`: the stand score is the median of its cell "
        "scores, its interval the cluster bootstrap of that "
        "median (one draw shared by every cell). `probe`: the "
        "score of the stand's mean embedding, as shipped until "
        "2026-10. The two separate god from poor stands "
        "equally well (AUC 0.836 vs 0.829); the median is the "
        "number the cell map can be checked against",
    )
    ap.add_argument(
        "--n-boot",
        type=int,
        default=2000,
        help="bootstrap resamples for the STAND-level CI. Per-pixel "
        "scores carry no CI on purpose -- see condition/score.py",
    )
    ap.add_argument(
        "--thr-boot",
        type=int,
        default=300,
        help="block-bootstrap resamples for the THRESHOLD interval, "
        "reported as threshold_se_* and in threshold.json. "
        "It is a diagnostic: research E43/E45 showed it is the "
        "label-sample half of the boundary's error only, and "
        "that classifying with it costs 40% of the degraded "
        "stands found for no fall in wrong reassurance",
    )
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--boot-rng",
        choices=("shared", "per-stand"),
        default="shared",
        help="`shared`: one generator consumed in stand order, "
        "bit-identical to earlier runs. `per-stand`: seeded "
        "from (seed, stand), so a stand's CI no longer depends "
        "on which stands were scored before it and stands can "
        "run in parallel",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=1,
        help="threads scoring stands in parallel; >1 needs --boot-rng per-stand",
    )
    ap.add_argument(
        "--boot-draw",
        choices=("full", "thinned"),
        default="full",
        help="`full`: every resample draws all its clusters, "
        "bit-identical to earlier runs. `thinned`: draws only "
        "how many land on the clusters that can matter and "
        "which -- the same resample distribution from a "
        "different stream, ~3.5x cheaper; CIs move by "
        "bootstrap noise only (score._boot_knn_mean_thinned)",
    )
    ap.add_argument(
        "--parallel",
        choices=("thread", "process"),
        default="thread",
        help="how --workers > 1 runs: threads in this process, or "
        "spawned processes that memory-map the embeddings "
        "(no GIL; writes them to outputs/data/_04_mmap/ first)",
    )
    ap.add_argument(
        "--batch-small",
        type=int,
        default=0,
        help="score stands of at most this many pixels in batches "
        "per forest type and year, sharing one transformed-"
        "pool matmul (needs --boot-rng per-stand). 0 = off",
    )
    ap.add_argument(
        "--batch-size", type=int, default=64, help="stands per --batch-small batch"
    )
    args = ap.parse_args(argv)
    pool_by_type = {}
    for ent in (e.strip() for e in args.pool_by_type.split(",")):
        if not ent:
            continue
        try:
            ft, n, km = ent.split(":")
            pool_by_type[ft] = (int(n), float(km))
        except ValueError:
            raise SystemExit(
                f"--pool-by-type: cannot read {ent!r}; want type:n_donors:km, "
                "e.g. olivinskog:8:20"
            )

    if args.legacy:
        args.k, args.no_whiten, args.no_exclude_block = 5, True, True
        # every reference choice back to its pre-research value too, or
        # `--legacy` would reproduce the old estimator against new pools
        args.annulus_bad, args.keep_cleared_donors = True, True
        args.n_donors, args.donor_km = 8, 0.0
        args.stand_score = "probe"
        pool_by_type = {}
    if args.no_exclude_block:
        args.exclude_km, args.exclude_block = 0.0, False

    t0 = time.time()
    polygons = pd.read_parquet(POLYGONS)
    poly_geo = gpd.read_parquet(POLY_GEO)
    pixels = pd.read_parquet(PIXELS)
    membership = pd.read_parquet(PIXEL_POLYGONS)
    roles = pd.read_parquet(ROLES)
    overlap = pd.read_parquet(POLY_OVERLAP)
    bad_pools = tuple(p.strip() for p in args.bad_pools.split(",") if p.strip())
    if bad_pools == ("converted",):
        bad_pixels = pd.read_parquet(BAD_PIXELS)
        bad_membership = pd.read_parquet(BAD_MEMBERSHIP)
        bad_sites_path = OUT_DATA / "bad_sites.parquet"
    else:
        v2p, v2m = (
            OUT_DATA / "bad_pixels_v2.parquet",
            OUT_DATA / "bad_membership_v2.parquet",
        )
        if not v2p.exists():
            raise SystemExit(
                f"--bad-pools {args.bad_pools} needs {v2p.name}; run\n"
                "  02f_bad_pool_variants.py --all-stands"
            )
        bad_pixels = pd.read_parquet(v2p)
        bad_membership = pd.read_parquet(v2m)
        known = set(bad_membership["pool"].unique())
        unknown = set(bad_pools) - known
        if unknown:
            raise SystemExit(f"unknown pool(s) {sorted(unknown)}; have {sorted(known)}")
        bad_membership = bad_membership[bad_membership["pool"].isin(bad_pools)]
        bad_pixels = bad_pixels[
            bad_pixels["pixel_id"].isin(set(bad_membership["pixel_id"]))
        ]
        bad_sites_path = OUT_DATA / "bad_sites_v2.parquet"
        print(
            f"transformed pool = {'+'.join(bad_pools)}: "
            f"{len(bad_pixels):,} pixels over "
            f"{bad_membership['poly_id'].nunique()} stands"
        )

    polygons["survey_year"] = survey_year(polygons)
    if "block" not in polygons.columns:
        raise SystemExit(
            "polygons.parquet has no `block` column -- re-run "
            "00_build_polygons.py, which assigns the spatial blocks that the "
            "reference pools and the threshold are held out by"
        )
    blocks = polygons.set_index("poly_id")["block"]
    cent = pd.DataFrame(
        {
            "poly_id": poly_geo["poly_id"],
            "cx": poly_geo.geometry.centroid.x,
            "cy": poly_geo.geometry.centroid.y,
        }
    )

    ftype_all = polygons.set_index("poly_id")["forest_type"]
    donors = anchor_donors(polygons, roles, membership, tuple(AEF_YEARS))
    lab = polygons[polygons["ref_role"] == "good"]
    print(f"{len(lab)} god-labelled stands; {len(donors)} usable as references")
    stale = int((~lab["survey_year"].isin(AEF_YEARS)).sum())
    print(
        f"  {stale} dropped: surveyed outside {min(AEF_YEARS)}-{max(AEF_YEARS)}, "
        "so their label cannot be anchored to an embedding"
    )
    print(
        f"  {len(lab) - stale - len(donors)} dropped: too few unambiguously "
        "good pixels left after resolving layers that disagree"
    )
    print(
        donors.groupby("forest_type")
        .agg(n=("poly_id", "size"), px=("n_good_pixels", "sum"))
        .to_string()
    )

    loss_year = {}
    if not args.keep_cleared_donors:
        hp = OUT_DATA / "hansen_pixels.parquet"
        if not hp.exists():
            raise SystemExit(
                f"the good-pool cleanup needs {hp.name}; run\n"
                "  02e_hansen_reference.py\n"
                "or pass --keep-cleared-donors to score without it"
            )
        h = pd.read_parquet(hp, columns=["pixel_id", "loss_year"])
        ly = pd.to_numeric(h["loss_year"], errors="coerce").fillna(0).astype(int)
        loss_year = {int(p): int(v) for p, v in zip(h["pixel_id"], ly) if v > 0}

    builder = PoolBuilder(
        pixels=pixels,
        bad_pixels=bad_pixels,
        donors=donors,
        membership=membership,
        pixel_roles=roles,
        bad_membership=bad_membership,
        neighbours=overlap,
        centroids=cent,
        blocks=blocks,
        poly_type=ftype_all,
        exclude_km=args.exclude_km,
        exclude_block=args.exclude_block,
        n_donors=args.n_donors,
        donor_km=args.donor_km,
        pool_by_type=pool_by_type,
        max_km=args.max_km,
        max_per_donor=args.max_per_donor,
        max_global=args.max_global,
        max_bad=args.max_bad,
        seed=args.seed,
        pooled_bad=not args.annulus_bad,
        max_type_bad=args.max_type_bad,
        loss_year=loss_year,
    )
    if loss_year:
        print(
            f"  good pool: dropped {builder.n_cleared_dropped:,} pixels across "
            f"{builder.n_donors_cleared} donors that Hansen records as cleared "
            "at or before their survey year (research E25)"
        )
    print(f"\npools built in {time.time() - t0:.0f}s")
    print(builder.sizes().to_string(index=False))
    if args.no_exclude_block:
        print(
            "  ! the good pool has NO spatial exclusion; the separation this "
            "run reports is inflated (research E01)"
        )
    elif args.exclude_block:
        print(
            "  ! pool exclusion is the whole spatial block, not the "
            f"{args.exclude_km:g} km radius; this is the pre-E20 "
            "configuration and it scores lower on both grains"
        )
    else:
        print(
            f"  good pool excludes donors within {args.exclude_km:g} km "
            "(= the transformed annulus inner edge); the threshold and the "
            "intervals still hold out whole spatial blocks"
        )
    if args.donor_km > 0:
        print(
            f"  local pool drawn by RADIUS: every donor within "
            f"{args.donor_km:g} km, floor {args.n_donors} where that radius "
            "is empty (research E44/E59)"
        )
    for ft, (n, km) in sorted(pool_by_type.items()):
        print(
            f"  local pool for {ft}: "
            + (
                f"every donor within {km:g} km, floor {n}"
                if km > 0
                else f"the nearest {n}"
            )
            + " (research E60)"
        )

    # ---- the metric --------------------------------------------------------
    # Fitted per forest type on UNLABELLED pixels only, so the class means the
    # score then measures distance to have no say in the metric it is measured
    # with. See condition/whiten.py.
    whit = {}
    if not args.no_whiten:
        unlabelled = set(roles.loc[roles["role"] == "unlabelled", "pixel_id"])
        for ft in sorted(polygons["forest_type"].dropna().unique()):
            pid = set(roles.loc[roles["forest_type"] == ft, "pixel_id"]) & unlabelled
            X = pixels.loc[pixels["pixel_id"].isin(pid), list(BANDS)].to_numpy(
                dtype=np.float32
            )
            if len(X) < 1000:
                print(
                    f"  ! {ft}: only {len(X)} unlabelled pixel-years, too few "
                    "to fit a metric on; scoring it with plain cosine"
                )
                continue
            whit[ft] = fit_whitener(X, shrink=args.shrink, seed=args.seed)
            print(
                f"  metric {ft:<14} shrink={args.shrink:g} fitted on "
                f"{whit[ft].n_fit:,} unlabelled pixel-years"
            )
        WHITENER_JSON.write_text(
            json.dumps({ft: w.to_dict() for ft, w in whit.items()})
        )

    # Locality of every transformed reference pixel, so the stand interval can
    # resample localities rather than pixels: a 500 m cell of cropland is one
    # observation of "what converted land looks like here", not 300.
    bad_sites = pd.read_parquet(bad_sites_path)
    if {"x", "y", "utm_zone"} <= set(bad_sites.columns):
        cell = (
            (bad_sites["x"] // BAD_CLUSTER_M).astype("int64").astype(str)
            + "_"
            + (bad_sites["y"] // BAD_CLUSTER_M).astype("int64").astype(str)
            + "_"
            + bad_sites["utm_zone"].astype(str)
        )
    else:
        # v2 sites are lon/lat; a degree cell is latitude-corrected so that a
        # locality is the same size in metres at 58 N and at 70 N.
        deg_lat = BAD_CLUSTER_M / 111_320.0
        deg_lon = deg_lat / np.cos(np.radians(bad_sites["lat"].to_numpy()))
        cell = (
            pd.Series(
                (bad_sites["lon"].to_numpy() // deg_lon).astype("int64"),
                index=bad_sites.index,
            ).astype(str)
            + "_"
            + pd.Series(
                (bad_sites["lat"].to_numpy() // deg_lat).astype("int64"),
                index=bad_sites.index,
            ).astype(str)
        )
    cell_of = dict(zip(bad_sites["pixel_id"].to_numpy(), cell.to_numpy()))
    # Integer codes in the cells' string sort order, so `np.unique` inside the
    # bootstrap orders the clusters exactly as it did on the strings.
    bad_row_cell = np.unique(
        np.array(
            [cell_of.get(int(p), "na") for p in bad_pixels["pixel_id"].to_numpy()]
        ),
        return_inverse=True,
    )[1].ravel()

    # ---- score every pixel of every stand, in every year --------------------
    # The per-stand work lives at module level (`score_one`, `score_batch`)
    # and reads its inputs from `_C`, so it can run serially, on threads, or in
    # worker processes that map the embedding matrices from disk.
    by_poly = membership.groupby("poly_id")["pixel_id"].apply(np.asarray)
    if args.workers > 1 and args.boot_rng == "shared":
        raise SystemExit(
            "--workers > 1 needs --boot-rng per-stand: one shared "
            "generator must be consumed in stand order"
        )
    if args.batch_small and args.boot_rng == "shared":
        raise SystemExit(
            "--batch-small needs --boot-rng per-stand: a batch "
            "scores its stands year by year, not stand by stand"
        )
    _C.__dict__.update(
        builder=builder,
        whit=whit,
        args=args,
        years=list(args.years),
        ftype=ftype_all,
        by_poly=by_poly,
        bad_row_cell=bad_row_cell,
        boot_rng=args.boot_rng,
        seed=args.seed,
        shared_rng=np.random.default_rng(args.seed),
        bad_cache={},
    )
    # `pixels` is only needed from here on for its ids; dropping the frame
    # frees several GB before any worker process is started.
    row_to_pixel = pixels["pixel_id"].to_numpy()
    del pixels

    from tqdm.auto import tqdm

    stand_ids = sorted(by_poly.index)

    tasks = _plan_tasks(stand_ids, args)
    if args.parallel == "process" and args.workers > 1:
        results = _run_processes(tasks, args.workers, OUT_DATA / "_04_mmap", tqdm)
    elif args.workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        from threadpoolctl import threadpool_limits

        # Parallel across stands, so each stand's BLAS and numba work stays on
        # its own thread instead of oversubscribing the cores.
        from effektovervaking_forests.condition import _kernels

        _kernels.use_serial()
        with threadpool_limits(1), ThreadPoolExecutor(args.workers) as ex:
            results = list(
                tqdm(
                    ex.map(_run_task, tasks),
                    total=len(tasks),
                    desc="scoring",
                    unit="task",
                )
            )
    else:
        results = (_run_task(t) for t in tqdm(tasks, desc="scoring", unit="task"))
    pix_rows, stand_rows = [], []
    for pr, sr in results:
        pix_rows += pr
        stand_rows += sr

    pix = pd.concat(pix_rows, ignore_index=True)
    stands = pd.DataFrame(stand_rows)
    # Batches and out-of-order workers hand rows back grouped differently; put
    # them in the serial order (stand, then year, then pixel) so the outputs do
    # not depend on how the loop was scheduled.
    pix, stands = _serial_order(pix, stands, stand_ids, _C.years)
    # `pixel_row` indexes the embedding matrix; turn it back into a pixel id.
    pix["pixel_id"] = row_to_pixel[pix.pop("pixel_row").to_numpy()]
    print(
        f"\n{len(pix):,} pixel-year scores, {len(stands):,} stand-year scores "
        f"({(time.time() - t0) / 60:.1f} min)"
    )

    # ---- calibrate on the labels, in the year each label was assigned -------
    cal = _calibrate(
        stands,
        polygons,
        blocks,
        pooled=args.legacy,
        thr_boot=args.thr_boot,
        seed=args.seed,
    )
    THRESHOLD_JSON.write_text(json.dumps(cal, indent=2))
    for scope in SCOPES:
        c = cal[scope]
        print(
            f"\n{scope:>6} threshold {c['threshold']:.4f}  Youden J={c['youden_j']:.3f}"
            f"  sens={c['sensitivity']:.2f} spec={c['specificity']:.2f}"
            f"  (n_good={c['n_good']}, n_bad={c['n_bad']})"
        )
        if "by_block" in c:
            bt = np.array(list(c["by_block"].values()))
            print(
                f"         per-block thresholds span {bt.min():.4f}-{bt.max():.4f} "
                f"across {c['n_blocks']} blocks; each stand is classified by "
                "the one fitted WITHOUT its own block"
            )
        if c["youden_j"] < 0.2:
            print(
                "  ! the two label classes barely separate. Report the "
                "continuous score and the CI, not a class per stand."
            )

    # ---- assemble outputs ---------------------------------------------------
    stands["block"] = stands["poly_id"].map(blocks)
    for scope in SCOPES:
        thr = _threshold_for(stands["block"], cal[scope])
        stands[f"threshold_{scope}"] = thr
        stands[f"condition_{scope}"] = [
            classify_condition({"dor": d, "ci_lo": lo, "ci_hi": hi}, t)
            for d, lo, hi, t in zip(
                stands[f"dor_{scope}"],
                stands[f"ci_lo_{scope}"],
                stands[f"ci_hi_{scope}"],
                thr,
            )
        ]
        # The threshold's own error bar is reported per stand but NOT used to
        # withdraw the verdict. Two research results say why. E43 built a joint
        # resample of `score - threshold` -- the test the verdict actually
        # depends on -- and found that counting the boundary honestly still
        # costs about 40% of the field-degraded stands found (recall 0.560 ->
        # 0.320) while wrong reassurance does not fall (0.069 -> 0.074): the
        # boundary is genuinely that poorly pinned down by 36 degraded stands in
        # six localities, so this is a fact about the label set and not a rule
        # worth adopting. E45 then tested whether the same verdict could be had
        # from the two intervals THIS script already computes, and it cannot:
        # agreement with the joint bootstrap is 0.835, because the block
        # resample below holds the scores fixed and so sees only the
        # label-sample half of the threshold's error. So the column is left out
        # rather than shipped wrong; `threshold_se_*` is published as the
        # partial figure it is, for readers who want to see how soft the
        # boundary is next to the stand's own interval.
        stands[f"threshold_se_{scope}"] = _thr_se_for(stands["block"], cal[scope])
        summ = summarise_pixels(pix, f"dor_{scope}")
        stands = stands.merge(summ, on=["poly_id", "year"], how="left")
        # `frac_poor` uses each stand's OWN out-of-block threshold, so the
        # share-of-a-stand-that-is-degraded number is held out the same way its
        # class is.
        thr_by_poly = dict(zip(stands["poly_id"], thr))
        pix_thr = pix["poly_id"].map(thr_by_poly)
        below = pix[f"dor_{scope}"] < pix_thr
        fp = (
            below.groupby([pix["poly_id"], pix["year"]])
            .mean()
            .rename(f"dor_{scope}_frac_poor")
            .reset_index()
        )
        stands = stands.merge(fp, on=["poly_id", "year"], how="left")

    meta = polygons[
        [
            "poly_id",
            "forest_type",
            "layer",
            "tilstand",
            "ref_role",
            "survey_year",
            "area_ha",
            "naturtype",
        ]
    ]
    stands = stands.merge(meta.drop(columns="forest_type"), on="poly_id", how="left")
    stands["label_age"] = stands["year"] - stands["survey_year"]
    pix = pix.merge(
        meta[["poly_id", "forest_type", "ref_role", "survey_year"]],
        on="poly_id",
        how="left",
    )
    pix["label_age"] = pix["year"] - pix["survey_year"]
    # How far this pixel sits inside the stand that claims it. A small stand is
    # mostly edge and an edge pixel is a mixture, so anything read per pixel has
    # to be readable alongside this (research E10).
    if "edge_m" in membership.columns:
        pix = pix.merge(
            membership[["pixel_id", "poly_id", "edge_m"]],
            on=["pixel_id", "poly_id"],
            how="left",
        )
        pix["interior"] = pix["edge_m"] >= INTERIOR_M_DEFAULT

    pix.to_parquet(PIXEL_CONDITION, index=False, compression="zstd")
    stands.to_parquet(CONDITION, index=False)
    print(f"\n-> {PIXEL_CONDITION.name}, {CONDITION.name}, {THRESHOLD_JSON.name}")

    _report(pix, stands, cal)


# ---- the scoring loop ------------------------------------------------------
# Module level rather than a closure in main(), so worker processes can
# import it. `_C` holds what a stand's scoring reads: filled in by main() for
# serial and threaded runs, and by `_init_worker` (with the embedding matrices
# memory-mapped from disk) in each worker process.
_C = SimpleNamespace()
_MMAP_INDEXES = ("_good_index", "_bad_index")


def _rep(ft, X):
    """Embeddings -> the space distances are taken in, for this forest type."""
    w = _C.whit.get(ft)
    return X if w is None else w(X)


def _stand_rng(poly_id):
    if _C.boot_rng == "shared":
        return _C.shared_rng
    return np.random.default_rng((_C.seed, zlib.crc32(str(poly_id).encode())))


def _bad_pack(poly_id, year, ft):
    """(bad, badT, badU, badU.T, bad_cluster) for one stand-year.

    Under the pooled transformed end-member (the default) this is the same
    cloud for every stand of a forest type in a year, so it is whitened,
    normalised and transposed once per (type, year) and cached, instead of
    once per stand-year. The per-stand annulus (`--annulus-bad`) is not cached.
    """
    key = (ft, int(year)) if _C.builder.pooled_bad else None
    if key is not None and key in _C.bad_cache:
        return _C.bad_cache[key]
    brows = _C.builder.bad_pool_rows(poly_id, year, ft)
    bad = _C.builder.bad_mat[brows]
    badT = _rep(ft, bad)
    # The bad pool is shared by both scopes, so its distances are computed
    # once per stand-year rather than twice inside score_matrix.
    badU = unit_rows(badT)
    out = (bad, badT, badU, np.ascontiguousarray(badU.T), _C.bad_row_cell[brows])
    if key is not None:
        _C.bad_cache[key] = out
    return out


def _stand_pools(poly_id, ft):
    """A stand's two good pools, raw / in the metric / unit rows / transposed.

    Good pools do not depend on the probe year -- donors are pinned to their
    own anchor years -- so they are built and mapped into the metric once per
    stand rather than once per stand-year. The transpose `knn_mean_unit` needs
    for its matmul is also done once here rather than once per year per scope.
    """
    pools = {s: _C.builder.good_pool(poly_id, ft, s, with_owners=True) for s in SCOPES}
    poolsT = {s: _rep(ft, g) for s, (g, _o, _n) in pools.items()}
    poolsU = {s: unit_rows(g) for s, g in poolsT.items()}
    poolsUt = {s: np.ascontiguousarray(u.T) for s, u in poolsU.items()}
    n_clusters = {
        s: int(len(np.unique(o))) if len(o) else 0 for s, (_g, o, _n) in pools.items()
    }
    return pools, poolsT, poolsU, poolsUt, n_clusters


def _pixel_good(probesU, sp, cols, sl=slice(None)):
    """Fill `cols[dor_/m_good_/n_donors_<scope>]` for these probes."""
    pools, _t, poolsU, poolsUt, _c = sp
    k = _C.args.k
    for scope in SCOPES:
        good, _owner, n_don = pools[scope]
        m_bad = cols["m_bad"][sl]
        if len(good) >= k:
            m_good = knn_mean_unit(probesU, poolsU[scope], k, pool_t=poolsUt[scope])
            dor = (m_bad / (m_good + m_bad + EPS)).astype(np.float32)
        else:
            m_good = np.full(len(probesU), np.nan, dtype=np.float32)
            dor = m_good.copy()
        if isinstance(sl, slice) and sl == slice(None):
            cols[f"dor_{scope}"] = dor
            cols[f"m_good_{scope}"] = m_good
            cols[f"n_donors_{scope}"] = n_don
        else:
            cols[f"dor_{scope}"][sl] = dor
            cols[f"m_good_{scope}"][sl] = m_good
            cols[f"n_donors_{scope}"][sl] = n_don


def _stand_record(poly_id, ft, year, probes, rng, sp, bp, probesU=None, cells=None):
    """One stand-year row: the stand score and its donor-clustered interval.

    Under `--stand-score median` (the default) the score is the median of the
    stand-year's cell scores `cells[dor_<scope>]` and the interval resamples
    donor stands and localities once for all of `probesU`; the mean-embedding
    score is kept, as a point value, in `dor_<scope>_probe`.
    """
    args = _C.args
    pools, poolsT, _u, poolsUt, n_clusters = sp
    bad, badT, _bu, badUt, bad_cluster = bp
    srec = {
        "poly_id": poly_id,
        "forest_type": ft,
        "year": year,
        "n_pixels": len(probes),
        "n_bad": len(bad),
    }
    # The probe and the whole transformed half of the interval are the same
    # for both scopes -- only the good pool changes -- so they are built once
    # here rather than rebuilt inside each call.
    probe = stand_probe(probes, _C.whit.get(ft))
    if args.stand_score == "median":
        med = median_cluster_ci(
            probesU,
            {s: cells[f"dor_{s}"] for s in SCOPES},
            {s: (poolsUt[s], pools[s][1]) for s in SCOPES},
            (badUt, bad_cluster),
            args.k,
            args.n_boot,
            rng,
        )
        d_b = cosine_dists_to_set(probe, np.asarray(badT, dtype=float))
        for scope in SCOPES:
            good, owner, n_don = pools[scope]
            d_g = (
                cosine_dists_to_set(probe, np.asarray(poolsT[scope], dtype=float))
                if len(good) >= args.k
                else np.empty(0)
            )
            point, lo, hi = med[scope]
            srec.update(
                {
                    f"dor_{scope}": point,
                    f"ci_lo_{scope}": lo,
                    f"ci_hi_{scope}": hi,
                    f"dor_{scope}_probe": (
                        knn_score(d_g, d_b, args.k) if len(d_g) >= args.k else np.nan
                    ),
                    f"n_good_{scope}": len(good),
                    f"n_donors_{scope}": n_don,
                    f"n_clusters_{scope}": n_clusters[scope],
                }
            )
        return srec
    bad_knn = None if args.legacy else bad_side(probe, badT, bad_cluster, args.k)
    for scope in SCOPES:
        good, owner, n_don = pools[scope]
        s = score_stand_year(
            probes,
            good,
            bad,
            k=args.k,
            n_boot=args.n_boot,
            rng=rng,
            transform=_C.whit.get(ft),
            probe=probe,
            good_t=poolsT[scope],
            bad_t=badT,
            bad_knn=bad_knn,
            thinned=args.boot_draw == "thinned",
            # Resample donor stands and 500 m localities, not rows: 60 pixels
            # of one donor are one donor seen 60 times, and the row-wise
            # interval is about a third too narrow (research E09).
            good_cluster=None if args.legacy else owner,
            bad_cluster=None if args.legacy else bad_cluster,
        )
        srec.update(
            {
                f"dor_{scope}": s["dor"],
                f"ci_lo_{scope}": s["ci_lo"],
                f"ci_hi_{scope}": s["ci_hi"],
                f"n_good_{scope}": s["n_good"],
                f"n_donors_{scope}": n_don,
                f"n_clusters_{scope}": n_clusters[scope],
            }
        )
    return srec


def score_one(poly_id):
    """Every pixel-year and stand-year score of one stand."""
    b = _C.builder
    rng = _stand_rng(poly_id)
    pix_rows, stand_rows = [], []
    ft = _C.ftype.get(poly_id)
    pid = _C.by_poly[poly_id]
    sp = _stand_pools(poly_id, ft)
    for year in _C.years:
        rows = b.probe_rows(pid, year)
        if not len(rows):
            continue
        probes = b.probes(rows)
        probesU = unit_rows(_rep(ft, probes))
        bp = _bad_pack(poly_id, year, ft)
        cols = {
            "poly_id": poly_id,
            "year": year,
            "pixel_row": rows,
            "m_bad": knn_mean_unit(probesU, bp[2], _C.args.k, pool_t=bp[3]),
        }
        _pixel_good(probesU, sp, cols)
        pix_rows.append(pd.DataFrame(cols))
        stand_rows.append(
            _stand_record(poly_id, ft, year, probes, rng, sp, bp, probesU, cols)
        )
    return pix_rows, stand_rows


def score_batch(poly_ids):
    """`score_one` for many small stands of ONE forest type, a year at a time.

    Under the pooled transformed end-member every stand of a type is scored
    against the same transformed cloud in a year, so the stands' pixels are
    stacked and their transformed kNN is one matmul + top-k instead of one per
    stand, and the year's pixel rows become one frame. The good pools differ
    per stand and stay per stand, as does the bootstrap, each stand drawing
    from its own generator in the same order as `score_one` would.
    """
    b = _C.builder
    k = _C.args.k
    ft = _C.ftype.get(poly_ids[0])
    prep = [(p, _stand_rng(p), _stand_pools(p, ft)) for p in poly_ids]
    pix_rows, stand_rows = [], []
    for year in _C.years:
        rows = [b.probe_rows(_C.by_poly[p], year) for p in poly_ids]
        n = np.array([len(r) for r in rows])
        if not n.sum():
            continue
        cuts = np.r_[0, np.cumsum(n)]
        allrows = np.concatenate(rows)
        probes_all = b.probes(allrows)
        pU_all = unit_rows(_rep(ft, probes_all))
        bp = _bad_pack(poly_ids[0], year, ft)
        N = len(allrows)
        cols = {
            "poly_id": np.repeat(np.asarray(poly_ids, dtype=object), n),
            "year": year,
            "pixel_row": allrows,
            "m_bad": knn_mean_unit(pU_all, bp[2], k, pool_t=bp[3]),
        }
        for scope in SCOPES:
            cols[f"dor_{scope}"] = np.empty(N, dtype=np.float32)
            cols[f"m_good_{scope}"] = np.empty(N, dtype=np.float32)
            cols[f"n_donors_{scope}"] = np.empty(N, dtype=np.int64)
        for i, (p, rng, sp) in enumerate(prep):
            if not n[i]:
                continue
            sl = slice(cuts[i], cuts[i + 1])
            _pixel_good(pU_all[sl], sp, cols, sl)
            stand_rows.append(
                _stand_record(
                    p,
                    ft,
                    year,
                    probes_all[sl],
                    rng,
                    sp,
                    bp,
                    pU_all[sl],
                    {f"dor_{s}": cols[f"dor_{s}"][sl] for s in SCOPES},
                )
            )
        pix_rows.append(pd.DataFrame(cols))
    return pix_rows, stand_rows


def _run_task(task):
    kind, what = task
    return score_one(what) if kind == "one" else score_batch(what)


def _plan_tasks(stand_ids, args):
    """[("one", poly_id) | ("batch", [poly_id, ...])], in the order to run them.

    With `--batch-small N`, stands of at most N pixels are grouped by forest
    type into batches of `--batch-size`. Worker processes get the biggest
    tasks first so no worker is left finishing a 46,000-pixel stand alone.
    """
    size = {p: len(_C.by_poly[p]) for p in stand_ids}
    small = set()
    if args.batch_small:
        if not _C.builder.pooled_bad:
            print(
                "  ! --batch-small needs the pooled transformed end-member; "
                "--annulus-bad scores stand by stand"
            )
        else:
            small = {p for p in stand_ids if size[p] <= args.batch_small}
    tasks = [("one", p) for p in stand_ids if p not in small]
    by_type: dict = {}
    for p in stand_ids:
        if p in small:
            by_type.setdefault(_C.ftype.get(p), []).append(p)
    for ids in by_type.values():
        for s in range(0, len(ids), args.batch_size):
            tasks.append(("batch", ids[s : s + args.batch_size]))
    if args.parallel == "process" and args.workers > 1:
        tasks.sort(
            key=lambda t: -(size[t[1]] if t[0] == "one" else sum(size[p] for p in t[1]))
        )
    n_b = sum(t[0] == "batch" for t in tasks)
    if n_b:
        print(
            f"  {len(small)} stands of <= {args.batch_small} pixels scored in "
            f"{n_b} batches; {len(tasks) - n_b} stands one by one"
        )
    return tasks


def _serial_order(pix, stands, stand_ids, years):
    """Rows in the serial loop's order: stand, then year, then pixel."""
    pos = {p: i for i, p in enumerate(stand_ids)}
    yi = {y: i for i, y in enumerate(years)}
    nyr = len(years)
    skey = stands["poly_id"].map(pos).to_numpy(np.int64) * nyr + stands["year"].map(
        yi
    ).to_numpy(np.int64)
    if np.any(np.diff(skey) < 0):
        stands = stands.iloc[np.argsort(skey, kind="stable")].reset_index(drop=True)
    # pixel rows come in contiguous (stand, year) runs; order the runs, not
    # the 18M rows one by one
    pk = pix["poly_id"].to_numpy()
    yk = pix["year"].to_numpy()
    brk = np.flatnonzero((pk[1:] != pk[:-1]) | (yk[1:] != yk[:-1])) + 1
    starts = np.r_[0, brk]
    ends = np.r_[brk, len(pix)]
    rkey = np.array([pos[p] for p in pk[starts]], dtype=np.int64) * nyr + np.array(
        [yi[y] for y in yk[starts]], dtype=np.int64
    )
    if np.any(np.diff(rkey) < 0):
        order = np.argsort(rkey, kind="stable")
        idx = np.concatenate([np.arange(starts[i], ends[i]) for i in order])
        pix = pix.take(idx).reset_index(drop=True)
    return pix, stands


def _init_worker(ctx_dir: str) -> None:
    """Worker-process setup: the context, with the big matrices memory-mapped."""
    global _LIMITS
    import pickle

    from threadpoolctl import threadpool_limits

    from effektovervaking_forests.condition import _kernels

    d = Path(ctx_dir)
    with open(d / "ctx.pkl", "rb") as f:
        ctx = pickle.load(f)
    b = ctx["builder"]

    def load(name):
        return np.load(d / f"{name}.npy", mmap_mode="r")

    b.good_mat, b.bad_mat = load("good_mat"), load("bad_mat")
    for attr in _MMAP_INDEXES:
        setattr(
            b,
            attr,
            {
                y: (load(f"{attr}_{y}_ids"), load(f"{attr}_{y}_rows"))
                for y in ctx["index_years"][attr]
            },
        )
    _C.__dict__.update(ctx["C"])
    _C.builder = b
    _C.bad_cache = {}
    # one stand at a time per process: its BLAS and numba work stays on one core
    _kernels.use_serial()
    _LIMITS = threadpool_limits(1)


def _run_processes(tasks, workers: int, ctx_dir: Path, tqdm):
    """Run `tasks` in `workers` spawned processes sharing memory-mapped embeddings.

    Threads are held back by the GIL between the numpy calls (pool assembly,
    the bootstrap's bookkeeping); processes are not. Each worker maps the
    embedding matrices and their year indexes from .npy files instead of
    receiving a copy, so the OS page cache holds one copy for all of them.
    """
    import copy
    import multiprocessing as mp
    import pickle
    import shutil
    from concurrent.futures import ProcessPoolExecutor, as_completed

    t = time.time()
    ctx_dir.mkdir(parents=True, exist_ok=True)
    b = _C.builder
    np.save(ctx_dir / "good_mat.npy", b.good_mat)
    np.save(ctx_dir / "bad_mat.npy", b.bad_mat)
    index_years = {}
    for attr in _MMAP_INDEXES:
        idx = getattr(b, attr)
        index_years[attr] = list(idx)
        for y, (ids, rows) in idx.items():
            np.save(ctx_dir / f"{attr}_{y}_ids.npy", ids)
            np.save(ctx_dir / f"{attr}_{y}_rows.npy", rows)
    light = copy.copy(b)
    light.good_mat = light.bad_mat = None
    for attr in _MMAP_INDEXES:
        setattr(light, attr, None)
    C = {
        k: v
        for k, v in _C.__dict__.items()
        if k not in ("builder", "bad_cache", "shared_rng")
    }
    with open(ctx_dir / "ctx.pkl", "wb") as f:
        pickle.dump(
            {"builder": light, "C": C, "index_years": index_years},
            f,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    print(
        f"  memory-mapped context for {workers} worker processes written in "
        f"{time.time() - t:.1f}s"
    )
    try:
        out = [None] * len(tasks)
        with ProcessPoolExecutor(
            workers,
            mp_context=mp.get_context("spawn"),
            initializer=_init_worker,
            initargs=(str(ctx_dir),),
        ) as ex:
            futs = {ex.submit(_run_task, task): i for i, task in enumerate(tasks)}
            for f in tqdm(
                as_completed(futs), total=len(futs), desc="scoring", unit="task"
            ):
                out[futs[f]] = f.result()
        return out
    finally:
        shutil.rmtree(ctx_dir, ignore_errors=True)


def _threshold_for(block: pd.Series, cal: dict) -> np.ndarray:
    """Each stand's decision threshold: its block's, or the pooled one as fallback."""
    by = cal.get("by_block")
    if not by:
        return np.full(len(block), cal["threshold"], dtype=float)
    return block.astype(str).map(by).fillna(cal["threshold"]).to_numpy(dtype=float)


def _thr_se_for(block: pd.Series, cal: dict) -> np.ndarray:
    """Each stand's threshold standard error: its block's, or the pooled one."""
    iv = cal.get("interval")
    if not iv:
        return np.full(len(block), np.nan, dtype=float)
    by = {k: v["se"] for k, v in iv["by_block"].items()}
    fallback = float(iv["pooled"].get("se", np.nan))
    return block.astype(str).map(by).fillna(fallback).to_numpy(dtype=float)


def _calibrate(
    stands: pd.DataFrame,
    polygons: pd.DataFrame,
    blocks: pd.Series,
    pooled: bool = False,
    thr_boot: int = 300,
    seed: int = 42,
) -> dict:
    """Fit a threshold per scope, using each labelled stand IN ITS SURVEY YEAR.

    Scoring a `god` stand in 2025 against a label assigned in 2017 would
    calibrate the threshold on whatever happened in between, which is exactly
    the confound this pipeline exists to avoid. So the calibration set is one
    row per labelled stand, taken at `year == survey_year`.

    The threshold is also fitted **leave-block-out**: a threshold fitted to all
    the labelled stands and then applied to those same stands is a fit reported
    as a validation, and with 36 bad stands it is worth a couple of points of
    Youden's J (research E01/E09). `pooled=True` restores the old behaviour.
    """
    lab = polygons.loc[
        polygons["ref_role"].isin(["good", "bad"]),
        ["poly_id", "ref_role", "survey_year"],
    ]
    at_survey = stands[["poly_id", "year", *[f"dor_{s}" for s in SCOPES]]].merge(
        lab, on="poly_id", how="inner"
    )
    at_survey = at_survey[at_survey["year"] == at_survey["survey_year"]]
    if at_survey.empty:
        raise SystemExit(
            "no labelled stand was scored in its own survey year -- pass "
            "--years covering the survey years, or the threshold would be "
            "fitted to labels that describe a different year"
        )
    at_survey = at_survey.assign(block=at_survey["poly_id"].map(blocks))
    out = {}
    for scope in SCOPES:
        cal = calibrate_threshold_blocked(
            at_survey[f"dor_{scope}"], at_survey["ref_role"], at_survey["block"]
        )
        if pooled:
            cal.pop("by_block", None)
            cal.pop("n_blocks", None)
        else:
            # The threshold is an estimate too, and on this label set a noisier
            # one than the stand's own score. Reported, never classified with:
            # research E43 measured what withdrawing verdicts on it costs, and
            # E45 showed this estimator sees only the label-sample half of it.
            cal["interval"] = threshold_interval_blocked(
                at_survey[f"dor_{scope}"],
                at_survey["ref_role"],
                at_survey["block"],
                n_boot=thr_boot,
                seed=seed,
            )
        out[scope] = cal
    out["n_calibration_stands"] = int(len(at_survey))
    return out


def _report(pix: pd.DataFrame, stands: pd.DataFrame, cal: dict) -> None:
    print(
        "\nmedian per-pixel DoR by field label (tilstand), scored in the "
        "survey year -- the ordering is the check that the score means what "
        "it claims:"
    )
    at = pix[pix["label_age"] == 0]
    print(
        at.groupby(at["ref_role"])[["dor_local", "dor_global"]]
        .agg(["count", "median"])
        .round(3)
        .to_string()
    )
    print("\nstand condition x forest type (local scope, latest year):")
    last = stands[stands["year"] == stands["year"].max()]
    print(pd.crosstab(last["condition_local"], last["forest_type"]).to_string())
    print("\nlocal vs global agreement on the stand class (latest year):")
    print(pd.crosstab(last["condition_local"], last["condition_global"]).to_string())
    n_nolocal = int((last["n_donors_local"] == 0).sum())
    if n_nolocal:
        print(
            f"\n{n_nolocal} stands have no donor within the local radius and "
            "carry a global score only"
        )


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("04", sys.argv[1:])
