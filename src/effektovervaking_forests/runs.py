"""The run ledger: which scenes have been processed, and how far.

Separate from the observation store because a fully clouded scene yields no
observations but has been processed. Stages:

    discovered -> observations_committed -> state_applied

Scenes at `observations_committed` must not be re-read from CDSE nor considered
done; `unapplied_scene_ids` finds them so the next run replays them from disk.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from . import config, store

COLUMNS = [
    "scene_id",
    "datetime",
    "processed_at",
    "n_pixels_read",
    "n_clear",
    "status",
    "stage",
    "seconds",
    "message",
    "recipe_version",
]

STAGE_OBS = "observations_committed"
STAGE_APPLIED = "state_applied"


def ledger_path(aoi_name: str):
    return config.RUNS_DIR / f"{aoi_name}_ledger.parquet"


def load(aoi_name: str) -> pd.DataFrame:
    p = ledger_path(aoi_name)
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    df = pd.read_parquet(p)
    if "stage" not in df.columns:
        # Pre-stage ledgers: treat as fully applied, not replayed.
        df["stage"] = STAGE_APPLIED
    return df


def processed_ids(aoi_name: str, include_failed: bool = False) -> set[str]:
    """Scenes that must not be read from CDSE again.

    Includes scenes at `observations_committed` (only model application remains).
    """
    df = load(aoi_name)
    if df.empty:
        return set()
    if not include_failed:
        df = df[df["status"] == "ok"]
    return set(df["scene_id"])


def unapplied_scene_ids(aoi_name: str) -> set[str]:
    """Successful scenes whose observations S-CCD has not yet consumed."""
    df = load(aoi_name)
    if df.empty:
        return set()
    m = (df["status"] == "ok") & (df["stage"] != STAGE_APPLIED)
    return set(df.loc[m, "scene_id"])


def record(aoi_name: str, rows: list[dict], stage: str = STAGE_OBS) -> None:
    """Commit ledger rows for a batch. Atomic; last write per scene_id wins."""
    if not rows:
        return
    now = dt.datetime.now(dt.UTC)
    for r in rows:
        r.setdefault("processed_at", now)
        r.setdefault("recipe_version", config.RECIPE_VERSION)
        r.setdefault("stage", stage)
    new = pd.DataFrame(rows)
    old = load(aoi_name)
    out = (
        pd.concat([old, new], ignore_index=True)
        .drop_duplicates(subset="scene_id", keep="last")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    store.write_parquet(out, ledger_path(aoi_name))


def mark_applied(aoi_name: str, scene_ids) -> int:
    """Advance scenes to `state_applied` — called only after state is saved."""
    ids = set(scene_ids)
    if not ids:
        return 0
    df = load(aoi_name)
    if df.empty:
        return 0
    m = df["scene_id"].isin(ids)
    df.loc[m, "stage"] = STAGE_APPLIED
    store.write_parquet(df, ledger_path(aoi_name))
    return int(m.sum())
