"""Turning S-CCD breaks into alerts.

A break is not an alert. The label study in this project (see
`results/label_accuracy.json`) found that an ungated break flags more than half
of all stable pixels — across every segmenter, the magnitude gate mattered more
than the choice of algorithm or its parameters. So the gate is applied here,
explicitly and with the threshold recorded on every row, rather than being
buried in the detector.

The gate default is the label-tuned S-CCD value carried over from the inspector.
It is a starting point, not a settled number: it was fitted on 15 loss positives,
so its confidence interval is wide, and re-tuning it against a larger, dated
NRT label set is the obvious next calibration job.

Alerts are append-only with a lifecycle status, so a human verdict recorded in
review is never overwritten by a later run.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from . import confidence as conf_mod
from . import config, store

# Label-tuned S-CCD magnitude gate (NBR units). Breaks weaker than this are
# stored with status "below_gate" rather than dropped, so re-gating later does
# not require re-running the model.
LOSS_GATE = 0.075

# "historical" is the bootstrap's output: a break the initial fit found somewhere
# in 2017-2025. It is real change, and it belongs in the table as context for a
# pixel's history, but it is not something anyone can go and look at now. Only
# breaks dated on or after config.MONITORING_START become operational alerts.
STATUSES = ("new", "historical", "below_gate", "confirmed", "verified", "rejected")

# A human verdict outranks anything a re-run computes.
REVIEWED = ("verified", "rejected")

# Written by `confidence.score_breaks`. Every input to the tier is stored, not
# just the tier, so a reviewer disputing "low" can see which term produced it.
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
    """Stable id: one pixel + one break date is one alert, forever.

    Deterministic rather than sequential so a re-run, a re-bootstrap or a
    restore from backup cannot mint a duplicate alert for a break a human has
    already reviewed.
    """
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

    Statuses are assigned here, not in the detector, and all of them are stored:
    a break below the gate or before the monitoring start is kept with that
    status rather than dropped, so re-gating or moving the start date is a query
    rather than a re-run of the model.
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
    # Bootstrap and refit breaks carry no latency fields; keep the columns so
    # the table's schema does not depend on which path produced a row.
    for col in ("detection_lag_days", "nrt_mode"):
        if col not in df.columns:
            df[col] = pd.NA
    df["recipe_version"] = config.RECIPE_VERSION
    # Only losses are alertable; a gain break is real change but not the signal
    # this system exists to catch, so it is stored and left un-triaged.
    below = df["magnitude"].abs() < gate
    df["status"] = "new"
    df.loc[below, "status"] = "below_gate"
    df.loc[df["kind"] == "gain", "status"] = "below_gate"

    start = pd.Timestamp(monitoring_start or config.MONITORING_START)
    old = pd.to_datetime(df["break_date"]) < start
    df.loc[old & (df["status"] == "new"), "status"] = "historical"

    # Confidence is computed here rather than in the detector for the same
    # reason the gate is: it is a publication decision, it depends on the
    # observation store as well as the model, and it must be re-derivable
    # without re-running S-CCD.
    df = conf_mod.score_breaks(aoi_name, df)

    existing = load(aoi_name)
    if len(existing):
        # A verdict or a triage decision outranks anything a re-run computes. A
        # refit re-derives a pixel's whole break history, so without this a
        # reprocessed 2019 granule would quietly un-review alerts from 2019.
        settled = existing[existing["status"].isin(REVIEWED + ("confirmed",))]
        df = df[~df["alert_id"].isin(settled["alert_id"])]
        # First detection is the truth: a break re-derived by a refit was not
        # detected today, and detection_lag_days is a latency measurement.
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
    """Recompute confidence for every alert in the table, in place.

    Confidence is NOT a property of the moment an alert was raised. Its largest
    term is the number of clear observations *after* the break, which grows with
    every cycle -- so an alert correctly published as `low` on the day it fired
    should climb to `medium` or `high` as the record accumulates behind it, and
    an alert that never gains corroboration should stay where it is. Freezing the
    tier at write time would make the review queue permanently wrong about
    everything except brand-new alerts.

    Statuses are not touched. A human verdict is a statement about reality; the
    confidence tier is a statement about our evidence, and a re-run is entitled
    to revise the second without disturbing the first.
    """
    df = load(aoi_name)
    if df.empty:
        return df
    scored = conf_mod.score_breaks(aoi_name, df, use_agreement=use_agreement)
    store.write_parquet(scored, alerts_path(aoi_name))
    return scored


def publishable(aoi_name: str, include_gain: bool = True) -> pd.DataFrame:
    """The rows the public product is allowed to show.

    `below_gate` is excluded from publication but never deleted from the table,
    so re-gating stays a query rather than a re-run of the model -- which is the
    whole reason those 60k-odd rows are kept.
    """
    df = load(aoi_name)
    if df.empty:
        return df
    keep = df["status"].isin(("new", "historical", "confirmed") + REVIEWED)
    if include_gain:
        # `record` files every gain as `below_gate` because gain is not the
        # signal this system exists to catch, so it is stored and left
        # un-triaged. But the review vocabulary lets a reviewer answer "gain",
        # and a verdict option with nothing on screen to attach it to is a
        # broken form -- so a gain that clears the same magnitude gate is
        # publishable as its own layer. Its `status` is deliberately NOT
        # rewritten: it is still un-triaged, and the triage pipeline should keep
        # seeing it that way.
        keep |= (df["kind"] == "gain") & (df["magnitude"].abs() >= df["gate"])
    else:
        keep &= df["kind"] == "loss"
    return df[keep].reset_index(drop=True)
