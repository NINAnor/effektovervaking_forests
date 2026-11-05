"""Structured logging and health metrics.

An append-only JSON Lines event log per AOI (events carry `run_id`, `stage`,
`event` and a UTC timestamp; stage names match the `runs.py` ledger), plus a
health snapshot computed from the artefacts themselves (ledger, store, state
meta, catalogue) rather than from the log, so it works even when a run died
before logging. `health_exit_code` maps the verdict to a timer-friendly code.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from . import config

LOG_DIR = config.LOG_DIR

# Health thresholds, deliberately generous (winter can go a week without a
# usable scene). Override per deployment.
STALE_STATE_DAYS = float(os.environ.get("FORESTMON_STALE_STATE_DAYS") or "14")
STALE_RUN_HOURS = float(os.environ.get("FORESTMON_STALE_RUN_HOURS") or "48")
MAX_BACKLOG = int(os.environ.get("FORESTMON_MAX_BACKLOG") or "50")
MAX_ERROR_RATE = float(os.environ.get("FORESTMON_MAX_ERROR_RATE") or "0.10")


def log_path(aoi_name: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{aoi_name}.jsonl"


class RunLogger:
    """One run's events, flushed line by line so they survive a killed process.

    `run_id` ties together the lines of one invocation.
    """

    def __init__(
        self, aoi_name: str, command: str, echo: bool = True, run_id: str | None = None
    ):
        self.aoi = aoi_name
        self.command = command
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.echo = echo
        self.t0 = time.time()
        self.counts: dict[str, int] = {}
        self.path = log_path(aoi_name)

    def event(self, event: str, stage: str = "", level: str = "info", **fields):
        rec = {
            "ts": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "aoi": self.aoi,
            "command": self.command,
            "stage": stage,
            "event": event,
            "level": level,
            "elapsed_s": round(time.time() - self.t0, 2),
            **fields,
        }
        self.counts[event] = self.counts.get(event, 0) + 1
        with open(self.path, "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
        if self.echo and level != "debug":
            extra = " ".join(
                f"{k}={v}" for k, v in fields.items() if k not in ("traceback",)
            )
            mark = {"error": "!!", "warn": " !"}.get(level, "  ")
            print(
                f"{mark} [{rec['elapsed_s']:>8.1f}s] {event:<22} {extra}",
                file=sys.stderr,
                flush=True,
            )

    def warn(self, event: str, **fields):
        self.event(event, level="warn", **fields)

    def error(self, event: str, **fields):
        self.event(event, level="error", **fields)


@contextmanager
def run_logger(aoi_name: str, command: str, echo: bool = True, **start_fields):
    """Bracket a run with start/end events, recording failure if one escapes.

    `run_end` is also emitted on failure (`ok=false`); a run with no `run_end`
    was killed.
    """
    log = RunLogger(aoi_name, command, echo=echo)
    log.event(
        "run_start",
        stage="start",
        pid=os.getpid(),
        host=socket.gethostname(),
        **start_fields,
    )
    try:
        yield log
    except BaseException as e:  # noqa: BLE001
        log.event(
            "run_end",
            stage="end",
            level="error",
            ok=False,
            error=f"{type(e).__name__}: {e}"[:400],
            seconds=round(time.time() - log.t0, 1),
        )
        raise
    else:
        log.event(
            "run_end",
            stage="end",
            ok=True,
            seconds=round(time.time() - log.t0, 1),
            counts=log.counts,
        )


# ── reading the log back ─────────────────────────────────────────────────────
def read_events(
    aoi_name: str,
    limit: int | None = None,
    level: str | None = None,
    run_id: str | None = None,
) -> pd.DataFrame:
    p = log_path(aoi_name)
    if not p.exists():
        return pd.DataFrame(columns=["ts", "run_id", "event", "level"])
    rows = []
    with open(p) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn last line during a live tail
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="mixed")
    if level:
        df = df[df["level"] == level]
    if run_id:
        df = df[df["run_id"] == run_id]
    df = df.sort_values("ts")
    return df.tail(limit) if limit else df


def last_run(aoi_name: str) -> dict | None:
    df = read_events(aoi_name)
    if df.empty or "event" not in df.columns:
        return None
    ends = df[df["event"] == "run_end"]
    return None if ends.empty else ends.iloc[-1].to_dict()


# ── health, computed from artefacts rather than from the log ─────────────────
def health(aoi_name: str) -> dict:
    """Health snapshot answered from files on disk.

    Imports are local so it still works when parts of the system are missing.
    """
    from . import alerts as alerts_mod
    from . import catalog, obs, runs, store
    from . import state as state_mod

    now = pd.Timestamp.now(tz="UTC")
    h: dict = {
        "aoi": aoi_name,
        "checked_at": now.isoformat(timespec="seconds"),
        "problems": [],
        "warnings": [],
    }

    # ── catalogue and backlog ────────────────────────────────────────────────
    cat = catalog.load(aoi_name)
    led = runs.load(aoi_name)
    h["n_catalogued"] = int(len(cat))
    h["n_in_ledger"] = int(len(led))
    if len(cat):
        deduped = catalog.dedupe_versions(cat)
        done = set(led.loc[led["status"] == "ok", "scene_id"]) if len(led) else set()
        backlog = deduped[~deduped["id"].isin(done)]
        h["backlog_scenes"] = int(len(backlog))
        if len(backlog):
            oldest = pd.to_datetime(backlog["datetime"], utc=True).min()
            h["oldest_unprocessed_acquisition"] = oldest.isoformat()
            h["backlog_age_days"] = round((now - oldest).total_seconds() / 86400, 1)
        newest_cat = pd.to_datetime(cat["datetime"], utc=True).max()
        h["newest_catalogued_acquisition"] = newest_cat.isoformat()
        h["catalogue_age_days"] = round((now - newest_cat).total_seconds() / 86400, 1)

    # ── read health ──────────────────────────────────────────────────────────
    if len(led):
        recent = led.sort_values("processed_at").tail(500)
        n_err = int((recent["status"] != "ok").sum())
        h["recent_error_rate"] = round(n_err / len(recent), 4)
        h["recent_errors"] = n_err
        secs = pd.to_numeric(recent["seconds"], errors="coerce").dropna()
        if len(secs):
            h["scene_seconds_median"] = round(float(secs.median()), 2)
            h["scene_seconds_p95"] = round(float(secs.quantile(0.95)), 2)
        if n_err:
            msgs = recent.loc[recent["status"] != "ok", "message"]
            h["top_errors"] = msgs.str.slice(0, 80).value_counts().head(3).to_dict()
        last_proc = pd.to_datetime(recent["processed_at"], utc=True).max()
        h["last_scene_processed"] = last_proc.isoformat()

    # ── the store-to-model gap ───────────────────────────────────────────────
    unapplied = runs.unapplied_scene_ids(aoi_name)
    h["n_stored_not_applied"] = len(unapplied)
    h["n_uncompacted_parts"] = sum(
        len(obs.parts_for(aoi_name, y)) for y in obs.years_present(aoi_name)
    )

    # ── the model ────────────────────────────────────────────────────────────
    meta = state_mod.load_meta(aoi_name)
    h["state_exists"] = state_mod.exists(aoi_name)
    if meta:
        h["state_n_pixels"] = meta.get("n_pixels")
        h["state_recipe_version"] = meta.get("recipe_version")
        h["alerts_dirty"] = bool(meta.get("alerts_dirty"))
        last_obs = meta.get("last_observation")
        if last_obs:
            lo = pd.Timestamp(last_obs)
            if lo.tzinfo is None:
                lo = lo.tz_localize("UTC")
            h["newest_observation_in_state"] = lo.isoformat()
            h["state_age_days"] = round((now - lo).total_seconds() / 86400, 1)

    # ── alerts ───────────────────────────────────────────────────────────────
    al = alerts_mod.load(aoi_name)
    if len(al):
        h["alerts_by_status"] = al["status"].value_counts().to_dict()
        actionable = al[al["status"] == "new"]
        h["n_alerts_awaiting_review"] = int(len(actionable))
        if len(actionable):
            h["oldest_unreviewed_break"] = str(actionable["break_date"].min())

    # ── is anything running, or stuck? ───────────────────────────────────────
    holder = store.lease_holder(aoi_name)
    if holder:
        held_for = (now - pd.Timestamp(holder["acquired_at"])).total_seconds() / 3600
        h["lease"] = {**holder, "held_hours": round(held_for, 2)}
        if held_for > 24:
            h["warnings"].append(
                f"lease held for {held_for:.1f} h by pid {holder.get('pid')} — "
                "a dead run, or a backfill that is genuinely still going"
            )

    lr = last_run(aoi_name)
    if lr:
        h["last_run"] = {
            "ts": str(lr.get("ts")),
            "command": lr.get("command"),
            "ok": lr.get("ok"),
            "seconds": lr.get("seconds"),
            "error": lr.get("error"),
        }
        age_h = (now - pd.Timestamp(lr["ts"])).total_seconds() / 3600
        h["hours_since_last_run"] = round(age_h, 1)

    # ── verdict ──────────────────────────────────────────────────────────────
    P, W = h["problems"], h["warnings"]
    if not h["state_exists"]:
        P.append("no S-CCD state — the model has never been fitted")
    if h.get("state_age_days", 0) > STALE_STATE_DAYS:
        P.append(
            f"model has not advanced in {h['state_age_days']} days "
            f"(threshold {STALE_STATE_DAYS})"
        )
    if h.get("hours_since_last_run", 0) > STALE_RUN_HOURS:
        P.append(
            f"no run completed in {h['hours_since_last_run']} h "
            f"(threshold {STALE_RUN_HOURS})"
        )
    if lr and lr.get("ok") is False:
        P.append(f"last run failed: {lr.get('error')}")
    if h.get("backlog_scenes", 0) > MAX_BACKLOG:
        W.append(
            f"{h['backlog_scenes']} scenes unprocessed — the run is not "
            "keeping up, or a backfill is incomplete"
        )
    if h.get("recent_error_rate", 0) > MAX_ERROR_RATE:
        P.append(f"{h['recent_error_rate']:.1%} of recent scene reads failed")
    if h.get("n_stored_not_applied", 0):
        W.append(
            f"{h['n_stored_not_applied']} scenes stored but not applied to "
            "state — the next run replays them from disk"
        )
    if h.get("alerts_dirty"):
        W.append(
            "alerts_dirty: state advanced but alerts were not written; the "
            "next run reconciles them from state"
        )
    if h.get("n_uncompacted_parts", 0) > 200:
        W.append(
            f"{h['n_uncompacted_parts']} uncompacted part files — "
            "run `forestmon obs compact`"
        )

    h["status"] = "unhealthy" if P else ("degraded" if W else "healthy")
    return h


def health_exit_code(h: dict) -> int:
    """0 healthy, 1 degraded, 2 unhealthy — for a timer or a readiness probe."""
    return {"healthy": 0, "degraded": 1, "unhealthy": 2}[h["status"]]
