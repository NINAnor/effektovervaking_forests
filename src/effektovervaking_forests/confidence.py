"""Alert confidence — the tier a reviewer and an end user actually act on.

WHY THIS EXISTS
---------------
`alerts.py` gates a break on magnitude and stops there. That is a yes/no on one
number, and this project's own label study says one number is not enough: the
gate was fitted on 15 loss positives, so its confidence interval is wide, and
`operational/README.md` names two further reasons a single break is not a
reliable claim — there is no confirmation logic, and alerts raised on the first
observation after the November-February optical gap "deserve their own
confidence class".

This module is that class. It is deliberately *not* a calibrated probability: a
probability fitted on 15 positives would be false precision, and a reviewer acts
on tiers anyway. It publishes an ordinal — low / medium / high — backed by a
continuous score that exists only to sort the review queue.

THE COMPONENTS, AND WHY EACH ONE
--------------------------------
1. **Confirming observations** (`n_confirm`) — clear looks at the pixel *after*
   the break. This is the strongest available signal and it is the convention
   GLAD and RADD users already read ("low = single detection, high = confirmed
   by later observations"), so it needs no explaining to anyone who has used a
   forest alert product.

   Note what S-CCD already guarantees: `rec_cg` holds only CONFIRMED segments,
   so every break here already survived `conse` consecutive anomalous looks.
   This term therefore does not separate old breaks from each other — a 2019
   break has hundreds of subsequent observations and saturates. That is correct
   behaviour: the term exists to hold *fresh* alerts back until the record
   supports them, which is exactly where an operational false positive hurts.

2. **Magnitude excess** (`mag_ratio`) — |magnitude| / gate. A break at three
   times the gate is a different claim from one that scraped past it, and the
   alert table already records the gate on every row so the ratio is honest even
   if the gate is re-tuned later.

3. **Season** — a cap, not a score. At 58-61 degrees north the optical record
   effectively stops from November to February. A break dated in that window, or
   one preceded by a long observation gap, was fitted across a hole in the data,
   and no magnitude makes that trustworthy. So it clamps the result to `low`
   rather than subtracting from it.

4. **Independent agreement** — OPERA DIST-ALERT, extracted separately in this
   project. An alert corroborated by a different sensor chain and a different
   algorithm is worth more than one that is not. It is a bonus, never a
   requirement: OPERA is 30 m and misses small events, so absence of agreement
   is not evidence of absence and must not push an alert down.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
Hansen (annual, needs Earth Engine, and cannot corroborate anything in the
current year) and RADD Europe (only the NRT window is public, and its Earth
Engine map ids expire) are both unavailable as a stable operational join. The
agreement term is written to take any table of (lon, lat, date) so either can be
added later without touching the rest.

Forest-mask agreement is applied in the publish step, not here: an alert outside
the forest baseline is still a real detection, and hiding it would cost us the
false-positive evidence the review queue exists to collect.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

from . import config

# ── tunables, all in one place ───────────────────────────────────────────────
# Clear observations after the break at which the confirmation term saturates.
# 12 is roughly one Norwegian growing season of usable looks, not a fitted value.
N_CONFIRM_FULL = 12

# Magnitude ratio (|magnitude| / gate) at which the magnitude term saturates.
MAG_RATIO_FULL = 3.0

# Months in which the optical record is effectively absent at this latitude.
WINTER_MONTHS = (11, 12, 1, 2)

# A gap this long before the break means the model fitted across a hole.
MAX_GAP_DAYS = 60

# Score ceiling imposed by the season rule. Below LOW_MAX by construction, so a
# capped alert can never be published above "low" whatever else it scores.
SEASON_CAP = 0.30

# Bonus for an independent product agreeing. Additive and small: agreement is
# corroboration, not proof, and its absence is not evidence.
AGREEMENT_BONUS = 0.15

# OPERA DIST-ALERT is on a 30 m grid; our pixels are 10 m. A match must be
# within one OPERA cell and within a window that allows for the two products
# dating the same event differently.
AGREE_RADIUS_M = 30.0
AGREE_WINDOW_DAYS = 90

# Tier boundaries on the continuous score.
LOW_MAX = 0.34
MEDIUM_MAX = 0.67

TIERS = ("low", "medium", "high")
TIER_CODE = {"low": 1, "medium": 2, "high": 3}

# Everything this module writes. Named once so re-scoring can clear its own
# previous output rather than colliding with it.
OUTPUT_COLS = (
    "n_confirm",
    "gap_before_days",
    "mag_ratio",
    "season_flag",
    "agree_opera",
    "conf_score",
    "confidence",
)


def tier(score: float) -> str:
    if not np.isfinite(score):
        return "low"
    if score < LOW_MAX:
        return "low"
    if score < MEDIUM_MAX:
        return "medium"
    return "high"


# ── per-pixel observation statistics ─────────────────────────────────────────


def observation_stats(aoi_name: str, breaks: pd.DataFrame) -> pd.DataFrame:
    """For each (pix_id, break_ordinal): clear looks after, and the gap before.

    The gap is measured to the last observation STRICTLY BEFORE the break. S-CCD
    dates a break at the first observation that confirms it, so an observation
    always falls on `break_ts` itself; using `<=` measures zero every time and
    the season rule's gap arm never fires. What the rule is actually asking is
    "how blind were we in the run-up to this", which is the previous look.

    Read with DuckDB rather than `obs.load`, which materialises the whole store.
    Only two columns are needed and the join is a range condition per pixel, so
    the query pushes down to the parquet and never holds the observation table
    in memory. The store is ~350 MB across the AOI; the answer is a few thousand
    rows.
    """
    import duckdb

    obs_glob = str(Path(config.OBS_DIR) / aoi_name / "*.parquet")
    if not list(Path(config.OBS_DIR).joinpath(aoi_name).glob("*.parquet")):
        # No observation store (a fresh AOI, or a test fixture). Every alert
        # falls back to magnitude alone rather than silently scoring zero.
        out = breaks[["pix_id", "break_ordinal"]].copy()
        out["n_confirm"] = np.nan
        out["gap_before_days"] = np.nan
        return out

    keys = breaks[["pix_id", "break_ordinal"]].drop_duplicates().copy()
    keys["break_ts"] = [dt.date.fromordinal(int(o)) for o in keys["break_ordinal"]]

    con = duckdb.connect()
    con.register("keys", keys)
    q = f"""
    WITH o AS (
        SELECT pix_id, CAST(datetime AS DATE) AS d
        FROM read_parquet('{obs_glob}')
        WHERE clear
    )
    SELECT k.pix_id,
           k.break_ordinal,
           COUNT(*) FILTER (WHERE o.d > k.break_ts)                  AS n_confirm,
           DATE_DIFF('day', MAX(o.d) FILTER (WHERE o.d < k.break_ts),
                            ANY_VALUE(k.break_ts))                   AS gap_before_days
    FROM keys k LEFT JOIN o USING (pix_id)
    GROUP BY k.pix_id, k.break_ordinal
    """
    out = con.execute(q).df()
    con.close()
    return out


# ── independent agreement ────────────────────────────────────────────────────


def _ecef(lon: np.ndarray, lat: np.ndarray, r: float = 6_371_008.8) -> np.ndarray:
    """Points on a sphere of the mean Earth radius, in metres."""
    lo, la = np.radians(lon), np.radians(lat)
    return np.c_[
        r * np.cos(la) * np.cos(lo), r * np.cos(la) * np.sin(lo), r * np.sin(la)
    ]


def opera_agreement(
    breaks: pd.DataFrame,
    opera_path: str | Path | None = None,
    radius_m: float = AGREE_RADIUS_M,
    window_days: int = AGREE_WINDOW_DAYS,
) -> pd.Series:
    """True where an OPERA DIST-ALERT sits within one cell and one season.

    Matched on both space and time. Space alone would corroborate a 2026 alert
    with a 2021 OPERA detection, which is not agreement about anything.
    """
    idx = pd.Index(range(len(breaks)))
    p = Path(opera_path) if opera_path is not None else config.OPERA_PARQUET
    if not p.exists():
        return pd.Series(False, index=idx)

    try:
        op = pd.read_parquet(p, columns=["lon", "lat", "veg_dist_date"])
    except (ValueError, KeyError):
        return pd.Series(False, index=idx)
    op = op.dropna(subset=["lon", "lat", "veg_dist_date"])
    if op.empty:
        return pd.Series(False, index=idx)

    from scipy.spatial import cKDTree

    # Earth-centred 3D coordinates, so the radius is metres everywhere. A local
    # equirectangular scale from the mean latitude was fine for kalklindeskog
    # (59-61 N) but olivinskog runs 58-70 N, where cos(lat) falls from 0.52 to
    # 0.34 and a 30 m radius would be 20-45 m east-west depending on the stand.
    # At 30 m the chord equals the arc to well under a millimetre.
    tree = cKDTree(_ecef(op["lon"].to_numpy(), op["lat"].to_numpy()))
    qpts = _ecef(breaks["lon"].to_numpy(), breaks["lat"].to_numpy())
    neighbours = tree.query_ball_point(qpts, r=radius_m)

    op_date = pd.to_datetime(op["veg_dist_date"]).to_numpy()
    bk_date = pd.to_datetime(breaks["break_date"]).to_numpy()
    window = np.timedelta64(int(window_days), "D")

    agree = np.zeros(len(breaks), dtype=bool)
    for i, nb in enumerate(neighbours):
        if not nb:
            continue
        agree[i] = bool(np.any(np.abs(op_date[nb] - bk_date[i]) <= window))
    return pd.Series(agree, index=idx)


# ── the score ────────────────────────────────────────────────────────────────


def score_breaks(
    aoi_name: str, breaks: pd.DataFrame, use_agreement: bool = True
) -> pd.DataFrame:
    """Attach confidence columns to an alert-shaped frame.

    Returns a copy with `n_confirm`, `gap_before_days`, `mag_ratio`,
    `season_flag`, `agree_opera`, `conf_score` and `confidence`. Every input to
    the score is kept, not just the verdict, so a reviewer disputing a tier can
    see which term produced it and a re-tune does not need a re-run.
    """
    df = breaks.reset_index(drop=True).copy()

    # Scoring is re-run on an ALREADY-SCORED table every cycle (see
    # `alerts.rescore`). Without dropping the previous answer first, the merge
    # below suffixes into n_confirm_x / n_confirm_y and every downstream lookup
    # raises KeyError -- on the second run, never the first, which is the kind
    # of bug that reaches production.
    df = df.drop(columns=[c for c in OUTPUT_COLS if c in df.columns])

    if df.empty:
        for c in ("n_confirm", "gap_before_days", "mag_ratio", "conf_score"):
            df[c] = pd.Series(dtype="float64")
        df["season_flag"] = pd.Series(dtype="object")
        df["agree_opera"] = pd.Series(dtype="bool")
        df["confidence"] = pd.Series(dtype="object")
        return df

    stats = observation_stats(aoi_name, df)
    df = df.merge(stats, on=["pix_id", "break_ordinal"], how="left")

    # (1) confirmation. Missing observation data is treated as "no confirmation
    # evidence" (0), not as full marks -- unknown must not read as confirmed.
    n_conf = df["n_confirm"].fillna(0).clip(lower=0)
    confirm_term = (n_conf / N_CONFIRM_FULL).clip(upper=1.0)

    # (2) magnitude excess over whatever gate this row was judged against.
    gate = df["gate"].replace(0, np.nan)
    df["mag_ratio"] = (df["magnitude"].abs() / gate).astype(float)
    mag_term = ((df["mag_ratio"] - 1.0) / (MAG_RATIO_FULL - 1.0)).clip(0.0, 1.0)
    mag_term = mag_term.fillna(0.0)

    score = 0.5 * confirm_term + 0.5 * mag_term

    # (4) agreement, before the cap: corroboration must not rescue an alert the
    # season rule has disqualified, because the season problem is about our
    # evidence, not about whether something happened.
    if use_agreement:
        df["agree_opera"] = opera_agreement(df).to_numpy()
    else:
        df["agree_opera"] = False
    score = np.where(df["agree_opera"], np.minimum(score + AGREEMENT_BONUS, 1.0), score)

    # (3) season cap.
    bmonth = pd.to_datetime(df["break_date"]).dt.month
    winter = bmonth.isin(WINTER_MONTHS)
    long_gap = df["gap_before_days"].fillna(0) > MAX_GAP_DAYS
    df["season_flag"] = np.select(
        [winter & long_gap, winter, long_gap],
        ["winter_and_gap", "winter", "observation_gap"],
        default="",
    )
    capped = winter | long_gap
    score = np.where(capped, np.minimum(score, SEASON_CAP), score)

    df["conf_score"] = np.round(score.astype(float), 4)
    df["confidence"] = [tier(s) for s in df["conf_score"]]
    return df


def summary(df: pd.DataFrame) -> pd.DataFrame:
    """Tier counts with the mean of each term, for sanity-checking a run."""
    if df.empty or "confidence" not in df:
        return pd.DataFrame()
    g = df.groupby("confidence")
    out = pd.DataFrame(
        {
            "n": g.size(),
            "mean_score": g["conf_score"].mean().round(3),
            "mean_n_confirm": g["n_confirm"].mean().round(1),
            "mean_mag_ratio": g["mag_ratio"].mean().round(2),
            "pct_agree_opera": (g["agree_opera"].mean() * 100).round(1),
        }
    )
    return out.reindex([t for t in TIERS if t in out.index])
