"""Step 8 -- collect everything the report shows into one JSON file.

The report used to carry its numbers inline, copied by hand from run logs. That
worked until the pipeline changed underneath it, at which point the page quietly
described a configuration that no longer existed. This script reads the shipped
outputs instead, so re-running the pipeline and re-running this is enough to
bring the report up to date.

Everything here is computed from `outputs/data/`, at the deployed
configuration -- there is no separate research path and no hardcoded number.

Run:  forestmon condition step 08
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from effektovervaking_forests.condition.paths import (
    AEF_YEARS,
    CONDITION,
    OUT_DATA,
    OUT_REPORT,
    OUT_TIF,
    PIXEL_CONDITION,
)

GOOD = "god"
BAD = ["dårlig", "sværtRedusert"]
SCOPES = ["local", "global"]
CLASSES = ["god", "moderat", "dårlig", "sværtRedusert"]
N_BOOT = 400
MIN_BAD_PER_CELL = 3  # a stratum with one or two poor stands cannot be scored


# --------------------------------------------------------------------------
def auc(s, y) -> float:
    """Rank AUC with tie-averaged ranks; y True = good (the higher scores)."""
    from scipy.stats import rankdata

    s = np.asarray(s, float)
    y = np.asarray(y, bool)
    ok = np.isfinite(s)
    s, y = s[ok], y[ok]
    if len(s) == 0 or y.all() or not y.any():
        return float("nan")
    r = rankdata(s)
    n1, n0 = int(y.sum()), int((~y).sum())
    return float((r[y].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def block_ci(s, y, blocks, seed: int = 0) -> tuple[float, float]:
    """Resample whole 5 km blocks, not stands.

    286 stands in 39 localities are not 286 independent observations. A row-wise
    interval understates that by roughly the square root of the ratio.
    """
    rng = np.random.default_rng(seed)
    s, y, blocks = np.asarray(s, float), np.asarray(y, bool), np.asarray(blocks)
    uniq = np.unique(blocks)
    idx = {b: np.flatnonzero(blocks == b) for b in uniq}
    out = []
    for _ in range(N_BOOT):
        draw = np.concatenate([idx[b] for b in rng.choice(uniq, len(uniq))])
        a = auc(s[draw], y[draw])
        if np.isfinite(a):
            out.append(a)
    if not out:
        return float("nan"), float("nan")
    return float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))


def roc(s, y) -> dict:
    """A thinned ROC curve -- the report draws it, it does not integrate it."""
    s = np.asarray(s, float)
    y = np.asarray(y, bool)
    ok = np.isfinite(s)
    s, y = s[ok], y[ok]
    a = auc(s, y)
    ys = y[np.argsort(-s)]  # sort the labels by descending score
    tpr = np.concatenate([[0], np.cumsum(ys) / max(ys.sum(), 1)])
    fpr = np.concatenate([[0], np.cumsum(~ys) / max((~ys).sum(), 1)])
    step = max(1, len(fpr) // 220)
    keep = np.unique(np.concatenate([np.arange(0, len(fpr), step), [len(fpr) - 1]]))
    return {
        "fpr": np.round(fpr[keep], 4).tolist(),
        "tpr": np.round(tpr[keep], 4).tolist(),
        "auc": round(a, 5),
        "n_good": int(y.sum()),
        "n_bad": int((~y).sum()),
    }


def stratified(df: pd.DataFrame, col: str, key: str) -> dict:
    """AUC computed inside each stratum, weighted by its good-poor pair count."""
    aucs, wts, cells = [], [], 0
    for _, g in df.groupby(key):
        y = (g["label"] == "good").to_numpy()
        if y.sum() == 0 or (~y).sum() < MIN_BAD_PER_CELL:
            continue
        a = auc(g[col].to_numpy(), y)
        if not np.isfinite(a):
            continue
        aucs.append(a)
        wts.append(int(y.sum() * (~y).sum()))
        cells += 1
    return {
        "auc": round(float(np.average(aucs, weights=wts)), 5) if aucs else None,
        "cells": cells,
        "pairs": int(sum(wts)),
    }


def area_tertiles(df: pd.DataFrame) -> np.ndarray:
    """Tertiles of log area, cut per forest type so the two never mix."""
    lab = np.empty(len(df), dtype=object)
    for ft, g in df.groupby("forest_type"):
        q = pd.qcut(
            np.log(g["area_ha"].to_numpy()),
            3,
            labels=[f"{ft[:4]}-{i + 1}" for i in range(3)],
            duplicates="drop",
        )
        lab[df.index.get_indexer(g.index)] = q.astype(str)
    return lab


def hansen_check() -> dict | None:
    """The trajectory step against Hansen loss year -- the one outside check.

    Hansen Global Forest Change is derived from Landsat and uses nothing from
    AlphaEarth, so agreement with it is evidence and not circularity. It sees
    canopy removal only, which makes the recall below a floor rather than an
    estimate: the selective removal and understorey change these habitat types
    are mostly lost to leave the canopy standing.

    Same definitions as `05_validate.py`, so the two agree by construction.
    """
    h_path = OUT_DATA / "hansen_pixels.parquet"
    t_path = OUT_DATA / "trajectory_pixels.parquet"
    if not (h_path.exists() and t_path.exists()):
        return None
    h = pd.read_parquet(h_path, columns=["pixel_id", "loss_year"])
    mem = pd.read_parquet(
        OUT_DATA / "pixel_polygons.parquet", columns=["pixel_id", "poly_id"]
    )
    t = pd.read_parquet(t_path, columns=["pixel_id", "trajectory", "sen_slope"])
    m = mem.merge(h, on="pixel_id", how="left")
    dirty = m.groupby("poly_id")["loss_year"].apply(lambda s: s.notna().any())
    clean = set(dirty.index[~dirty])
    m["group"] = np.where(
        m["loss_year"].between(2018, 2024),
        "disturbed",
        np.where(
            m["loss_year"].isna() & m["poly_id"].isin(clean), "control", "excluded"
        ),
    )
    m = m.drop_duplicates("pixel_id").merge(t, on="pixel_id", how="inner")
    d, c = m[m["group"] == "disturbed"], m[m["group"] == "control"]
    if d.empty or c.empty:
        return None
    return {
        "n_cleared": int(len(d)),
        "n_clean": int(len(c)),
        "recall": round(float((d["trajectory"] == "declining").mean()), 4),
        "fpr": round(float((c["trajectory"] == "declining").mean()), 4),
        "auc": round(
            auc(-m["sen_slope"].to_numpy(), (m["group"] == "disturbed").to_numpy()), 4
        ),
    }


# --------------------------------------------------------------------------
def build(year: int) -> dict:
    cond = pd.read_parquet(CONDITION)
    traj = pd.read_parquet(OUT_DATA / "trajectory.parquet")
    thr = json.loads((OUT_DATA / "threshold.json").read_text())
    gate = json.loads((OUT_DATA / "trajectory_gate.json").read_text())

    now = cond[cond["year"] == year]
    D: dict = {
        "year": year,
        "years": list(AEF_YEARS),
        "n_polygons": int(now["poly_id"].nunique()),
        "poly_counts": now["forest_type"].value_counts().to_dict(),
        "gate": gate,
        "threshold": {
            s: {k: v for k, v in thr[s].items() if not isinstance(v, (list, dict))}
            for s in SCOPES
        },
    }

    # ---- the evaluation set: every labelled stand in its own survey year ----
    lab = cond[cond["label_age"] == 0].copy()
    ev = lab[lab["tilstand"].isin([GOOD] + BAD)].copy()
    ev["label"] = np.where(ev["tilstand"] == GOOD, "good", "bad")
    ev["area_tertile"] = area_tertiles(ev)
    ev["locality_and_size"] = ev["block"].astype(str) + "|" + ev["area_tertile"]
    y = (ev["label"] == "good").to_numpy()

    D["n_labelled"] = {"total": len(ev), "good": int(y.sum()), "bad": int((~y).sum())}
    D["tilstand_counts"] = {c: int((lab["tilstand"] == c).sum()) for c in CLASSES}

    # ---- ordering, distribution and ROC, per scope ----
    D["medians"], D["box"], D["roc"], D["auc_stand"] = {}, {}, {}, {}
    for sc in SCOPES:
        col = f"dor_{sc}"
        med = {c: lab.loc[lab["tilstand"] == c, col].median() for c in CLASSES}
        vals = [med[c] for c in CLASSES]
        D["medians"][sc] = {
            "values": {
                c: (None if pd.isna(v) else round(float(v), 4)) for c, v in med.items()
            },
            "counts": {
                c: int(lab.loc[lab["tilstand"] == c, col].notna().sum())
                for c in CLASSES
            },
            "monotone": bool(
                all(vals[i] <= vals[i - 1] + 1e-9 for i in range(1, len(vals)))
            ),
        }
        D["box"][sc] = []
        for c in CLASSES:
            v = lab.loc[lab["tilstand"] == c, col].dropna().to_numpy()
            if not len(v):
                continue
            D["box"][sc].append(
                {
                    "label": c,
                    "n": int(len(v)),
                    "q1": round(float(np.percentile(v, 25)), 5),
                    "med": round(float(np.median(v)), 5),
                    "q3": round(float(np.percentile(v, 75)), 5),
                    "lo": round(float(v.min()), 5),
                    "hi": round(float(v.max()), 5),
                    "values": np.round(v, 4).tolist(),
                }
            )

        D["roc"][sc] = {}
        D["auc_stand"][sc] = {}
        for ft in ["kalklindeskog", "olivinskog", "both"]:
            g = ev if ft == "both" else ev[ev["forest_type"] == ft]
            gy = (g["label"] == "good").to_numpy()
            if gy.sum() == 0 or (~gy).sum() == 0:
                continue
            D["roc"][sc][ft] = roc(g[col].to_numpy(), gy)
            lo, hi = block_ci(g[col].to_numpy(), gy, g["block"].to_numpy())
            D["auc_stand"][sc][ft] = {
                "auc": round(auc(g[col].to_numpy(), gy), 5),
                "lo": round(lo, 4),
                "hi": round(hi, 4),
                "n_good": int(gy.sum()),
                "n_bad": int((~gy).sum()),
            }

    # How wide the per-stand interval is, against how far apart the field
    # classes actually sit. If the first is larger than the second the score
    # ranks a population of stands but cannot resolve one of them.
    D["resolution"] = {}
    for sc in SCOPES:
        w = (now[f"ci_hi_{sc}"] - now[f"ci_lo_{sc}"]).median()
        m = D["medians"][sc]["values"]
        D["resolution"][sc] = {
            "ci_width_median": None if pd.isna(w) else round(float(w), 4),
            "god_minus_darlig": round(m["god"] - m["dårlig"], 4),
        }

    # ---- what the separation survives ----
    D["controls"] = {}
    for col in ["dor_local", "dor_global", "area_ha"]:
        D["controls"][col] = {
            "pooled": round(auc(ev[col].to_numpy(), y), 5),
            "within_locality": stratified(ev, col, "block"),
            "within_size": stratified(ev, col, "area_tertile"),
            "within_both": stratified(ev, col, "locality_and_size"),
        }

    rows = []
    for b, g in ev.groupby("block"):
        gy = (g["label"] == "good").to_numpy()
        if gy.sum() == 0 or (~gy).sum() < MIN_BAD_PER_CELL:
            continue
        rows.append(
            {
                "block": int(b),
                "forest_type": g["forest_type"].mode()[0],
                "good": int(gy.sum()),
                "bad": int((~gy).sum()),
                "pairs": int(gy.sum() * (~gy).sum()),
                "auc_local": round(auc(g["dor_local"].to_numpy(), gy), 4),
                "auc_global": round(auc(g["dor_global"].to_numpy(), gy), 4),
            }
        )
    D["locality_weight"] = sorted(rows, key=lambda r: -r["pairs"])
    D["blocks"] = {
        "total": int(cond["block"].nunique()),
        "with_poor": int(ev[ev["label"] == "bad"]["block"].nunique()),
        "with_both": len(rows),
    }

    # ---- the two distance components, so the ratio is never read alone ----
    pix = pd.read_parquet(
        PIXEL_CONDITION,
        columns=[
            "pixel_id",
            "year",
            "ref_role",
            "label_age",
            "m_good_local",
            "m_good_global",
            "m_bad",
            "dor_local",
            "dor_global",
        ],
    )
    p = pix[(pix["label_age"] == 0) & (pix["ref_role"].isin(["good", "bad"]))]
    py = (p["ref_role"] == "good").to_numpy()
    D["components"] = {
        k: round(
            auc(-p[k].to_numpy() if k.startswith("m_good") else p[k].to_numpy(), py), 4
        )
        for k in ["m_good_local", "m_good_global", "m_bad", "dor_local", "dor_global"]
    }
    D["n_pixel_years"] = int(len(pix))
    del pix, p

    # ---- classes, and how the two scopes disagree ----
    D["cond_counts"] = {
        sc: now[f"condition_{sc}"].fillna("no_data").value_counts().to_dict()
        for sc in SCOPES
    }
    xt = pd.crosstab(
        now["condition_local"].fillna("no_data"),
        now["condition_global"].fillna("no_data"),
    )
    D["scope_xtab"] = {
        "index": xt.index.tolist(),
        "columns": xt.columns.tolist(),
        "values": xt.values.tolist(),
    }

    D["traj_counts"] = traj["trajectory"].value_counts().to_dict()
    tb = pd.crosstab(traj["trajectory"], traj["forest_type"])
    D["traj_by_type"] = {
        "index": tb.index.tolist(),
        "columns": tb.columns.tolist(),
        "values": tb.values.tolist(),
    }

    j = now.merge(
        traj[["poly_id", "trajectory", "sen_slope", "mk_p", "sd"]],
        on="poly_id",
        how="left",
    )
    D["xtab"] = {}
    for sc in SCOPES:
        ct = pd.crosstab(j["trajectory"], j[f"condition_{sc}"].fillna("no_data"))
        cols = [
            c
            for c in ["good_condition", "indistinguishable", "poor_condition"]
            if c in ct.columns
        ]
        D["xtab"][sc] = {
            "index": ct.index.tolist(),
            "columns": cols,
            "values": ct[cols].values.tolist(),
        }

    D["priority"] = {}
    for sc in SCOPES:
        sel = j[
            (j[f"condition_{sc}"] == "good_condition")
            & (j["trajectory"] == "declining")
        ]
        D["priority"][sc] = [
            {
                "poly_id": r.poly_id,
                "ft": r.forest_type,
                "tilstand": r.tilstand,
                "dor": round(float(getattr(r, f"dor_{sc}")), 4),
                "slope": round(float(r.sen_slope), 4),
                "frac_poor": round(float(getattr(r, f"dor_{sc}_frac_poor")), 3),
                "area": round(float(r.area_ha), 3),
            }
            for r in sel.sort_values("sen_slope").itertuples()
        ]
        D["priority"][f"{sc}_recovering"] = int(
            (
                (j[f"condition_{sc}"] == "poor_condition")
                & (j["trajectory"] == "improving")
            ).sum()
        )

    # ---- four example stands, one per trajectory class ----
    cs = [f"c_{y}" for y in AEF_YEARS]
    ex = []
    for cls in ["declining", "stable", "fluctuating", "improving"]:
        g = traj[traj["trajectory"] == cls].dropna(subset=cs)
        if not len(g):
            continue
        key = g["sen_slope"].abs() if cls in ("declining", "improving") else g["sd"]
        r = g.loc[key.idxmax() if cls != "stable" else key.idxmin()]
        xs = np.array(AEF_YEARS, float)
        fit = [
            float(r["sen_slope"] * (xs[0] - xs.mean()) + np.mean([r[c] for c in cs])),
            float(r["sen_slope"] * (xs[-1] - xs.mean()) + np.mean([r[c] for c in cs])),
        ]
        ex.append(
            {
                "cls": cls,
                "poly_id": r["poly_id"],
                "forest_type": r["forest_type"],
                "tilstand": r["tilstand"],
                "sen_slope": round(float(r["sen_slope"]), 4),
                "mk_p": round(float(r["mk_p"]), 4),
                "sd": round(float(r["sd"]), 4),
                "c": [round(float(r[c]), 4) for c in cs],
                "fit": [round(v, 4) for v in fit],
            }
        )
    D["examples"] = ex

    D["hansen"] = hansen_check()

    # ---- what the run produced ----
    tifs = sorted(OUT_TIF.glob("*.tif"))
    gpkg = OUT_TIF.parent / "condition_results.gpkg"
    D["outputs"] = {
        "n_tif": len(tifs),
        "tif_mb": round(sum(t.stat().st_size for t in tifs) / 1e6, 1),
        "gpkg_mb": round(gpkg.stat().st_size / 1e6, 1) if gpkg.exists() else None,
        "n_cells": int(
            pd.read_parquet(
                OUT_DATA / "pixel_sites.parquet", columns=["pixel_id"]
            ).shape[0]
        ),
    }
    return D


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, default=max(AEF_YEARS))
    ap.add_argument("--out", type=Path, default=OUT_REPORT / "report_data.json")
    args = ap.parse_args(argv)

    def plain(o):
        """pandas nulls and numpy scalars, as JSON understands them."""
        if o is pd.NA or (isinstance(o, float) and not np.isfinite(o)):
            return None
        if isinstance(o, np.generic):
            return o.item()
        try:
            return None if pd.isna(o) else str(o)
        except (TypeError, ValueError):
            return str(o)

    D = build(args.year)
    D["run_date"] = dt.date.today().isoformat()
    blob = json.dumps(D, ensure_ascii=False, separators=(",", ":"), default=plain)
    args.out.write_text(blob, encoding="utf-8")
    print(f"{args.out}  ({args.out.stat().st_size / 1e3:.0f} kB)")

    # The page is a template with one placeholder; the data goes in here so the
    # published report can never describe a run other than the one on disk.
    tpl = Path(__file__).resolve().parent / "report_template.html"
    if tpl.exists():
        html = args.out.parent / "condition_report.html"
        html.write_text(
            tpl.read_text(encoding="utf-8").replace("__REPORT_DATA__", blob),
            encoding="utf-8",
        )
        print(f"{html}  ({html.stat().st_size / 1e3:.0f} kB)")
    print(
        f"  stand AUC   local {D['auc_stand']['local']['both']['auc']:.3f}"
        f"   global {D['auc_stand']['global']['both']['auc']:.3f}"
    )
    print(
        f"  within one locality  local "
        f"{D['controls']['dor_local']['within_locality']['auc']:.3f}"
        f"   global {D['controls']['dor_global']['within_locality']['auc']:.3f}"
    )
    print(
        f"  outputs  {D['outputs']['n_tif']} GeoTIFFs "
        f"({D['outputs']['tif_mb']} MB) + {D['outputs']['gpkg_mb']} MB GeoPackage"
    )


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("08", sys.argv[1:])
