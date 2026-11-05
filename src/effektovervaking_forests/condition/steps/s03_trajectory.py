"""Step 3 (= the user-facing STEP 1) -- trajectory classification, 2017-2025.

Z-score, Theil-Sen slope and Mann-Kendall test on the yearly nature type AE score
from step 04, so this step runs AFTER step 04 (needs `condition.parquet` and
`pixel_condition.parquet`). Declining/improving classes are gated at a stated
false-positive rate calibrated on Hansen-clean units (step 02e; without
`hansen_pixels.parquet` it falls back to the Mann-Kendall p-value).

Runs per polygon and, unless `--no-pixel-level`, per pixel.

Output: `outputs/data/trajectory.parquet`, `trajectory_pixels.parquet` and
`trajectory_gate.json`.

Run:  forestmon condition step 03
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

from effektovervaking_forests.condition.paths import (
    CONDITION,
    HANSEN_PIXELS,
    OUT_DATA,
    PIXEL_CONDITION,
    PIXEL_POLYGONS,
    POLYGONS,
    TRAJECTORY,
)
from effektovervaking_forests.condition.trend import (
    GATE_PCTL_DEFAULT,
    annual_matrix,
    calibrate_slope_gate,
    classify_trend,
    theil_sen_mann_kendall,
    zscore_by_group,
)

GATE_JSON = OUT_DATA / "trajectory_gate.json"
TRAJ_PIX = OUT_DATA / "trajectory_pixels.parquet"
# Percentile (of Hansen-clean sd) for the stable/fluctuating cut; a fixed 0.30 z
# cut would call most stands fluctuating on the AE score (METHODS.md).
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
        "--sd-pctl",
        type=float,
        default=SD_PCTL_AE,
        help="set the stable/fluctuating cut at this percentile of "
        "the yearly-value sd of Hansen-clean units, per grain, "
        "instead of the fixed --sd-stable. 81.4 keeps the share of "
        "Hansen-clean stands called fluctuating at the 18.6%% the "
        "earlier axis input had",
    )
    args = ap.parse_args(argv)
    _run_ae(args)


def _run_ae(args) -> None:
    """The trend on the nature type AE score from step 04's outputs."""
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
    # One series per pixel, typed by its first stand. Its yearly value is the mean
    # over claiming stands of that type; other-type claims are on another scale.
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
                    "input": "ae_global",
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
    (None, None) when 02e has not been run."""
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

    Returns (table, gate). The gate is calibrated on `clean` units, one per grain
    (stand slopes are less spread than pixel slopes).
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
    # Also carry the raw (un-standardised) yearly scores.
    raw = pd.DataFrame(mat, columns=[f"c_{y}" for y in years])
    return pd.concat([out, raw], axis=1), gate


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("03", sys.argv[1:])
