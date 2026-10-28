"""Durability primitives shared by every file this package writes.

Three things every writer here needs and none of them had:

* **Atomic replace.** `df.to_parquet(p)` truncates the target before it writes.
  A run killed at that moment leaves a zero-byte parquet where the ledger used
  to be — and the ledger is the only record of what has been processed. Writing
  to a temp file in the same directory and `os.replace`-ing it is atomic on
  POSIX, so a reader ever sees the old file or the new one, never a torn one.

* **A single-run lease.** Two `forestmon run` invocations on one AOI would
  read-modify-write the same ledger and the same state pickle, and the loser's
  work vanishes silently. Schedulers do double-fire — Kubernetes Jobs are
  explicitly at-least-once, and a systemd timer plus an impatient operator gets
  you there just as fast — so "idempotent when nothing is new" is not enough.
  The lease is advisory, per-AOI, and carries the holder's pid/host/start so a
  stale one can be diagnosed rather than just deleted.

* **Immutable part files.** See `obs.py`: appending by rewriting a growing
  yearly parquet costs O(n^2) over a backfill and puts the whole partition at
  risk on every write. Parts are written once, never modified, and compacted
  later as a separate, restartable step.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from . import config


def write_parquet(df: pd.DataFrame, path: Path) -> Path:
    """Write `df` to `path` atomically (temp file in the same dir + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def write_json(obj, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)
    return path


# ── the lease ────────────────────────────────────────────────────────────────
LOCK_DIR = config.OPS / "locks"


def lock_path(aoi_name: str) -> Path:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    return LOCK_DIR / f"{aoi_name}.lock"


class LockBusy(RuntimeError):
    """Another process holds the AOI lease."""


def _read_lock(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _pid_alive(pid: int, host: str) -> bool:
    """Only meaningful for our own host; a remote holder is assumed alive."""
    if host != socket.gethostname():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def lease(
    aoi_name: str, what: str = "run", stale_after_h: float = 24.0, force: bool = False
):
    """Hold the per-AOI write lease for the duration of the block.

    Raises `LockBusy` rather than waiting: these are scheduled jobs, and a run
    that fires while the previous one is still going should exit and let the
    next tick pick the work up, not queue up behind it and double the load on
    CDSE. A lease whose holder is gone, or older than `stale_after_h`, is taken
    over with a note in the new lease.
    """
    p = lock_path(aoi_name)
    taken_over = None
    if p.exists() and not force:
        held = _read_lock(p)
        acquired = held.get("acquired_at")
        age_h = None
        if acquired:
            age_h = (
                dt.datetime.now(dt.UTC) - pd.Timestamp(acquired).to_pydatetime()
            ).total_seconds() / 3600
        alive = _pid_alive(int(held.get("pid", -1)), held.get("host", ""))
        stale = (not alive) or (age_h is not None and age_h > stale_after_h)
        if not stale:
            raise LockBusy(
                f"{aoi_name}: '{held.get('what')}' already running "
                f"(pid {held.get('pid')} on {held.get('host')}, started "
                f"{acquired}). Wait for it, or pass --force if you are certain "
                "it is dead."
            )
        taken_over = held

    payload = {
        "aoi": aoi_name,
        "what": what,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "acquired_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    if taken_over:
        payload["took_over_from"] = taken_over
    write_json(payload, p)
    try:
        yield payload
    finally:
        # Only release a lease we still own — a forced takeover elsewhere means
        # someone else is the writer now and their lease must survive.
        if _read_lock(p).get("pid") == os.getpid():
            try:
                p.unlink()
            except FileNotFoundError:
                pass


def lease_holder(aoi_name: str) -> dict | None:
    p = lock_path(aoi_name)
    return _read_lock(p) if p.exists() else None


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def stopwatch():
    t0 = time.time()
    return lambda: round(time.time() - t0, 2)
