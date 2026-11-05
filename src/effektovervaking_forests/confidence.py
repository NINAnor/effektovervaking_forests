"""Alert confidence: the low / medium / high tier a reviewer acts on.

An ordinal tier, deliberately not a calibrated probability, backed by a
continuous score that only sorts the review queue. Terms:

1. `n_confirm`: clear looks at the pixel after the break (saturates at
   N_CONFIRM_FULL); holds fresh alerts back until the record supports them.
2. `mag_ratio`: |magnitude| / gate.
3. Season: a cap to `low` for winter breaks or breaks after a long gap (the
   model was fitted across a hole in the data).
4. OPERA DIST-ALERT agreement: a small bonus, never a penalty (30 m, misses
   small events). `opera_agreement` takes any (lon, lat, date) table.

The forest-mask check is applied at publish time, not here. See
docs/architecture.md.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd

from . import config

# ── tunables ─────────────────────────────────────────────────────────────────
# Clear looks after the break at which confirmation saturates (about one
# growing season; not a fitted value).
N_CONFIRM_FULL = 12

# Magnitude ratio (|magnitude| / gate) at which the magnitude term saturates.
MAG_RATIO_FULL = 3.0

# Months with effectively no optical record at this latitude.
WINTER_MONTHS = (11, 12, 1, 2)

# A gap this long before the break means the fit spans a hole.
MAX_GAP_DAYS = 60

# Season-rule score ceiling; below LOW_MAX, so capped alerts stay "low".
SEASON_CAP = 0.30

# Additive bonus for independent agreement.
AGREEMENT_BONUS = 0.15

# OPERA is on a 30 m grid: match within one cell and a date window.
AGREE_RADIUS_M = 30.0
AGREE_WINDOW_DAYS = 90

# Tier boundaries on the continuous score.
LOW_MAX = 0.34
MEDIUM_MAX = 0.67

TIERS = ("low", "medium", "high")
TIER_CODE = {"low": 1, "medium": 2, "high": 3}

# Everything this module writes; cleared before re-scoring.
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

    The gap is measured to the last observation strictly BEFORE the break:
    S-CCD dates a break at an observation, so `<=` would always give zero.

    Uses DuckDB so the observation store is never fully materialised.
    """
    import duckdb

    obs_glob = str(Path(config.OBS_DIR) / aoi_name / "*.parquet")
    if not list(Path(config.OBS_DIR).joinpath(aoi_name).glob("*.parquet")):
        # No observation store: score on magnitude alone.
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
    """True where an OPERA DIST-ALERT sits within one cell and the date window."""
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

    # Earth-centred 3D coordinates so the radius is metres at any latitude.
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

    Returns a copy with the OUTPUT_COLS columns; every input to the score is
    kept, not just the tier.
    """
    df = breaks.reset_index(drop=True).copy()

    # Re-scoring an already-scored table: drop old outputs or the merge below
    # suffixes them (n_confirm_x / _y).
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

    # (1) confirmation; missing data counts as no evidence.
    n_conf = df["n_confirm"].fillna(0).clip(lower=0)
    confirm_term = (n_conf / N_CONFIRM_FULL).clip(upper=1.0)

    # (2) magnitude excess over the row's own gate.
    gate = df["gate"].replace(0, np.nan)
    df["mag_ratio"] = (df["magnitude"].abs() / gate).astype(float)
    mag_term = ((df["mag_ratio"] - 1.0) / (MAG_RATIO_FULL - 1.0)).clip(0.0, 1.0)
    mag_term = mag_term.fillna(0.0)

    score = 0.5 * confirm_term + 0.5 * mag_term

    # (4) agreement, applied before the cap so it cannot rescue a capped alert.
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
