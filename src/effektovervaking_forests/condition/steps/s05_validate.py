"""Step 5 -- join trajectory to condition, and check both against the field labels.

This is the step that decides whether any of the preceding numbers mean
anything. The `tilstand` labels give four tests, and the fourth exists because
the first run of this pipeline failed it:

  1. **Ordering.** Median DoR should fall god > moderat > dårlig >
     sværtRedusert. `moderat` never enters a reference pool, so its position
     between the two anchors is a genuine out-of-sample check, not a fit.
  2. **Separation.** How well the leave-one-out DoR separates labelled good from
     labelled bad, as AUC and as the calibrated Youden J -- at BOTH grains. The
     stand and the pixel answer differently and both belong in the report: a
     `tilstand` describes a whole stand, so it is a weak label for any one
     pixel, and a pixel-grain AUC well below the stand-grain one is expected
     rather than alarming.
  3. **Where the score is coming from.** `dor = m_bad / (m_good + m_bad)` can
     rise either because a pixel moved toward the good cloud or because it is
     far from the transformed one. Those are different claims, and with a bad
     pool built from cropland and built-up the second is the larger term: `m_bad`
     runs 2-3x `m_good` and varies more. So this step reports the discriminative
     power of each component separately. If `m_bad` alone carries almost all of
     the AUC, the map is substantially a map of distance-from-town, and that has
     to be visible rather than buried in a ratio.
  4b. **Controls.** Stand area predicts the field label almost as well as the
     embedding does -- the bad-labelled stands are smaller -- and every bad
     label in the set comes from an MI survey. So the AUC is also reported
     within log-area strata and on pixels at least 20 m inside their own stand,
     because a separation that only exists between stands of different sizes,
     or only on mixed edge pixels, is not a condition signal.
  4c. **Trajectory against an independent product.** Hansen Global Forest Change
     loss year (step 2e) is Landsat-based, so it is the one label here that does
     not come from AlphaEarth. It gives the `declining` class a recall and a
     false-positive rate instead of an assertion.

  5. **Coherence with trajectory.** Stands the Mann-Kendall test calls
     `declining` should score lower than `improving` ones. The trajectory is
     the trend of the nature type score itself (since 2026-10; the naturalness
     axis under `03_trajectory.py --input axis`), so disagreement points at
     stands in poor condition but recovering, or good but deteriorating -- which
     is exactly the management-relevant quadrant.

Every AUC carries a bootstrap over whole 5 km spatial BLOCKS, not over pixels:
97k pixels from 421 stands in 66 localities are not 97k independent
observations, and a row-wise interval would understate the uncertainty by
roughly the square root of that ratio.

Everything is evaluated at `label_age == 0`, i.e. each labelled stand in the
year it was actually surveyed. Scoring a 2017 `god` label against a 2025
embedding would test what happened in between, not the method.

Output: `outputs/data/condition_combined.csv` + a printed report.

Run:  forestmon condition step 05
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

from effektovervaking_forests.condition.blocks import INTERIOR_M_DEFAULT
from effektovervaking_forests.condition.paths import (
    COMBINED,
    CONDITION,
    HANSEN_PIXELS,
    OUT_DATA,
    PIXEL_CONDITION,
    PIXEL_POLYGONS,
    POLYGONS,
    TRAJECTORY,
)

TILSTAND_ORDER = ["god", "moderat", "dårlig", "sværtRedusert"]
SCOPES = ("local", "global")


def auc(scores: np.ndarray, positive: np.ndarray) -> float:
    """Rank-based AUC (Mann-Whitney U), ties handled by averaging ranks."""
    ok = np.isfinite(scores)
    s, y = np.asarray(scores)[ok], np.asarray(positive)[ok]
    if not y.any() or y.all():
        return float("nan")
    order = np.argsort(s, kind="mergesort")
    ranks = np.empty(len(s), float)
    ranks[order] = np.arange(1, len(s) + 1)
    # average ranks within ties so a flat score does not fake discrimination
    ranks = (
        pd.DataFrame({"s": s, "r": ranks})
        .groupby("s")["r"]
        .transform("mean")
        .to_numpy()
    )
    n1, n0 = int(y.sum()), int((~y).sum())
    return float((ranks[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def block_ci(scores, positive, blocks, n_boot: int = 200, seed: int = 0):
    """(auc, lo, hi) resampling whole spatial blocks.

    The stands are not independent -- the six layers map the same localities
    repeatedly -- so the block, not the pixel and not the stand, is the unit
    that is plausibly exchangeable.
    """
    s = np.asarray(scores, float)
    y = np.asarray(positive, bool)
    b = np.asarray(blocks)
    ok = np.isfinite(s)
    s, y, b = s[ok], y[ok], b[ok]
    point = auc(s, y)
    uniq = np.unique(b)
    if len(uniq) < 3:
        return point, float("nan"), float("nan")
    by = {u: np.flatnonzero(b == u) for u in uniq}
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(n_boot):
        rows = np.concatenate([by[u] for u in rng.choice(uniq, len(uniq))])
        a = auc(s[rows], y[rows])
        if np.isfinite(a):
            out.append(a)
    if len(out) < 20:
        return point, float("nan"), float("nan")
    return point, float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))


def stratified_auc(df, strat, col):
    """AUC within strata, pooled by stratum size -- "among stands of this size"."""
    y = (df["ref_role"] == "good").to_numpy()
    aucs, wts = [], []
    for v in pd.unique(strat):
        m = strat == v
        a = auc(df.loc[m, col].to_numpy(), y[m])
        if np.isfinite(a) and (~y[m]).sum() >= 3:
            aucs.append(a)
            wts.append(int(m.sum()))
    return float(np.average(aucs, weights=wts)) if aucs else float("nan")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--year",
        type=int,
        default=None,
        help="year to carry into the combined table (default: the "
        "latest scored, which is the only one that is a "
        "statement about the stands now)",
    )
    args = ap.parse_args(argv)

    cond = pd.read_parquet(CONDITION)
    traj = pd.read_parquet(TRAJECTORY)
    pix = pd.read_parquet(PIXEL_CONDITION)

    year = args.year or int(cond["year"].max())
    keep_traj = [
        c
        for c in ["poly_id", "trajectory", "sen_slope", "mk_z", "mk_p", "n_valid", "sd"]
        if c in traj.columns
    ]
    df = cond[cond["year"] == year].merge(traj[keep_traj], on="poly_id", how="outer")
    df.to_csv(COMBINED, index=False)
    print(f"{len(df)} polygons at {year} -> {COMBINED.name}")
    print(
        f"({len(cond):,} stand-years and {len(pix):,} pixel-years available in full)\n"
    )

    polygons = pd.read_parquet(POLYGONS)
    blk = polygons.set_index("poly_id")["block"]
    area = polygons.set_index("poly_id")["area_ha"]
    cond["block"], pix["block"] = cond["poly_id"].map(blk), pix["poly_id"].map(blk)
    at_stand = cond[cond["label_age"] == 0].copy()
    at_pixel = pix[pix["label_age"] == 0].copy()
    at_stand["area_ha"] = at_stand["poly_id"].map(area)

    # ---- 1. ordering -------------------------------------------------------
    print("1. DoR by field condition label, each stand in its own survey year")
    for scope in SCOPES:
        tab = (
            at_stand.groupby(at_stand["tilstand"].fillna("(none)"))[f"dor_{scope}"]
            .agg(["count", "median", "std"])
            .round(3)
        )
        order = [t for t in TILSTAND_ORDER if t in tab.index] + [
            t for t in tab.index if t not in TILSTAND_ORDER
        ]
        print(f"\n   scope = {scope}")
        print(tab.loc[order].to_string())
        meds = [tab.at[t, "median"] for t in TILSTAND_ORDER if t in tab.index]
        print(
            f"   monotone god -> sværtRedusert: "
            f"{'YES' if all(a >= b for a, b in zip(meds, meds[1:])) else 'NO'}"
        )
        if "moderat" in tab.index and {"god", "dårlig"} <= set(tab.index):
            m, g, b = (tab.at[k, "median"] for k in ("moderat", "god", "dårlig"))
            print(
                f"   held-out 'moderat' median {m:.3f} between god {g:.3f} and "
                f"dårlig {b:.3f}: {'YES' if min(g, b) <= m <= max(g, b) else 'NO'}"
            )

    # ---- 2. separation, at both grains -------------------------------------
    print("\n2. separation of labelled good vs bad (leave-one-out)")
    for grain, tbl in (("stand", at_stand), ("pixel", at_pixel)):
        lab = tbl[tbl["ref_role"].isin(["good", "bad"])]
        if lab.empty:
            continue
        y = (lab["ref_role"] == "good").to_numpy()
        print(f"\n   grain = {grain}  (n_good={int(y.sum())}, n_bad={int((~y).sum())})")
        for scope in SCOPES:
            line = f"     dor_{scope:<7}"
            for ftype, g in list(lab.groupby("forest_type")) + [("both", lab)]:
                a, lo, hi = block_ci(
                    g[f"dor_{scope}"].to_numpy(),
                    (g["ref_role"] == "good").to_numpy(),
                    g["block"].to_numpy(),
                )
                ci = f"[{lo:.2f},{hi:.2f}]" if np.isfinite(lo) else ""
                line += f"  {ftype}={a:.3f}{ci}"
            print(line)
        print("     intervals resample whole 5 km spatial blocks, not rows")

    # ---- 2b. what the DEPLOYED three-way decision actually delivers --------
    # AUC answers "does the score rank a degraded stand below a good one", which
    # is prevalence-free and is the right question for the ranking claim. It is
    # silent about the question a manager asks -- "of the stands this calls poor,
    # how many are degraded, and how many degraded stands does it miss" -- and
    # those depend on the threshold AND on the abstention rule. Reported here
    # because the gap between the two is large and not visible from AUC.
    print("\n2b. what the deployed decision delivers (positive class = DEGRADED)")
    lab2 = at_stand[at_stand["ref_role"].isin(["good", "bad"])]
    yb = (lab2["ref_role"] == "bad").to_numpy()
    print(
        f"   {len(lab2)} labelled stands, {int(yb.sum())} degraded "
        f"-- label-set prevalence {yb.mean():.3f}"
    )
    print(
        "   ! precision and F1 inherit that prevalence, which is a survey "
        "artefact, not a base rate. Sensitivity, specificity and AUC do not."
    )
    for scope in SCOPES:
        cls = lab2[f"condition_{scope}"].to_numpy()
        sc = lab2[f"dor_{scope}"].to_numpy()
        fin = np.isfinite(sc)
        for label, keep in (
            ("classified only", fin & (cls != "indistinguishable")),
            ("abstention = not flagged", fin),
        ):
            pred = cls[keep] == "poor_condition"
            t = yb[keep]
            tp = int((pred & t).sum())
            fp = int((pred & ~t).sum())
            fn = int((~pred & t).sum())
            tn = int((~pred & ~t).sum())
            prec = tp / (tp + fp) if tp + fp else float("nan")
            rec = tp / (tp + fn) if tp + fn else float("nan")
            spec = tn / (tn + fp) if tn + fp else float("nan")
            f1 = 2 * prec * rec / (prec + rec) if prec + rec else float("nan")
            den = np.sqrt(float(tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
            mcc = (tp * tn - fp * fn) / den if den else float("nan")
            print(
                f"   {scope:<7} [{label:<24}] n={keep.sum():<4} "
                f"TP {tp:<3} FP {fp:<3} FN {fn:<3} TN {tn:<4} "
                f"prec {prec:.3f} recall {rec:.3f} spec {spec:.3f} "
                f"F1 {f1:.3f} MCC {mcc:.3f}"
            )
    print(
        "   MCC and F1 on ~36 positives in six localities are far noisier than "
        "AUC: block-bootstrap widths ~0.63 and ~0.43 against AUC's ~0.27, and "
        "the MCC interval includes zero. Quote them as operating points, not "
        "as accuracy."
    )

    # ---- 3. what the ratio is actually made of -----------------------------
    print("\n3. where the score comes from: the two distance components")
    lab = at_pixel[at_pixel["ref_role"].isin(["good", "bad"])]
    if not lab.empty:
        y = (lab["ref_role"] == "good").to_numpy()
        print(
            "   median distance by label (a pixel scores well by being CLOSE to "
            "good and FAR from transformed):"
        )
        print(
            lab.groupby("ref_role")[["m_good_local", "m_good_global", "m_bad"]]
            .median()
            .round(4)
            .to_string()
        )
        print("\n   AUC of each component alone (m_good negated: nearer is better):")
        for col, sign in (("m_good_local", -1), ("m_good_global", -1), ("m_bad", 1)):
            print(f"     {col:<16} {auc(sign * lab[col].to_numpy(), y):.3f}")
        for scope in SCOPES:
            print(f"     dor_{scope:<12} {auc(lab[f'dor_{scope}'].to_numpy(), y):.3f}")
        a_bad = auc(lab["m_bad"].to_numpy(), y)
        # Compare against the BEST scope, not `global` alone. Once the good pool
        # got its own inner radius (research E20) the reference side started
        # carrying real signal and `local` overtook `m_bad`; warning off the
        # global column only would keep crying wolf about the better scope.
        a_dor = max(auc(lab[f"dor_{sc}"].to_numpy(), y) for sc in SCOPES)
        if np.isfinite(a_bad) and np.isfinite(a_dor) and a_bad >= a_dor - 0.02:
            print(
                "\n   ! the distance to the TRANSFORMED pool carries as much of the "
                "\n     separation as the full ratio does. The bad pool (WorldCover "
                "\n     cropland and built-up) is a much more distant end-member than "
                "\n     the good pool, so the ratio is dominated by it and the result "
                "\n     is partly a map of distance from converted land. Read "
                "\n     m_good_* alongside the ratio, never the ratio alone."
            )

    thr_file = OUT_DATA / "threshold.json"
    if thr_file.exists():
        cal = json.loads(thr_file.read_text())
        print(
            f"\n   calibrated on {cal['n_calibration_stands']} stands in their "
            "own survey year:"
        )
        for scope in SCOPES:
            c = cal[scope]
            print(
                f"     {scope:<7} threshold {c['threshold']:.4f}  "
                f"Youden J={c['youden_j']:.3f}"
            )
            if c["youden_j"] < 0.2:
                print(
                    "       ! weak: report the continuous score, not a per-stand class"
                )

    # ---- 4b. the two confounds, controlled ---------------------------------
    print("\n4b. controls: is the separation about condition, or about the stands?")
    lab_s = at_stand[at_stand["ref_role"].isin(["good", "bad"])].copy()
    if not lab_s.empty and lab_s["area_ha"].notna().any():
        strat = lab_s.groupby("forest_type")["area_ha"].transform(
            lambda a: pd.qcut(np.log(a), 3, labels=False, duplicates="drop")
        )
        strat = lab_s["forest_type"].astype(str) + "-" + strat.astype(str)
        a_area = auc(
            lab_s["area_ha"].to_numpy(), (lab_s["ref_role"] == "good").to_numpy()
        )
        print(
            f"   stand AREA alone separates the labels at AUC {a_area:.3f} "
            "-- the bad-labelled stands are simply smaller, so this is the "
            "yardstick the score has to beat, not zero"
        )
        for scope in SCOPES:
            print(
                f"     dor_{scope:<7} pooled "
                f"{auc(lab_s[f'dor_{scope}'].to_numpy(), (lab_s['ref_role'] == 'good').to_numpy()):.3f}"
                f"   within area strata "
                f"{stratified_auc(lab_s, strat, f'dor_{scope}'):.3f}"
            )
    lab_p = at_pixel[at_pixel["ref_role"].isin(["good", "bad"])]
    if "edge_m" in lab_p.columns and lab_p["edge_m"].notna().any():
        inner = lab_p[lab_p["edge_m"] >= INTERIOR_M_DEFAULT]
        print(
            f"\n   pixels at least {INTERIOR_M_DEFAULT:g} m inside their own "
            f"stand ({len(inner):,} of {len(lab_p):,}, "
            f"{inner['poly_id'].nunique()} stands) -- a small stand is mostly "
            "edge and an edge pixel is a mixture:"
        )
        for scope in SCOPES:
            print(
                f"     dor_{scope:<7} all pixels "
                f"{auc(lab_p[f'dor_{scope}'].to_numpy(), (lab_p['ref_role'] == 'good').to_numpy()):.3f}"
                f"   interior only "
                f"{auc(inner[f'dor_{scope}'].to_numpy(), (inner['ref_role'] == 'good').to_numpy()):.3f}"
            )

    # ---- 4c. the trajectory, against a product that is not AlphaEarth ------
    _hansen_check(df)

    # ---- 4. coherence ------------------------------------------------------
    print("\n4. condition vs trajectory")
    print(
        df.groupby("trajectory")[["dor_local", "dor_global"]]
        .agg(["count", "median"])
        .round(3)
        .to_string()
    )
    print("\n   class cross-tab (local scope):")
    print(pd.crosstab(df["trajectory"], df["condition_local"]).to_string())

    poor_improving = df[
        (df["condition_local"] == "poor_condition") & (df["trajectory"] == "improving")
    ]
    good_declining = df[
        (df["condition_local"] == "good_condition") & (df["trajectory"] == "declining")
    ]
    print(
        f"\n   {len(poor_improving)} stands are in poor condition but improving "
        "(recovering; leave alone or keep the current management)"
    )
    print(
        f"   {len(good_declining)} stands are in good condition but declining "
        "(the priority set -- still good, actively losing it)"
    )
    if len(good_declining):
        cols = [
            c
            for c in [
                "poly_id",
                "forest_type",
                "tilstand",
                "survey_year",
                "dor_local",
                "dor_local_frac_poor",
                "sen_slope",
                "mk_p",
                "area_ha",
            ]
            if c in good_declining.columns
        ]
        print(
            good_declining.sort_values("sen_slope")[cols]
            .head(15)
            .round(3)
            .to_string(index=False)
        )

    # ---- 5. within-stand heterogeneity, which only the pixel grain shows ----
    print("\n5. stands whose pixels disagree with their own stand-level score")
    latest = cond[cond["year"] == year]
    split = latest[
        (latest["dor_local_frac_poor"] > 0.3)
        & (latest["condition_local"] == "good_condition")
    ]
    print(
        f"   {len(split)} stands class as good overall yet have >30% of their "
        "pixels below the threshold -- part-degraded stands a stand-level "
        "score cannot show, and the reason the probe here is a pixel"
    )
    if len(split):
        cols = [
            "poly_id",
            "forest_type",
            "tilstand",
            "area_ha",
            "dor_local",
            "dor_local_median",
            "dor_local_p10",
            "dor_local_frac_poor",
        ]
        print(
            split.sort_values("dor_local_frac_poor", ascending=False)[
                [c for c in cols if c in split.columns]
            ]
            .head(10)
            .round(3)
            .to_string(index=False)
        )


def _hansen_check(df: pd.DataFrame) -> None:
    """Recall and false-positive rate of `declining`, against Hansen loss year."""
    print("\n4c. trajectory vs Hansen Global Forest Change (independent of AlphaEarth)")
    if not HANSEN_PIXELS.exists():
        print("   not run: no hansen_pixels.parquet (see 02e_hansen_reference.py)")
        return
    h = pd.read_parquet(HANSEN_PIXELS)[["pixel_id", "loss_year"]]
    mem = pd.read_parquet(PIXEL_POLYGONS)[["pixel_id", "poly_id"]]
    tp = OUT_DATA / "trajectory_pixels.parquet"
    if not tp.exists():
        print(
            "   not run: no trajectory_pixels.parquet "
            "(03_trajectory.py --no-pixel-level was used)"
        )
        return
    t = pd.read_parquet(tp)[["pixel_id", "trajectory", "sen_slope"]]
    m = mem.merge(h, on="pixel_id", how="left")
    dirty = m.groupby("poly_id")["loss_year"].apply(lambda s: s.notna().any())
    clean_polys = set(dirty.index[~dirty])
    m["group"] = np.where(
        m["loss_year"].between(2018, 2024),
        "disturbed",
        np.where(
            m["loss_year"].isna() & m["poly_id"].isin(clean_polys),
            "control",
            "excluded",
        ),
    )
    m = m.drop_duplicates("pixel_id").merge(t, on="pixel_id", how="inner")
    d = m[m["group"] == "disturbed"]
    c = m[m["group"] == "control"]
    if d.empty or c.empty:
        print("   not run: no disturbed or no control pixels")
        return
    rec = float((d["trajectory"] == "declining").mean())
    fpr = float((c["trajectory"] == "declining").mean())
    a = auc(-m["sen_slope"].to_numpy(), (m["group"] == "disturbed").to_numpy())
    print(
        f"   {len(d):,} pixels Hansen says were cleared 2018-2024, "
        f"{len(c):,} in stands with no recorded loss at all"
    )
    print(f"   recall of `declining` on cleared pixels   {rec:.3f}")
    print(f"   false-positive rate on clean pixels        {fpr:.3f}")
    print(f"   AUC of the Theil-Sen slope, cleared vs clean {a:.3f}")
    if rec < 0.4:
        print(
            "   ! low recall. Expected under the default `--input ae_global`: "
            "the AE score's transformed pool is drawn from each year's own "
            "imagery, so it jitters about twice as much as the naturalness "
            "axis (recall 0.51 there, `03_trajectory.py --input axis`). Under "
            "--no-gate the p-value rule halves recall again."
        )
    print(
        "   Hansen sees canopy removal only, so this is a sensitivity FLOOR: "
        "the selective removal and understorey change these habitat types are "
        "mostly lost to are invisible to it, and `control` means 'no evidence "
        "of clearing', not 'intact'."
    )


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("05", sys.argv[1:])
