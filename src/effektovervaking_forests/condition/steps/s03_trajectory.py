"""Step 3 (= the user-facing STEP 1) -- trajectory classification, 2017-2025.

The RECOVER recipe -- z-score, Theil-Sen slope, Mann-Kendall test -- applied to
AlphaEarth instead of GPP and short-vegetation height, which no longer carry
recent years.

The one thing that recipe needs and AlphaEarth does not hand over is a scalar
per year. Since 2026-10 that scalar is the **AE score at the nature type scope**
from `04_condition.py` -- the stand's median cell score each year, and each
cell's own score at pixel grain -- so the trajectory and the condition class are
read off one number. This step therefore runs AFTER step 4.

`--input axis` restores the earlier scalar, a projection onto the *naturalness
axis* (the direction from the transformed reference cloud to the god-labelled
one, fitted once per forest type and held fixed; `condition.axis`). On the
Hansen check that axis finds cleared stands better (stand AUC 0.75 against 0.64)
and jitters half as much year to year on unchanged stands, because the AE
score's transformed pool is drawn from each year's own imagery. The AE score was
chosen for having one score everywhere; the cost is recorded in docs/METHODS.md.

The two clouds are the ones step 4 scores against, and they carry the same two
corrections (`condition/refs.py`, `condition/worldcover.py`): the good side is
each god-labelled stand **at its own survey year**, because `tilstand` describes
the day the surveyor stood there; the bad side is cropland and built-up from
WorldCover in a 3-8 km ring, because the field labels alone give barely 1,600
bad pixels across both forest types.

Runs at two grains, both written out:
  * per polygon  -- the stand's yearly score (the mean projection under
                    `--input axis`);
  * per pixel    -- the default here, since the layers overlap and a pixel
                    belongs to a set of polygons rather than to one
                    (`condition/membership.py`).

Output: `outputs/data/trajectory.parquet` (+ `trajectory_pixels.parquet`).

Run:  forestmon condition step 03
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

from effektovervaking_forests.condition.axis import annual_matrix, fit_axis, project
from effektovervaking_forests.condition.paths import (
    AEF_YEARS,
    BAD_MEMBERSHIP,
    BAD_PIXELS,
    BANDS,
    CONDITION,
    HANSEN_PIXELS,
    OUT_DATA,
    PIXEL_CONDITION,
    PIXEL_POLYGONS,
    PIXELS,
    POLY_OVERLAP,
    POLYGONS,
    TRAJECTORY,
)
from effektovervaking_forests.condition.refs import (
    PoolBuilder,
    anchor_donors,
    survey_year,
)
from effektovervaking_forests.condition.trend import (
    GATE_PCTL_DEFAULT,
    calibrate_slope_gate,
    classify_trend,
    theil_sen_mann_kendall,
    zscore_by_group,
)

AXES_JSON = OUT_DATA / "naturalness_axes.json"
GATE_JSON = OUT_DATA / "trajectory_gate.json"
TRAJ_PIX = OUT_DATA / "trajectory_pixels.parquet"
# Share of Hansen-clean stands the axis run called fluctuating at sd >= 0.30 z
# was 18.6%; the AE input's stable/fluctuating cut is set to keep that share.
SD_PCTL_AE = 81.4


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument(
        "--sd-stable",
        type=float,
        default=0.30,
        help="z-units; below this a non-significant series is 'stable'",
    )
    ap.add_argument(
        "--min-slope",
        type=float,
        default=0.0,
        help="z-units/yr; |Theil-Sen| below this is not a trend",
    )
    ap.add_argument(
        "--no-pixel-level",
        action="store_true",
        help="skip the per-pixel trend, which is the slow part",
    )
    ap.add_argument("--max-per-donor", type=int, default=60)
    ap.add_argument("--max-global", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--gate-pctl",
        type=float,
        default=GATE_PCTL_DEFAULT,
        help="the false-positive rate the declining/improving classes "
        "are calibrated to, as a percentile of the slopes of "
        "pixels Hansen says were never cleared. The default 2%% "
        "matches the shipped significance rule's observed rate "
        "while finding roughly twice as much real clearing "
        "(research T01-T04)",
    )
    ap.add_argument(
        "--no-gate",
        action="store_true",
        help="classify by Mann-Kendall p < alpha instead, as "
        "published. Correctly calibrated and half as sensitive",
    )
    ap.add_argument(
        "--input",
        choices=("ae_global", "axis"),
        default="ae_global",
        help="the yearly scalar the trend runs on: `ae_global`, the "
        "AE score at the nature type scope from 04_condition.py "
        "(stand median; each cell's own score at pixel grain), "
        "or `axis`, the projection onto the naturalness axis "
        "used until 2026-10",
    )
    ap.add_argument(
        "--sd-pctl",
        type=float,
        default=None,
        help="set the stable/fluctuating cut at this percentile of "
        "the yearly-value sd of Hansen-clean units, per grain, "
        "instead of the fixed --sd-stable. Default 81.4 under "
        "`--input ae_global`: the AE score jitters about twice "
        "as much as the axis, so 0.30 z called 73%% of stands "
        "fluctuating; 81.4 keeps the axis run's share of "
        "Hansen-clean stands that are fluctuating (18.6%%)",
    )
    args = ap.parse_args(argv)
    if args.sd_pctl is None and args.input == "ae_global":
        args.sd_pctl = SD_PCTL_AE

    if args.input == "ae_global":
        _run_ae(args)
        return

    import geopandas as gpd

    polygons = pd.read_parquet(POLYGONS)
    polygons["survey_year"] = survey_year(polygons)
    pixels = pd.read_parquet(PIXELS)
    membership = pd.read_parquet(PIXEL_POLYGONS)
    roles = pd.read_parquet(OUT_DATA / "pixel_roles.parquet")
    poly_geo = gpd.read_parquet(OUT_DATA / "polygons_geo.parquet")
    years = sorted(pixels["year"].unique())
    print(
        f"{len(pixels):,} pixel-years | years {years[0]}-{years[-1]} "
        f"| {membership.poly_id.nunique()} polygons, "
        f"{pixels.pixel_id.nunique():,} distinct pixels"
    )

    poly_type = polygons.set_index("poly_id")["forest_type"]
    if "block" not in polygons.columns:
        raise SystemExit(
            "polygons.parquet has no `block` column -- re-run 00_build_polygons.py"
        )
    builder = PoolBuilder(
        pixels=pixels,
        bad_pixels=pd.read_parquet(BAD_PIXELS),
        donors=anchor_donors(polygons, roles, membership, tuple(AEF_YEARS)),
        membership=membership,
        pixel_roles=roles,
        bad_membership=pd.read_parquet(BAD_MEMBERSHIP),
        neighbours=pd.read_parquet(POLY_OVERLAP),
        centroids=pd.DataFrame(
            {
                "poly_id": poly_geo["poly_id"],
                "cx": poly_geo.geometry.centroid.x,
                "cy": poly_geo.geometry.centroid.y,
            }
        ),
        poly_type=poly_type,
        blocks=polygons.set_index("poly_id")["block"],
        max_per_donor=args.max_per_donor,
        max_global=args.max_global,
        seed=args.seed,
    )

    # ---- naturalness axis, one per forest type, held fixed ------------------
    axes, axis_report = {}, []
    for ftype in sorted(poly_type.dropna().unique()):
        good, bad = builder.axis_pools(ftype, tuple(years))
        if len(good) == 0 or len(bad) == 0:
            print(f"  ! {ftype}: no axis (good={len(good)}, bad={len(bad)})")
            continue
        w = fit_axis(good, bad)
        axes[ftype] = w
        sep = float(project(good, w).mean() - project(bad, w).mean())
        axis_report.append(
            {
                "forest_type": ftype,
                "n_good_px": len(good),
                "n_bad_px": len(bad),
                "separation": sep,
            }
        )
        print(
            f"  axis {ftype:<14} good={len(good):>7,}px bad={len(bad):>7,}px "
            f"separation={sep:+.4f}"
        )
    AXES_JSON.write_text(
        json.dumps(
            {"axes": {k: v.tolist() for k, v in axes.items()}, "summary": axis_report},
            indent=2,
        )
    )

    # A separation near zero means the two label classes do not occupy different
    # regions of embedding space, and every trend read off that axis is noise.
    weak = [a for a in axis_report if abs(a["separation"]) < 0.01]
    if weak:
        print(
            "  ! weak separation for "
            + ", ".join(a["forest_type"] for a in weak)
            + " -- the axis barely distinguishes the label classes; treat its "
            "trajectories as unreliable and see 05_validate.py"
        )

    # ---- project every pixel-year onto its type's axis ----------------------
    # A pixel is projected ONCE, on its own type's axis, however many polygons
    # claim it -- the old per-(polygon, pixel) table projected shared ground
    # repeatedly and weighted it by how many layers happened to map it.
    px_type = (
        membership.assign(forest_type=membership["poly_id"].map(poly_type))
        .drop_duplicates("pixel_id")
        .set_index("pixel_id")["forest_type"]
    )
    pixels["forest_type"] = pixels["pixel_id"].map(px_type)
    proj = np.full(len(pixels), np.nan)
    for ftype, w in axes.items():
        m = (pixels["forest_type"] == ftype).to_numpy()
        proj[m] = project(pixels.loc[m, list(BANDS)].to_numpy(), w)
    pixels["c"] = proj

    # ---- polygon grain -----------------------------------------------------
    # The join to membership is what restores the pixels overlapping polygons
    # used to lose to each other.
    per_poly = pixels[["pixel_id", "year", "c"]].merge(membership, on="pixel_id")
    per_poly["forest_type"] = per_poly["poly_id"].map(poly_type)
    poly_year = (
        per_poly.groupby(["poly_id", "forest_type", "year"])["c"]
        .agg(["mean", "size"])
        .reset_index()
        .rename(columns={"mean": "c", "size": "n_pixels"})
    )
    # ---- the undisturbed reference the class gate is calibrated on ---------
    # Hansen loss year, sampled independently of AlphaEarth (02e). A stand or a
    # pixel counts as undisturbed only if the WHOLE stand is Hansen-clean: a
    # pixel 20 m from a clearcut is not a control, and Hansen's 30 m cells make
    # the pixel-level distinction unreliable.
    clean_poly, clean_pixel = _clean_reference(membership, poly_type)
    if args.no_gate:
        clean_poly = clean_pixel = None
        print(
            "\n! classifying by Mann-Kendall p-value. That rule is correctly "
            "calibrated and finds about half as much real clearing as the "
            "slope gate at the same false-positive rate (research T04)."
        )

    traj, gate_poly = _trend_table(
        poly_year, ["poly_id", "forest_type"], years, args, clean=clean_poly
    )
    traj = traj.merge(
        polygons[
            ["poly_id", "layer", "forest_type", "ref_role", "tilstand", "area_ha"]
        ].drop(columns="forest_type"),
        on="poly_id",
        how="left",
    )
    traj.to_parquet(TRAJECTORY, index=False)
    print(f"\n{len(traj)} polygons -> {TRAJECTORY.name}")
    if gate_poly:
        print(
            f"  stand-grain gate: declining below {gate_poly[0]:+.4f} z/yr, "
            f"improving above {gate_poly[1]:+.4f} z/yr "
            f"({args.gate_pctl:g}% of Hansen-clean stands each side)"
        )
    print(pd.crosstab(traj["trajectory"], traj["forest_type"]).to_string())
    print("\ntrajectory x field condition label (tilstand):")
    print(
        pd.crosstab(traj["trajectory"], traj["tilstand"].fillna("(none)")).to_string()
    )

    # ---- pixel grain -------------------------------------------------------
    if not args.no_pixel_level:
        px = pixels[["pixel_id", "forest_type", "year", "c"]].dropna(subset=["c"])
        tp, gate_px = _trend_table(
            px, ["pixel_id", "forest_type"], years, args, clean=clean_pixel
        )
        tp.to_parquet(TRAJ_PIX, index=False)
        print(f"\n{len(tp):,} pixels -> {TRAJ_PIX.name}")
        if gate_px:
            print(
                f"  pixel-grain gate: declining below {gate_px[0]:+.4f} z/yr, "
                f"improving above {gate_px[1]:+.4f} z/yr"
            )
            GATE_JSON.write_text(
                json.dumps(
                    {
                        "pctl": args.gate_pctl,
                        "input": args.input,
                        "stand": list(gate_poly or []),
                        "pixel": list(gate_px),
                    },
                    indent=2,
                )
            )
        print(tp["trajectory"].value_counts().to_string())


def _run_ae(args) -> None:
    """The trend on the nature type AE score, read from step 4's outputs."""
    for p in (CONDITION, PIXEL_CONDITION):
        if not p.exists():
            raise SystemExit(
                f"{p.name} is missing -- run 04_condition.py first; "
                "the trajectory is read off its nature type score"
            )
    polygons = pd.read_parquet(POLYGONS)
    membership = pd.read_parquet(PIXEL_POLYGONS, columns=["pixel_id", "poly_id"])
    poly_type = polygons.set_index("poly_id")["forest_type"]
    stands = pd.read_parquet(
        CONDITION, columns=["poly_id", "forest_type", "year", "n_pixels", "dor_global"]
    )
    years = sorted(stands["year"].unique())
    poly_year = stands.rename(columns={"dor_global": "c"}).dropna(subset=["c"])
    print(
        f"{len(poly_year):,} stand-years of the nature type AE score, "
        f"{years[0]}-{years[-1]}"
    )

    clean_poly, clean_pixel = _clean_reference(membership, poly_type)
    if args.no_gate:
        clean_poly = clean_pixel = None
    traj, gate_poly = _trend_table(
        poly_year, ["poly_id", "forest_type"], years, args, clean=clean_poly
    )
    traj = traj.merge(
        polygons[["poly_id", "layer", "ref_role", "tilstand", "area_ha"]],
        on="poly_id",
        how="left",
    )
    traj.to_parquet(TRAJECTORY, index=False)
    print(f"\n{len(traj)} polygons -> {TRAJECTORY.name}")
    if gate_poly:
        print(
            f"  stand-grain gate: declining below {gate_poly[0]:+.4f} z/yr, "
            f"improving above {gate_poly[1]:+.4f} z/yr "
            f"({args.gate_pctl:g}% of Hansen-clean stands each side); "
            f"fluctuating at sd >= {args.sd_used[0]:.3f} z"
        )
    print(pd.crosstab(traj["trajectory"], traj["forest_type"]).to_string())

    if args.no_pixel_level:
        return
    # A pixel is one series however many stands claim it, on its first stand's
    # forest type as under the axis input. Overlapping stands of that type score
    # it against slightly different pools (each stand's own 3 km exclusion), so
    # its yearly value is the mean over them; a claim from the other forest type
    # is a different scale and is left out.
    px_type = (
        membership.drop_duplicates("pixel_id")
        .set_index("pixel_id")["poly_id"]
        .map(poly_type)
    )
    pc = pd.read_parquet(
        PIXEL_CONDITION, columns=["pixel_id", "year", "forest_type", "dor_global"]
    )
    pc = pc[pc["forest_type"].to_numpy() == px_type.reindex(pc["pixel_id"]).to_numpy()]
    px = (
        pc.groupby(["pixel_id", "forest_type", "year"], sort=False, observed=True)[
            "dor_global"
        ]
        .mean()
        .rename("c")
        .reset_index()
        .dropna(subset=["c"])
    )
    tp, gate_px = _trend_table(
        px, ["pixel_id", "forest_type"], years, args, clean=clean_pixel
    )
    tp.to_parquet(TRAJ_PIX, index=False)
    print(f"\n{len(tp):,} pixels -> {TRAJ_PIX.name}")
    if gate_px:
        print(
            f"  pixel-grain gate: declining below {gate_px[0]:+.4f} z/yr, "
            f"improving above {gate_px[1]:+.4f} z/yr"
        )
        GATE_JSON.write_text(
            json.dumps(
                {
                    "pctl": args.gate_pctl,
                    "input": args.input,
                    "stand": list(gate_poly or []),
                    "pixel": list(gate_px),
                    "sd_pctl": args.sd_pctl,
                    "sd_stable": {"stand": args.sd_used[0], "pixel": args.sd_used[1]},
                },
                indent=2,
            )
        )
    print(tp["trajectory"].value_counts().to_string())


def _clean_reference(membership: pd.DataFrame, poly_type: pd.Series):
    """(clean stand ids, clean pixel ids) -- no Hansen forest loss in any year.

    Returns (None, None) when 02e has not been run, in which case the caller
    falls back to the significance rule rather than inventing a gate.
    """
    if not HANSEN_PIXELS.exists():
        print(
            "\n! no hansen_pixels.parquet -- run 02e_hansen_reference.py to "
            "calibrate the trajectory classes against an independent "
            "disturbance product. Falling back to the Mann-Kendall p-value."
        )
        return None, None
    h = pd.read_parquet(HANSEN_PIXELS)[["pixel_id", "loss_year"]]
    m = membership.merge(h, on="pixel_id", how="left")
    dirty = m.groupby("poly_id")["loss_year"].apply(lambda s: s.notna().any())
    clean_polys = set(dirty.index[~dirty])
    clean_pixels = set(
        m.loc[m["poly_id"].isin(clean_polys) & m["loss_year"].isna(), "pixel_id"]
    )
    print(
        f"\nHansen reference: {len(clean_polys)} of {dirty.size} stands have "
        f"no recorded forest loss in any year, giving {len(clean_pixels):,} "
        "undisturbed pixels to calibrate the class gate on"
    )
    return clean_polys, clean_pixels


def _trend_table(
    long: pd.DataFrame, keys: list[str], years, args, clean: set | None = None
):
    """z-score within forest type, then Theil-Sen + Mann-Kendall on each series.

    Returns (table, gate). The gate is calibrated on `clean` -- the units an
    independent product says were never cleared -- so the `declining` and
    `improving` classes have a stated false-positive rate instead of relying on
    a p-value that is calibrated but underpowered. One gate per grain: a
    polygon-mean series is smoother than a pixel one and its slopes are
    correspondingly less spread, so a pixel-calibrated cut applied to stands
    would flag almost nothing.
    """
    key_df, mat = annual_matrix(long, "c", years, keys)
    z = zscore_by_group(mat, key_df["forest_type"].to_numpy())
    feat = theil_sen_mann_kendall(z)
    gate = None
    if clean:
        mask = key_df[keys[0]].isin(clean).to_numpy()
        slopes = feat.loc[mask, "sen_slope"].to_numpy()
        try:
            gate = calibrate_slope_gate(slopes, args.gate_pctl)
        except ValueError as exc:
            print(f"  ! {exc}; falling back to the significance rule")
    sd_stable = args.sd_stable
    if clean and args.sd_pctl is not None:
        sd_clean = feat.loc[key_df[keys[0]].isin(clean).to_numpy(), "sd"].to_numpy()
        sd_stable = float(np.nanpercentile(sd_clean, args.sd_pctl))
    args.sd_used = getattr(args, "sd_used", []) + [sd_stable]
    feat["trajectory"] = classify_trend(
        feat,
        alpha=args.alpha,
        sd_stable=sd_stable,
        min_slope=args.min_slope,
        gate=gate,
    )
    out = pd.concat([key_df.reset_index(drop=True), feat], axis=1)
    # Carry the un-standardised level too: the z-scored slope says how fast, the
    # raw value (AE score, or axis projection) says relative to what.
    raw = pd.DataFrame(mat, columns=[f"c_{y}" for y in years])
    return pd.concat([out, raw], axis=1), gate


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("03", sys.argv[1:])
