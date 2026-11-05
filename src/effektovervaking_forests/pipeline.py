"""The steps that tie catalogue, reader, store and model together.

Kept separate from the CLI so the same code path runs whether a person types the
command or a systemd timer fires it, and so each step can be tested without
Typer in the way.

**Why this file is mostly about commit boundaries.** Three artefacts have to
agree — the observation store, the run ledger, and the S-CCD state pickle — and
no filesystem will let us update all three atomically. So each is written in an
order chosen so that any crash leaves work to redo, never work silently lost:

    1. observations   -> a new immutable part file       (idempotent by key)
    2. ledger         -> stage = observations_committed  (scene won't be re-read)
    3. state          -> pickle replaced atomically
    4. ledger         -> stage = state_applied           (scene is fully done)
    5. alerts         -> written, then meta.alerts_dirty cleared

A crash before (2) re-reads the scene: harmless, the store dedupes. Between (2)
and (4) the observations are on disk but unseen by the model, and the next run
replays them FROM DISK rather than from CDSE. After (4) but before (5), state
holds breaks the alert table is missing, and `alerts_dirty` makes the next run
rebuild the table from state. The one thing never allowed is applying the same
observation to the recursive model twice, which is why the ledger advances to
`state_applied` only after the pickle is on disk.
"""

from __future__ import annotations

import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from . import alerts as alerts_mod
from . import aoi as aoi_mod
from . import catalog, config, obs, obslog, reader, runs, store
from . import sccd as sccd_mod
from . import state as state_mod


def _read_one(sc: pd.Series, pixels: pd.DataFrame) -> tuple[dict, pd.DataFrame | None]:
    t0 = time.time()
    px = reader.pixels_for_scene(pixels, sc)
    base = {"scene_id": sc["id"], "datetime": pd.Timestamp(sc["datetime"])}
    if px.empty:
        return {
            **base,
            "n_pixels_read": 0,
            "n_clear": 0,
            "status": "ok",
            "seconds": round(time.time() - t0, 2),
            "message": "no AOI pixels in granule bbox",
        }, None
    try:
        df = reader.read_scene(sc, px)
    except Exception as e:  # noqa: BLE001
        return {
            **base,
            "n_pixels_read": 0,
            "n_clear": 0,
            "status": "error",
            "seconds": round(time.time() - t0, 2),
            "message": f"{type(e).__name__}: {e}"[:300],
        }, None
    return {
        **base,
        "n_pixels_read": len(df),
        "n_clear": int(df["clear"].sum()),
        "status": "ok",
        "seconds": round(time.time() - t0, 2),
        "message": "",
    }, df


def ingest(
    aoi_name: str,
    scenes: pd.DataFrame,
    pixels: pd.DataFrame,
    workers: int = config.SCENE_WORKERS,
    batch_size: int = config.INGEST_BATCH,
    progress=None,
    log: obslog.RunLogger | None = None,
) -> dict:
    """Read scenes into the observation store, committing every `batch_size`.

    Threaded because the work is entirely network latency — GDAL's range reads
    against S3 release the GIL, so threads (not processes) are the right tool.
    Note `workers` multiplies with `reader.BAND_WORKERS`: measured throughput
    peaks at ~14 concurrent connections and degrades past it, so 2 x 7 is the
    default rather than a number that looks braver.

    Bounded batches are what make a 15,000-scene backfill survivable. Holding
    every scene's DataFrame until the end would be tens of gigabytes of RAM and
    would make an interruption at hour ten cost all ten hours; committing a part
    file plus its ledger rows per batch caps the loss at one batch.

    Every scene gets a ledger row whatever happens, including failures, so a
    scene CDSE cannot serve is retried on the next run rather than being
    silently forgotten or silently retried forever.
    """
    scenes = scenes.reset_index(drop=True)
    n = len(scenes)
    total = {
        "n_scenes": n,
        "n_ok": 0,
        "n_error": 0,
        "n_rows": 0,
        "n_clear": 0,
        "n_batches": 0,
        "scene_ids": [],
    }
    if n == 0:
        return total

    done = 0
    t_start = time.time()
    for start in range(0, n, batch_size):
        batch = scenes.iloc[start : start + batch_size]
        t_batch = time.time()
        rows: list[dict] = []
        frames: list[pd.DataFrame] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_read_one, sc, pixels) for _, sc in batch.iterrows()]
            for fut in as_completed(futures):
                row, df = fut.result()
                rows.append(row)
                if df is not None and len(df):
                    frames.append(df)
                done += 1
                if log and row["status"] != "ok":
                    # Every failed read is logged individually: a run that quietly
                    # skips 300 scenes and reports "ok" is the failure mode this
                    # whole ledger exists to prevent.
                    log.error(
                        "scene_read_failed",
                        stage="ingest",
                        scene_id=row["scene_id"],
                        reason=row["message"],
                    )
                if progress:
                    progress(done, n, row)

        # (1) observations, then (2) the ledger. Never the other way round: a
        # ledger entry without its observations would mark a scene done that
        # nothing has ever read.
        if frames:
            batch_df = pd.concat(frames, ignore_index=True)
            obs.append(aoi_name, batch_df)
            total["n_rows"] += len(batch_df)
            total["n_clear"] += int(batch_df["clear"].sum())
        runs.record(aoi_name, rows, stage=runs.STAGE_OBS)

        total["n_batches"] += 1
        n_ok = sum(r["status"] == "ok" for r in rows)
        total["n_ok"] += n_ok
        total["n_error"] += len(rows) - n_ok
        total["scene_ids"].extend(r["scene_id"] for r in rows if r["status"] == "ok")

        if log:
            # Throughput is reported per-batch AND split by whether a scene had
            # pixels to read, because the cumulative rate alone is not comparable
            # between runs — or even between batches of one run. Early years are
            # dominated by granules whose bbox contains our pixels but whose swath
            # does not, and those cost ~0 s. A run working through them looks 40%
            # "faster" than one reading real data, which makes the number useless
            # for judging a transport change. `substantive_scene_s` is the median
            # over scenes that actually read something, and is the one to compare.
            secs = pd.Series([r["seconds"] for r in rows], dtype="float64")
            has_px = pd.Series([r["n_pixels_read"] > 0 for r in rows])
            batch_s = time.time() - t_batch
            log.event(
                "batch_committed",
                stage="ingest",
                batch=total["n_batches"],
                scenes_done=done,
                scenes_total=n,
                n_ok=n_ok,
                n_error=len(rows) - n_ok,
                n_with_pixels=int(has_px.sum()),
                rows=total["n_rows"],
                clear=total["n_clear"],
                median_scene_s=round(float(secs.median()), 2),
                substantive_scene_s=(
                    round(float(secs[has_px.values].median()), 2)
                    if has_px.any()
                    else None
                ),
                batch_scenes_per_min=round(60 * len(rows) / max(batch_s, 1e-6), 1),
                scenes_per_min=round(60 * done / max(time.time() - t_start, 1e-6), 1),
                gdal_num_threads=config.GDAL_OPTS["GDAL_NUM_THREADS"],
                eta_min=round(
                    (n - done) * (time.time() - t_start) / max(done, 1) / 60, 1
                ),
            )
    return total


def pending_scenes(
    aoi_name: str, limit: int | None = None, since: str | None = None
) -> pd.DataFrame:
    """Catalogued scenes with no ledger entry at all, oldest first.

    A scene at `observations_committed` is NOT pending: its observations are
    already stored, and re-reading them would spend a CDSE round trip to
    reproduce rows the store already holds. What is outstanding for it is model
    application, which `apply_pending_observations` does from disk.
    """
    df = catalog.pending(aoi_name, runs.processed_ids(aoi_name))
    if since:
        df = df[
            pd.to_datetime(df["datetime"], utc=True) >= pd.Timestamp(since, tz="UTC")
        ]
    return df.head(limit) if limit else df


def _fit_from_store(
    aoi_name: str, pix_ids: set[str], states: dict, progress=None
) -> tuple[dict, list[dict], int]:
    """(Re)fit whole histories for a set of pixels, from stored observations."""
    if not pix_ids:
        return states, [], 0
    hist = obs.load(aoi_name, pix_ids=pix_ids)
    if hist.empty:
        return states, [], 0
    fitted, breaks, stats = sccd_mod.bootstrap(hist, progress=progress)
    states.update(fitted)
    return states, breaks, stats["n_fitted"]


def apply_pending_observations(
    aoi_name: str,
    grid: pd.DataFrame,
    progress=None,
    log: obslog.RunLogger | None = None,
) -> dict:
    """Fold every stored-but-unapplied observation into S-CCD state.

    This is the step that closes the gap between the store and the model, and it
    reads only from disk. It runs on every cycle, so a run interrupted after its
    observations were committed is repaired by the next one without touching
    CDSE.

    Three populations of pixel are handled, not one:

    * **In state, observations after the watermark** — the normal path,
      `sccd_update`.
    * **In state, observations BEFORE the watermark** — a reprocessed product
      carrying an old sensing date. Feeding these forward would corrupt a
      recursive model, so the affected pixels are refit from full history.
    * **Not in state** — too sparse to fit at bootstrap. Refit once the store
      says they have crossed MIN_OBS, instead of being counted as unknown
      forever.
    """
    fingerprint = aoi_mod.grid_fingerprint(grid)
    unapplied = runs.unapplied_scene_ids(aoi_name)
    meta_now = state_mod.load_meta(aoi_name)
    result: dict = {"n_unapplied_scenes": len(unapplied)}
    if not unapplied and not meta_now.get("alerts_dirty"):
        result["status"] = "nothing to apply"
        return result
    if log:
        log.event(
            "applying_observations",
            stage=runs.STAGE_OBS,
            n_scenes=len(unapplied),
            alerts_dirty=bool(meta_now.get("alerts_dirty")),
        )

    states, meta = state_mod.load(aoi_name, grid_fingerprint=fingerprint)
    watermark = (
        pd.Timestamp(meta["last_observation"]) if meta.get("last_observation") else None
    )

    new_obs = (
        obs.load(aoi_name, scene_ids=unapplied)
        if unapplied
        else pd.DataFrame(columns=obs.SCHEMA_COLS)
    )
    result["n_observations"] = int(len(new_obs))

    fresh: list[dict] = []
    forward, late = sccd_mod.split_by_watermark(new_obs, states, watermark)
    result["n_late_observations"] = int(len(late))

    if len(late) and log:
        log.warn(
            "late_observations_routed_to_refit",
            stage="model",
            n_observations=int(len(late)),
            n_pixels=int(late["pix_id"].nunique()),
            watermark=str(watermark),
        )
    if len(late):
        # Superseded/reprocessed history: refit rather than update. Their breaks
        # are re-derived wholesale, so they go through the alert table by id and
        # any human verdict on them survives.
        late_pix = set(late["pix_id"].unique())
        states, refit_breaks, n_refit = _fit_from_store(
            aoi_name, late_pix, states, progress=progress
        )
        fresh.extend(refit_breaks)
        result["n_pixels_refit"] = n_refit

    if len(forward):
        states, upd_fresh, stats = sccd_mod.update(states, forward, progress=progress)
        pending_pix = set(stats.pop("pixels_without_state", []))
        result.update(stats)
        fresh.extend(upd_fresh)

        # Pixels that had no state: fit the ones that now qualify.
        if pending_pix:
            counts = obs.observation_counts(aoi_name, pix_ids=pending_pix)
            ready = set(counts[counts >= sccd_mod.MIN_OBS].index)
            states, new_breaks, n_new = _fit_from_store(
                aoi_name, ready, states, progress=progress
            )
            fresh.extend(new_breaks)
            result["n_pixels_newly_fitted"] = n_new
            result["n_pixels_still_too_sparse"] = len(pending_pix - ready)

    if len(new_obs):
        newest = pd.to_datetime(new_obs["datetime"], utc=True).max()
        if watermark is None or newest > watermark:
            meta["last_observation"] = str(newest)
    meta["grid_fingerprint"] = fingerprint
    meta["alerts_dirty"] = True
    meta["applied_scenes"] = int(meta.get("applied_scenes", 0)) + len(unapplied)
    meta["state_saved_at"] = store.now_iso()

    # (3) state, then (4) the ledger. If the ledger were advanced first, a crash
    # here would leave scenes marked applied that the model never absorbed.
    state_mod.save(aoi_name, states, meta)
    if log:
        log.event(
            "state_saved",
            stage=runs.STAGE_APPLIED,
            n_pixels=len(states),
            watermark=meta.get("last_observation"),
        )
    runs.mark_applied(aoi_name, unapplied)

    # (5) alerts. Recomputed from state, not only from this run's delta, so a
    # previously failed alert write is repaired rather than lost forever.
    result.update(reconcile_alerts(aoi_name, states, grid, fresh=fresh, meta=meta))
    if log:
        log.event(
            "alerts_written",
            stage="alerts",
            n_written=result.get("n_alerts_written", 0),
            n_repaired=result.get("n_alerts_repaired", 0),
            n_new_breaks=len(fresh),
        )
    result["status"] = "ok"
    return result


def reconcile_alerts(
    aoi_name: str,
    states: dict,
    grid: pd.DataFrame,
    fresh: list[dict] | None = None,
    meta: dict | None = None,
) -> dict:
    """Bring the alert table in line with the breaks S-CCD is holding.

    Normally this writes exactly the fresh breaks. When `meta['alerts_dirty']`
    survived from a previous run — meaning state was saved but the alert write
    did not complete — it enumerates every break in state and inserts the ones
    the table is missing. Alert ids are deterministic (`aoi:pix_id:ordinal`), so
    the repair cannot mint duplicates for breaks a human has already reviewed.
    """
    fresh = list(fresh or [])
    known = alerts_mod.known_ids(aoi_name)
    missing = [
        b
        for b in sccd_mod.all_breaks(states)
        if alerts_mod.alert_id(aoi_name, b["pix_id"], b["break_ordinal"]) not in known
    ]
    seen = {(b["pix_id"], b["break_ordinal"]) for b in fresh}
    to_write = fresh + [
        b for b in missing if (b["pix_id"], b["break_ordinal"]) not in seen
    ]
    if to_write:
        alerts_mod.record(aoi_name, to_write, grid)
    if meta is not None:
        meta["alerts_dirty"] = False
        state_mod.save_meta(aoi_name, meta)
    return {
        "n_alerts_written": len(to_write),
        "n_alerts_repaired": len(to_write) - len(fresh),
    }


def run_update(
    aoi_name: str,
    workers: int = config.SCENE_WORKERS,
    limit: int | None = None,
    sync: bool = True,
    batch_size: int = config.INGEST_BATCH,
    force_lock: bool = False,
    progress=None,
    echo_log: bool = True,
) -> dict:
    """One operational cycle: catch up the catalogue, ingest, advance S-CCD.

    This is the function a timer calls. It is safe to run when nothing is new —
    it exits having done nothing rather than re-processing, which is what makes
    a frequent schedule cheap. It holds the AOI's write lease for the duration,
    so a scheduler that fires twice (Kubernetes Jobs are explicitly at-least-once,
    and an operator with a terminal is faster than any scheduler) gets one
    worker, not two racing on the same ledger.
    """
    t0 = time.time()
    result = {
        "aoi": aoi_name,
        "started": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }

    with obslog.run_logger(
        aoi_name, "run", echo=echo_log, workers=workers, limit=limit
    ) as log:
        result["run_id"] = log.run_id
        with store.lease(aoi_name, what="run", force=force_lock):
            grid = aoi_mod.load_grid(aoi_name)
            if sync:
                cat = catalog.sync(aoi_name)
                log.event("catalog_synced", stage="catalog", n_scenes=len(cat))

            todo = pending_scenes(aoi_name, limit=limit)
            result["n_pending"] = int(len(todo))
            log.event(
                "pending_resolved",
                stage="catalog",
                n_pending=len(todo),
                oldest=str(todo["datetime"].min()) if len(todo) else None,
            )
            if len(todo):
                result["ingest"] = ingest(
                    aoi_name,
                    todo,
                    grid,
                    workers=workers,
                    batch_size=batch_size,
                    progress=progress,
                    log=log,
                )
                result["ingest"].pop("scene_ids", None)

            applied = apply_pending_observations(
                aoi_name, grid, progress=progress, log=log
            )
            result.update(applied)
            if not len(todo) and applied.get("status") == "nothing to apply":
                result["status"] = "nothing to do"

    result.setdefault("status", "ok")
    result["seconds"] = round(time.time() - t0, 1)
    return result
