"""Failure-injection tests for the operational pipeline's commit boundaries.

These do not test whether S-CCD detects disturbance — that is what the label
study is for. They test the properties that decide whether an unattended
overnight backfill and a scheduled NRT job can be trusted:

* an interruption never loses committed work and never double-counts on resume,
* a second invocation cannot race the first,
* a reprocessed product supersedes the one it replaces rather than joining it,
* observations that reached disk but not the model are replayed from disk,
* a failed alert write is repaired on the next run rather than lost.

Nothing here touches the network.
"""

from __future__ import annotations

import datetime as dt
import os

import numpy as np
import pandas as pd
import pytest

from effektovervaking_forests import alerts as alerts_mod
from effektovervaking_forests import bench, config, obs, pipeline, reader, runs, store
from effektovervaking_forests import sccd as sccd_mod

AOI = "testaoi"
BANDS = config.BANDS


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Point every write target at a temp dir, leaving real data alone."""
    for name in (
        "OPS",
        "GRID_DIR",
        "CATALOG_DIR",
        "STATE_DIR",
        "OBS_DIR",
        "ALERTS_DIR",
        "PARITY_DIR",
        "RUNS_DIR",
    ):
        d = tmp_path / name.lower()
        d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, name, d)
    monkeypatch.setattr(store, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(config, "OPERA_PARQUET", tmp_path / "opera.parquet")
    monkeypatch.setattr(config, "MONITORING_START", "2026-01-01")
    yield tmp_path


# ── helpers ──────────────────────────────────────────────────────────────────
def scene_id(sensing: str, proc: str, tile: str = "T32VNM") -> str:
    return f"S2A_MSIL2A_{sensing}_N0511_R008_{tile}_{proc}"


def obs_frame(
    scene: str, when: str, pix: list[str], clear: bool = True, value: int = 1000
) -> pd.DataFrame:
    df = pd.DataFrame({"pix_id": pix})
    for b in BANDS:
        df[b] = value
    df["scl"] = 4 if clear else 9
    df["scene_id"] = scene
    df["datetime"] = pd.Timestamp(when, tz="UTC")
    df["nodata"] = False
    df["clear"] = clear
    return df


def scene_row(sid: str, when: str) -> pd.Series:
    return pd.Series(
        {
            "id": sid,
            "datetime": pd.Timestamp(when, tz="UTC"),
            "tile": "T32VNM",
            "assets": {},
            "bbox": [0, 0, 1, 1],
        }
    )


def grid_frame(pix: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "pix_id": pix,
            "poly_id": 1,
            "lon": np.arange(len(pix), dtype=float),
            "lat": np.arange(len(pix), dtype=float),
            "utm_x": 0.0,
            "utm_y": 0.0,
            "utm_crs": "EPSG:32632",
            "home_tile": "T32VNM",
        }
    )


# ── the store ────────────────────────────────────────────────────────────────
def test_append_writes_parts_and_never_rewrites_them():
    a = obs.append(
        AOI,
        obs_frame(
            scene_id("20260101T104031", "20260101T120000"), "2026-01-01", ["p1", "p2"]
        ),
    )
    first = obs.parts_for(AOI, 2026)
    mtimes = {p: p.stat().st_mtime_ns for p in first}
    b = obs.append(
        AOI,
        obs_frame(
            scene_id("20260106T104031", "20260106T120000"), "2026-01-06", ["p1", "p2"]
        ),
    )
    assert a == b == 2
    assert len(obs.parts_for(AOI, 2026)) == 2
    # The first batch's file is untouched: appends are additive, not read-modify-write.
    assert all(p.stat().st_mtime_ns == mtimes[p] for p in first)
    assert len(obs.load(AOI)) == 4


def test_reprocessed_product_supersedes_rather_than_duplicates():
    """One acquisition, two product versions: the newer processing must win."""
    old = scene_id("20260101T104031", "20260101T120000")
    new = scene_id("20260101T104031", "20260305T090000")
    obs.append(AOI, obs_frame(old, "2026-01-01", ["p1"], value=1000))
    obs.append(AOI, obs_frame(new, "2026-01-01", ["p1"], value=2222))

    got = obs.load(AOI)
    assert len(got) == 1, "the same acquisition entered the series twice"
    assert got["B02"].iloc[0] == 2222
    assert got["scene_id"].iloc[0] == new


def test_compaction_is_idempotent_and_survives_being_repeated():
    for i, day in enumerate(["2026-01-01", "2026-01-06", "2026-01-11"]):
        obs.append(
            AOI,
            obs_frame(
                scene_id(f"2026010{i}T104031", "20260101T120000"), day, ["p1", "p2"]
            ),
        )
    n = obs.compact(AOI)
    assert n == {2026: 6}
    assert obs.parts_for(AOI, 2026) == []
    assert obs.compact(AOI) == {}  # nothing left to do
    assert len(obs.load(AOI)) == 6

    # A part landing after compaction merges cleanly into the existing file.
    obs.append(
        AOI,
        obs_frame(scene_id("20260116T104031", "20260116T120000"), "2026-01-16", ["p1"]),
    )
    assert len(obs.load(AOI)) == 7
    obs.compact(AOI)
    assert len(obs.load(AOI)) == 7


def test_clear_only_default_and_store_all_opt_in():
    cloudy = obs_frame(
        scene_id("20260101T104031", "20260101T120000"),
        "2026-01-01",
        ["p1"],
        clear=False,
    )
    assert obs.append(AOI, cloudy) == 0
    assert obs.append(AOI, cloudy, store_all=True) == 1
    assert len(obs.load(AOI, clear_only=False)) == 1
    assert len(obs.load(AOI)) == 0


# ── the ledger ───────────────────────────────────────────────────────────────
def test_ledger_stages_separate_stored_from_applied():
    rows = [
        {
            "scene_id": "a",
            "datetime": pd.Timestamp("2026-01-01", tz="UTC"),
            "n_pixels_read": 1,
            "n_clear": 1,
            "status": "ok",
            "seconds": 1.0,
            "message": "",
        }
    ]
    runs.record(AOI, rows)
    assert runs.processed_ids(AOI) == {"a"}, "a stored scene must not be re-read"
    assert runs.unapplied_scene_ids(AOI) == {"a"}, "…but is not done either"

    runs.mark_applied(AOI, {"a"})
    assert runs.unapplied_scene_ids(AOI) == set()
    assert runs.processed_ids(AOI) == {"a"}


def test_failed_scene_is_retried_but_successful_one_is_not():
    runs.record(
        AOI,
        [
            {
                "scene_id": "ok1",
                "datetime": pd.Timestamp("2026-01-01", tz="UTC"),
                "n_pixels_read": 1,
                "n_clear": 1,
                "status": "ok",
                "seconds": 1.0,
                "message": "",
            },
            {
                "scene_id": "bad",
                "datetime": pd.Timestamp("2026-01-02", tz="UTC"),
                "n_pixels_read": 0,
                "n_clear": 0,
                "status": "error",
                "seconds": 1.0,
                "message": "boom",
            },
        ],
    )
    assert runs.processed_ids(AOI) == {"ok1"}
    assert runs.processed_ids(AOI, include_failed=True) == {"ok1", "bad"}


# ── atomic writes and the lease ──────────────────────────────────────────────
def test_write_parquet_leaves_no_temp_file_and_replaces_atomically(tmp_path):
    target = tmp_path / "x.parquet"
    store.write_parquet(pd.DataFrame({"a": [1]}), target)
    store.write_parquet(pd.DataFrame({"a": [2, 3]}), target)
    assert len(pd.read_parquet(target)) == 2
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_second_invocation_is_refused_while_the_first_holds_the_lease():
    with store.lease(AOI, what="run"):
        with pytest.raises(store.LockBusy):
            with store.lease(AOI, what="run"):
                pass
    with store.lease(AOI, what="run"):  # released on exit
        pass


def test_lease_from_a_dead_process_is_taken_over():
    p = store.lock_path(AOI)
    store.write_json(
        {
            "aoi": AOI,
            "what": "run",
            "pid": 2**22,
            "host": os.uname().nodename,
            "acquired_at": store.now_iso(),
        },
        p,
    )
    with store.lease(AOI, what="run") as held:
        assert held["took_over_from"]["pid"] == 2**22


# ── ingest: batching and resumability ────────────────────────────────────────
def _fake_reader(monkeypatch, fail_on: set[str] | None = None):
    fail_on = fail_on or set()
    monkeypatch.setattr(
        pipeline.reader, "pixels_for_scene", lambda pixels, scene: pixels
    )

    def fake_read(scene, px):
        if scene["id"] in fail_on:
            raise RuntimeError("simulated CDSE failure")
        return obs_frame(scene["id"], str(scene["datetime"].date()), list(px["pix_id"]))

    monkeypatch.setattr(pipeline.reader, "read_scene", fake_read)


def test_ingest_commits_each_batch_so_an_interruption_keeps_earlier_work(monkeypatch):
    grid = grid_frame(["p1", "p2"])
    days = pd.date_range("2026-01-01", periods=6, freq="5D", tz="UTC")
    scenes = pd.DataFrame(
        [
            scene_row(scene_id(f"{d:%Y%m%d}T104031", "20260101T120000"), str(d.date()))
            for d in days
        ]
    )
    _fake_reader(monkeypatch)

    # Interrupt in the middle of the third batch of two.
    real_append = obs.append
    calls = {"n": 0}

    def flaky_append(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt("killed mid-run")
        return real_append(*a, **kw)

    monkeypatch.setattr(pipeline.obs, "append", flaky_append)

    with pytest.raises(KeyboardInterrupt):
        pipeline.ingest(AOI, scenes, grid, workers=2, batch_size=2)

    assert len(runs.processed_ids(AOI)) == 4, "two committed batches must survive"
    assert len(obs.load(AOI)) == 8

    # Resume: only the uncommitted scenes are re-read, and nothing is doubled.
    monkeypatch.setattr(pipeline.obs, "append", real_append)
    todo = scenes[~scenes["id"].isin(runs.processed_ids(AOI))]
    assert len(todo) == 2
    pipeline.ingest(AOI, todo, grid, workers=2, batch_size=2)
    assert len(obs.load(AOI)) == 12
    assert obs.load(AOI).duplicated(subset=obs.KEY).sum() == 0


def test_ingest_records_failures_so_they_are_retried_not_forgotten(monkeypatch):
    grid = grid_frame(["p1"])
    scenes = pd.DataFrame(
        [
            scene_row(scene_id("20260101T104031", "20260101T120000"), "2026-01-01"),
            scene_row(scene_id("20260106T104031", "20260106T120000"), "2026-01-06"),
        ]
    )
    _fake_reader(monkeypatch, fail_on={scenes.iloc[1]["id"]})

    got = pipeline.ingest(AOI, scenes, grid, workers=1, batch_size=5)
    assert got["n_ok"] == 1 and got["n_error"] == 1
    led = runs.load(AOI)
    assert set(led["status"]) == {"ok", "error"}
    assert scenes.iloc[1]["id"] not in runs.processed_ids(AOI)


# ── model-facing invariants ──────────────────────────────────────────────────
def test_observations_behind_the_watermark_are_routed_to_refit_not_update():
    new_obs = pd.concat(
        [
            obs_frame(
                scene_id("20260101T104031", "20260305T090000"), "2026-01-01", ["p1"]
            ),
            obs_frame(
                scene_id("20260310T104031", "20260310T120000"), "2026-03-10", ["p1"]
            ),
        ],
        ignore_index=True,
    )
    states = {"p1": object()}
    forward, late = sccd_mod.split_by_watermark(
        new_obs, states, pd.Timestamp("2026-02-01", tz="UTC")
    )
    assert len(forward) == 1 and len(late) == 1
    assert late["datetime"].iloc[0] == pd.Timestamp("2026-01-01", tz="UTC")


def test_a_pixel_with_no_state_is_named_not_just_counted():
    """Sparse pixels must be re-fittable later, so update() has to report which."""
    new_obs = obs_frame(
        scene_id("20260101T104031", "20260101T120000"), "2026-01-01", ["ghost"]
    )
    _, fresh, stats = sccd_mod.update({}, new_obs)
    assert fresh == []
    assert stats["pixels_without_state"] == ["ghost"]


# ── alerts ───────────────────────────────────────────────────────────────────
def _break(pix: str, ordinal: int, mag: float, kind: str = "loss") -> dict:
    return {
        "pix_id": pix,
        "break_ordinal": ordinal,
        "break_date": dt.date.fromordinal(ordinal).isoformat(),
        "break_year": 2026.0,
        "magnitude": mag,
        "kind": kind,
    }


def test_bootstrap_breaks_before_monitoring_start_are_historical_not_new():
    grid = grid_frame(["p1", "p2"])
    old = dt.date(2019, 6, 1).toordinal()
    recent = dt.date(2026, 6, 1).toordinal()
    tbl = alerts_mod.record(
        AOI, [_break("p1", old, -0.5), _break("p2", recent, -0.5)], grid
    )
    by = dict(zip(tbl["pix_id"], tbl["status"]))
    assert by["p1"] == "historical"
    assert by["p2"] == "new"


def test_a_human_verdict_survives_a_refit_that_re_derives_the_same_break():
    grid = grid_frame(["p1"])
    ordinal = dt.date(2026, 6, 1).toordinal()
    alerts_mod.record(AOI, [_break("p1", ordinal, -0.5)], grid)

    tbl = alerts_mod.load(AOI)
    tbl.loc[:, "status"] = "rejected"
    store.write_parquet(tbl, alerts_mod.alerts_path(AOI))

    alerts_mod.record(AOI, [_break("p1", ordinal, -0.5)], grid)
    out = alerts_mod.load(AOI)
    assert len(out) == 1
    assert out["status"].iloc[0] == "rejected"


class _FakePack:
    """Just enough of an SccdOutput for breaks_from_pack."""

    def __init__(self, ordinals):
        mag = np.zeros(6, dtype=float)
        mag[3], mag[5] = -5000.0, 0.0  # NIR down, SWIR2 flat -> loss
        self.rec_cg = np.array(
            [(o, mag) for o in ordinals],
            dtype=[("t_break", "i8"), ("magnitude", "f8", (6,))],
        )


def test_a_failed_alert_write_is_repaired_from_state_on_the_next_run():
    grid = grid_frame(["p1"])
    ordinal = dt.date(2026, 6, 1).toordinal()
    states = {"p1": _FakePack([ordinal])}

    # State holds a break the alert table has never seen (the previous run died
    # between saving state and writing alerts).
    assert alerts_mod.known_ids(AOI) == set()
    res = pipeline.reconcile_alerts(AOI, states, grid, fresh=[])
    assert res["n_alerts_repaired"] == 1
    assert alerts_mod.load(AOI)["status"].iloc[0] == "new"

    # Running it again is a no-op rather than a duplicate.
    res = pipeline.reconcile_alerts(AOI, states, grid, fresh=[])
    assert res["n_alerts_written"] == 0
    assert len(alerts_mod.load(AOI)) == 1


# ── replay: stored-but-unapplied observations ────────────────────────────────
def test_unapplied_observations_are_replayed_from_disk_not_re_read(monkeypatch):
    """The gap between 'in the store' and 'in the model' must close by itself."""
    grid = grid_frame(["p1"])
    sid = scene_id("20260301T104031", "20260301T120000")
    obs.append(AOI, obs_frame(sid, "2026-03-01", ["p1"]))
    runs.record(
        AOI,
        [
            {
                "scene_id": sid,
                "datetime": pd.Timestamp("2026-03-01", tz="UTC"),
                "n_pixels_read": 1,
                "n_clear": 1,
                "status": "ok",
                "seconds": 1.0,
                "message": "",
            }
        ],
    )

    saved: dict = {}
    ordinal = dt.date(2026, 3, 1).toordinal()
    monkeypatch.setattr(pipeline.aoi_mod, "grid_fingerprint", lambda g: "fp")
    monkeypatch.setattr(
        pipeline.state_mod,
        "load",
        lambda a, grid_fingerprint=None: (
            {"p1": _FakePack([])},
            {"last_observation": "2026-02-01 00:00:00+00:00"},
        ),
    )
    monkeypatch.setattr(
        pipeline.state_mod,
        "save",
        lambda a, st, meta: saved.update(state=st, meta=dict(meta)),
    )
    monkeypatch.setattr(
        pipeline.state_mod, "save_meta", lambda a, meta: saved.update(meta=dict(meta))
    )

    seen: dict = {}

    def fake_update(states, new_obs, progress=None):
        seen["n"] = len(new_obs)
        states["p1"] = _FakePack([ordinal])
        return (
            states,
            [],
            {
                "n_pixels_updated": 1,
                "n_pixels_without_state": 0,
                "n_update_failures": 0,
                "n_new_breaks": 0,
                "pixels_without_state": [],
            },
        )

    monkeypatch.setattr(pipeline.sccd_mod, "update", fake_update)

    res = pipeline.apply_pending_observations(AOI, grid)

    assert seen["n"] == 1, "the stored observation never reached the model"
    assert res["n_unapplied_scenes"] == 1
    assert runs.unapplied_scene_ids(AOI) == set(), "ledger must advance after save"
    assert saved["meta"]["last_observation"].startswith("2026-03-01")
    assert saved["meta"]["alerts_dirty"] is False, "alerts were written"
    # The break the model produced is in the alert table, from state.
    assert len(alerts_mod.load(AOI)) == 1

    # A second cycle with nothing new is a genuine no-op.
    assert (
        pipeline.apply_pending_observations(AOI, grid)["status"] == "nothing to apply"
    )


# ── the inspector and the operational run must not drift apart ───────────────
# The inspector (Geethen_forests/scripts/c2c_ts_server.py) and the label-tuning
# results live outside this repository. Point these at them to cross-check;
# otherwise the pinned values below are the contract.
INSPECTOR_SRC = os.environ.get("FORESTMON_INSPECTOR_SRC")
LABEL_TUNING_CSV = os.environ.get("FORESTMON_LABEL_TUNING_CSV")


def test_sccd_parameters_are_pinned():
    """The deployed S-CCD configuration (p_cg 0.90, conse 6, lambda 20) is the
    one the label study scored. Changing it is a recalibration, not a refactor.
    """
    assert (config.SCCD_P_CG, config.SCCD_CONSE, config.PYX_LAM) == (0.90, 6, 20)
    assert alerts_mod.LOSS_GATE == 0.075


@pytest.mark.skipif(not INSPECTOR_SRC, reason="FORESTMON_INSPECTOR_SRC not set")
def test_sccd_parameters_match_the_inspector():
    """An operational alert has to be reproducible in the inspector a scientist
    is looking at. These constants are duplicated in two files by necessity (the
    inspector is a standalone GEE server), so assert they are the same rather
    than hoping.

    Parsed from source rather than imported: `c2c_ts_server.py` pulls in Earth
    Engine at import time, and this test must run offline.
    """
    import re
    from pathlib import Path

    src = Path(INSPECTOR_SRC).read_text()
    m = re.search(r"^SCCD_P_CG,\s*SCCD_CONSE\s*=\s*([\d.]+),\s*(\d+)", src, re.M)
    lam = re.search(r"^PYX_LAM\s*=\s*(\d+)", src, re.M)
    assert m and lam, "could not find the inspector's S-CCD constants"
    assert float(m.group(1)) == config.SCCD_P_CG
    assert int(m.group(2)) == config.SCCD_CONSE
    assert int(lam.group(1)) == config.PYX_LAM


@pytest.mark.skipif(not LABEL_TUNING_CSV, reason="FORESTMON_LABEL_TUNING_CSV not set")
def test_loss_gate_is_the_label_tuned_argmax():
    """`alerts.LOSS_GATE` is the single most consequential number in the system —
    an ungated break flags over half of all stable pixels. Pin it to the tuning
    result it claims to come from.
    """
    df = pd.read_csv(LABEL_TUNING_CSV)
    cur = df[(df.method == "sccd") & (df.is_current) & (df.task == "loss")]
    best = cur.sort_values("f1", ascending=False).iloc[0]
    assert float(best["gate"]) == alerts_mod.LOSS_GATE, (
        f"LOSS_GATE={alerts_mod.LOSS_GATE} but the label-tuned argmax is "
        f"{best['gate']} (F1 {best['f1']:.3f})"
    )


# ── the transport benchmark must measure the settings it claims to ───────────
def test_read_scene_applies_gdal_overrides_inside_every_band_worker(monkeypatch):
    import contextlib

    calls = []

    def fake_env(**overrides):
        calls.append(dict(overrides))
        return contextlib.nullcontext()

    def fake_read(path, xs, ys, crs):
        return np.ones(len(xs), dtype="int32"), np.ones(len(xs), dtype=bool)

    monkeypatch.setattr(reader, "_gdal_env", fake_env)
    monkeypatch.setattr(reader, "_read_at", fake_read)
    assets = {
        asset: f"s3://test/{asset}.jp2"
        for asset in [*config.ASSET_FOR_BAND.values(), config.SCL_ASSET]
    }
    scene = pd.Series(
        {
            "id": "scene",
            "datetime": pd.Timestamp("2026-01-01", tz="UTC"),
            "assets": assets,
        }
    )
    pixels = pd.DataFrame(
        {"pix_id": ["p1"], "utm_x": [1.0], "utm_y": [2.0], "utm_crs": ["EPSG:32632"]}
    )

    reader.read_scene(
        scene,
        pixels,
        apply_offset=False,
        scl_first=False,
        gdal_overrides={"GDAL_HTTP_VERSION": "2"},
    )

    assert len(calls) == 7
    assert all(c == {"GDAL_HTTP_VERSION": "2"} for c in calls)


def test_transport_parity_rejects_missing_rows_and_changed_cells():
    base = obs_frame("scene", "2026-01-01", ["p1", "p2"])
    same_reordered = base.iloc[::-1].reset_index(drop=True)
    assert bench._same_scene_values(base, same_reordered) == (True, 0)

    missing = base.iloc[:1]
    assert bench._same_scene_values(base, missing)[0] is False

    changed = base.copy()
    changed.loc[0, "B08"] += 1
    assert bench._same_scene_values(base, changed) == (False, 1)
