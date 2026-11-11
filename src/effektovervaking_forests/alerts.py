"""Turning S-CCD breaks into alerts.

A break is not an alert: the magnitude gate is applied here, with the threshold
recorded on every row, rather than inside the detector. Alerts are append-only
with a lifecycle status, so a human verdict is never overwritten by a later run.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from . import confidence as conf_mod
from . import config, store

# Magnitude gate (NBR units). Weaker breaks are stored as "below_gate", not
# dropped, so re-gating does not require re-running the model.
LOSS_GATE = 0.075

# A human verdict outranks anything a re-run computes.
REVIEWED = ("verified", "rejected")

# Written by `confidence.score_breaks`; every input to the tier is stored.
CONF_COLS = (
    "n_confirm",
    "gap_before_days",
    "mag_ratio",
    "season_flag",
    "agree_opera",
    "conf_score",
    "confidence",
)


def alerts_path(aoi_name: str):
    return config.ALERTS_DIR / f"{aoi_name}_alerts.parquet"


def load(aoi_name: str) -> pd.DataFrame:
    p = alerts_path(aoi_name)
    if not p.exists():
        return pd.DataFrame(
            columns=[
                "alert_id",
                "aoi",
                "pix_id",
                "poly_id",
                "lon",
                "lat",
                "break_date",
                "break_year",
                "magnitude",
                "kind",
                "gate",
                "detected_at",
                "detection_lag_days",
                "nrt_mode",
                "status",
                "recipe_version",
                *CONF_COLS,
            ]
        )
    return pd.read_parquet(p)


def alert_id(aoi: str, pix_id: str, break_ordinal: int) -> str:
    """Deterministic id: one pixel + one break date is one alert."""
    return f"{aoi}:{pix_id}:{break_ordinal}"


_alert_id = alert_id  # historical name, kept for callers


def known_ids(aoi_name: str) -> set[str]:
    """Alert ids already in the table — the basis for reconciling against state."""
    df = load(aoi_name)
    return set(df["alert_id"]) if len(df) else set()


def record(
    aoi_name: str,
    breaks: list[dict],
    grid: pd.DataFrame,
    gate: float = LOSS_GATE,
    monitoring_start: str | None = None,
) -> pd.DataFrame:
    """Append breaks as alerts, preserving any existing human verdict.

    Below-gate and pre-monitoring breaks are kept with that status, not dropped.
    """
    if not breaks:
        return load(aoi_name)

    df = pd.DataFrame(breaks)
    loc = grid.set_index("pix_id")[["poly_id", "lon", "lat"]]
    df = df.join(loc, on="pix_id")

    df["alert_id"] = [
        alert_id(aoi_name, p, o) for p, o in zip(df["pix_id"], df["break_ordinal"])
    ]
    df["aoi"] = aoi_name
    df["gate"] = gate
    df["detected_at"] = dt.datetime.now(dt.UTC)
    # Bootstrap/refit breaks carry no latency fields; keep the schema fixed.
    for col in ("detection_lag_days", "nrt_mode"):
        if col not in df.columns:
            df[col] = pd.NA
    df["recipe_version"] = config.RECIPE_VERSION
    # Only losses are alertable; gains are stored but left un-triaged.
    below = df["magnitude"].abs() < gate
    df["status"] = "new"
    df.loc[below, "status"] = "below_gate"
    df.loc[df["kind"] == "gain", "status"] = "below_gate"

    start = pd.Timestamp(monitoring_start or config.MONITORING_START)
    old = pd.to_datetime(df["break_date"]) < start
    df.loc[old & (df["status"] == "new"), "status"] = "historical"

    # Confidence depends on the observation store, so it is scored here.
    df = conf_mod.score_breaks(aoi_name, df)

    existing = load(aoi_name)
    if len(existing):
        # A verdict or triage decision outranks a re-run (a refit re-derives
        # the whole break history).
        settled = existing[existing["status"].isin(REVIEWED + ("confirmed",))]
        df = df[~df["alert_id"].isin(settled["alert_id"])]
        # Keep first-detection time: detection_lag_days is a latency measure.
        first = existing.set_index("alert_id")[["detected_at", "detection_lag_days"]]
        seen = df["alert_id"].isin(first.index)
        if seen.any():
            df.loc[seen, "detected_at"] = df.loc[seen, "alert_id"].map(
                first["detected_at"]
            )
            df.loc[seen, "detection_lag_days"] = df.loc[seen, "alert_id"].map(
                first["detection_lag_days"]
            )

    out = (
        pd.concat([existing, df], ignore_index=True)
        .drop_duplicates(subset="alert_id", keep="last")
        .sort_values("break_date")
        .reset_index(drop=True)
    )
    store.write_parquet(out, alerts_path(aoi_name))
    return out


def summary(aoi_name: str) -> pd.DataFrame:
    df = load(aoi_name)
    if df.empty:
        return df
    return (
        df.groupby(["status", "kind"])
        .size()
        .rename("n")
        .reset_index()
        .sort_values("n", ascending=False)
    )


def rescore(aoi_name: str, use_agreement: bool = True) -> pd.DataFrame:
    """Recompute confidence for every alert in place; statuses are untouched.

    Confidence grows as clear observations accumulate after the break, so it is
    not frozen at write time.
    """
    df = load(aoi_name)
    if df.empty:
        return df
    scored = conf_mod.score_breaks(aoi_name, df, use_agreement=use_agreement)
    store.write_parquet(scored, alerts_path(aoi_name))
    return scored


def publishable(aoi_name: str, include_gain: bool = True) -> pd.DataFrame:
    """The rows the public product is allowed to show (never `below_gate`)."""
    df = load(aoi_name)
    if df.empty:
        return df
    return df[publishable_mask(df, include_gain)].reset_index(drop=True)


def publishable_mask(df: pd.DataFrame, include_gain: bool = True) -> pd.Series:
    """Rows of an alert table that `publishable` would keep."""
    keep = df["status"].isin(("new", "historical", "confirmed") + REVIEWED)
    if include_gain:
        # `record` files gains as `below_gate`; a gain clearing the magnitude
        # gate is published as its own layer without rewriting its status.
        keep |= (df["kind"] == "gain") & (df["magnitude"].abs() >= df["gate"])
    else:
        keep &= df["kind"] == "loss"
    return keep
