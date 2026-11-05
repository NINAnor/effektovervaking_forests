"""Step 4 (= the user-facing STEP 2) -- per-pixel condition scoring.

For every pixel of every stand and every year, scores the distance ratio
dor = bad / (good + bad) from k nearest neighbours (cosine, whitened metric)
to a good and a bad reference pool, at two scopes (`local`, `global`). The
stand score is the median of its pixel scores with a donor-clustered
bootstrap interval; thresholds are fitted leave-block-out on the field labels in
their survey year. Method and defaults: docs/condition/METHODS.md.

Needs steps 00, 02, 02b, 02c, 02d and 02e (`hansen_pixels.parquet` is required to
drop cleared donor pixels). Runs for the whole record unless `--years` is given.

Outputs `pixel_condition.parquet` (per pixel-year), `condition.parquet` (per
stand-year), `threshold.json` and `whitener.json`.

Run:  forestmon condition step 04
"""

from __future__ import annotations

import argparse
import json
import sys
import time
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
    calibrate_threshold_blocked,
    classify_condition,
    knn_mean_unit,
    median_cluster_ci,
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
        "--max-type-bad",
        type=int,
        default=2000,
        help="cap on the pooled transformed cloud, per forest type and year",
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
        "that classifying with it costs 40%% of the degraded "
        "stands found for no fall in wrong reassurance",
    )
    ap.add_argument("--seed", type=int, default=42)
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

    t0 = time.time()
    polygons = pd.read_parquet(POLYGONS)
    poly_geo = gpd.read_parquet(POLY_GEO)
    pixels = pd.read_parquet(PIXELS)
    membership = pd.read_parquet(PIXEL_POLYGONS)
    roles = pd.read_parquet(ROLES)
    overlap = pd.read_parquet(POLY_OVERLAP)
    bad_pixels = pd.read_parquet(BAD_PIXELS)
    bad_membership = pd.read_parquet(BAD_MEMBERSHIP)

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

    # Donor pixels cleared (Hansen) at or before their survey year are dropped.
    hp = OUT_DATA / "hansen_pixels.parquet"
    if not hp.exists():
        raise SystemExit(f"the good-pool cleanup needs {hp.name}; run step 02e")
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
        n_donors=args.n_donors,
        donor_km=args.donor_km,
        pool_by_type=pool_by_type,
        max_km=args.max_km,
        max_per_donor=args.max_per_donor,
        max_global=args.max_global,
        seed=args.seed,
        pooled_bad=True,
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
    # Fitted per forest type on unlabelled pixels only (condition/whiten.py).
    whit = {}
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
    WHITENER_JSON.write_text(json.dumps({ft: w.to_dict() for ft, w in whit.items()}))

    # Locality of each transformed reference pixel: the interval resamples
    # localities (500 m cells), not pixels.
    bad_sites = pd.read_parquet(OUT_DATA / "bad_sites.parquet")
    cell = (
        (bad_sites["x"] // BAD_CLUSTER_M).astype("int64").astype(str)
        + "_"
        + (bad_sites["y"] // BAD_CLUSTER_M).astype("int64").astype(str)
        + "_"
        + bad_sites["utm_zone"].astype(str)
    )
    cell_of = dict(zip(bad_sites["pixel_id"].to_numpy(), cell.to_numpy()))
    # Integer codes in the cells' string sort order.
    bad_row_cell = np.unique(
        np.array(
            [cell_of.get(int(p), "na") for p in bad_pixels["pixel_id"].to_numpy()]
        ),
        return_inverse=True,
    )[1].ravel()

    # ---- score every pixel of every stand, in every year --------------------
    # One rng, consumed in stand order, so the bootstrap is reproducible.
    by_poly = membership.groupby("poly_id")["pixel_id"].apply(np.asarray)
    _C.__dict__.update(
        builder=builder,
        whit=whit,
        args=args,
        years=list(args.years),
        ftype=ftype_all,
        by_poly=by_poly,
        bad_row_cell=bad_row_cell,
        rng=np.random.default_rng(args.seed),
        bad_cache={},
    )
    row_to_pixel = pixels["pixel_id"].to_numpy()
    del pixels

    from tqdm.auto import tqdm

    pix_rows, stand_rows = [], []
    for poly_id in tqdm(sorted(by_poly.index), desc="scoring", unit="stand"):
        pr, sr = score_one(poly_id)
        pix_rows += pr
        stand_rows += sr

    pix = pd.concat(pix_rows, ignore_index=True)
    stands = pd.DataFrame(stand_rows)
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
        # `threshold_se_*` is the label-sample half of the threshold's error only;
        # it is reported but not used to withdraw verdicts (METHODS.md, E43/E45).
        stands[f"threshold_se_{scope}"] = _thr_se_for(stands["block"], cal[scope])
        summ = summarise_pixels(pix, f"dor_{scope}")
        stands = stands.merge(summ, on=["poly_id", "year"], how="left")
        # `frac_poor` uses each stand's own out-of-block threshold.
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
    # Distance inside the claiming stand; read per-pixel scores with it (E10).
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
# `_C` holds what a stand's scoring reads; filled in by main().
_C = SimpleNamespace()


def _rep(ft, X):
    """Embeddings -> the space distances are taken in, for this forest type."""
    w = _C.whit.get(ft)
    return X if w is None else w(X)


def _bad_pack(poly_id, year, ft):
    """(bad, badT, badU, badU.T, bad_cluster) for one stand-year.

    The bad pool is shared per (forest type, year), so it is cached."""
    key = (ft, int(year))
    if key in _C.bad_cache:
        return _C.bad_cache[key]
    brows = _C.builder.bad_pool_rows(poly_id, year, ft)
    bad = _C.builder.bad_mat[brows]
    badT = _rep(ft, bad)
    badU = unit_rows(badT)
    out = (bad, badT, badU, np.ascontiguousarray(badU.T), _C.bad_row_cell[brows])
    _C.bad_cache[key] = out
    return out


def _stand_pools(poly_id, ft):
    """A stand's two good pools: raw / in the metric / unit rows / transposed.

    Independent of the probe year (donors are pinned to their anchor years),
    so built once per stand."""
    pools = {s: _C.builder.good_pool(poly_id, ft, s, with_owners=True) for s in SCOPES}
    poolsT = {s: _rep(ft, g) for s, (g, _o, _n) in pools.items()}
    poolsU = {s: unit_rows(g) for s, g in poolsT.items()}
    poolsUt = {s: np.ascontiguousarray(u.T) for s, u in poolsU.items()}
    n_clusters = {
        s: int(len(np.unique(o))) if len(o) else 0 for s, (_g, o, _n) in pools.items()
    }
    return pools, poolsT, poolsU, poolsUt, n_clusters


def _pixel_good(probesU, sp, cols):
    """Fill `cols[dor_/m_good_/n_donors_<scope>]` for these probes."""
    pools, _t, poolsU, poolsUt, _c = sp
    k = _C.args.k
    for scope in SCOPES:
        good, _owner, n_don = pools[scope]
        m_bad = cols["m_bad"]
        if len(good) >= k:
            m_good = knn_mean_unit(probesU, poolsU[scope], k, pool_t=poolsUt[scope])
            dor = (m_bad / (m_good + m_bad + EPS)).astype(np.float32)
        else:
            m_good = np.full(len(probesU), np.nan, dtype=np.float32)
            dor = m_good.copy()
        cols[f"dor_{scope}"] = dor
        cols[f"m_good_{scope}"] = m_good
        cols[f"n_donors_{scope}"] = n_don


def _stand_record(poly_id, ft, year, probes, rng, sp, bp, probesU, cells):
    """One stand-year row: median of `cells[dor_<scope>]` with its donor-clustered
    interval; the mean-embedding score is kept as `dor_<scope>_probe`."""
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
    probe = stand_probe(probes, _C.whit.get(ft))
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


def score_one(poly_id):
    """Every pixel-year and stand-year score of one stand."""
    b = _C.builder
    rng = _C.rng
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
    thr_boot: int = 300,
    seed: int = 42,
) -> dict:
    """Fit a threshold per scope, leave-block-out, on one row per labelled
    stand taken at `year == survey_year`."""
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
        # Reported only, never classified with (METHODS.md, E43/E45).
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
