"""A two-zone AOI driven end to end through ingest and model application.

`test_multizone.py` pins the grid, tile assignment and reader filter one unit at
a time; this checks that they hold together across the commit boundaries:
ingest -> observation store -> ledger -> `apply_pending_observations` -> state
and alerts. The reader and S-CCD are faked (no network, no pyxccd fits); the
grid, tile filter, store, ledger and alert table are the real ones.
"""

from __future__ import annotations

import datetime as dt

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import box

from effektovervaking_forests import alerts as alerts_mod
from effektovervaking_forests import aoi as aoi_mod
from effektovervaking_forests import config, obs, pipeline, runs, store
from effektovervaking_forests import state as state_mod

AOI = "zonepipe"
BANDS = config.BANDS
TILES = {"32VKN": "EPSG:32632", "33WVQ": "EPSG:32633"}
DAYS = ["2026-03-01", "2026-03-06"]
BREAK = dt.date(2026, 3, 5).toordinal()


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    for name in (
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
    # Bergen (zone 32) and Nordland (zone 33), as in test_multizone.
    polys = gpd.GeoDataFrame(
        {"union_id": [10, 20]},
        geometry=[
            box(5.300, 60.400, 5.302, 60.401),
            box(15.000, 68.000, 15.003, 68.001),
        ],
        crs=4326,
    )
    p = tmp_path / "zonepipe.parquet"
    polys.to_parquet(p)
    monkeypatch.setitem(config.AOI_PARQUET, AOI, p)
    yield tmp_path


class _FakePack:
    """Just enough of an SccdOutput for breaks_from_pack."""

    def __init__(self, ordinals):
        mag = np.zeros(6, dtype=float)
        mag[3] = -5000.0  # NIR down, SWIR2 flat -> loss
        self.rec_cg = np.array(
            [(o, mag) for o in ordinals],
            dtype=[("t_break", "i8"), ("magnitude", "f8", (6,))],
        )


@pytest.fixture
def grid():
    aoi_mod.save_grid(AOI, aoi_mod.build_grid(AOI))
    huge = (-1e7, -1e8, 1e7, 1e8)  # each tile "covers" every coordinate
    return aoi_mod.assign_home_tiles(AOI, {t: (huge, c) for t, c in TILES.items()})


def scene_rows(grid):
    """One scene per (tile, day). The bbox covers everything, so only the
    home-tile / zone filter decides what a scene is offered."""
    rows = []
    for tile in TILES:
        for day in DAYS:
            stamp = day.replace("-", "")
            rows.append(
                {
                    "id": f"S2A_MSIL2A_{stamp}T104031_N0511_R008_T{tile}_20260401T1200",
                    "datetime": pd.Timestamp(day, tz="UTC"),
                    "tile": tile,
                    "assets": {},
                    "bbox": [-180, -90, 180, 90],
                }
            )
    return pd.DataFrame(rows)


@pytest.fixture
def offered(monkeypatch):
    """Fake the S3 read; record which pixels each scene was actually offered."""
    seen: dict[str, pd.DataFrame] = {}

    def fake_read(scene, px):
        seen[scene["id"]] = px
        df = pd.DataFrame({"pix_id": list(px["pix_id"])})
        for b in BANDS:
            df[b] = 1000
        df["scl"] = 4
        df["scene_id"] = scene["id"]
        df["datetime"] = pd.Timestamp(scene["datetime"])
        df["nodata"] = False
        df["clear"] = True
        return df

    monkeypatch.setattr(pipeline.reader, "read_scene", fake_read)
    return seen


@pytest.fixture
def model(monkeypatch, grid):
    """In-memory S-CCD: state starts as one pack per grid pixel; update() gives
    the first pixel of each zone a break and records what it was fed."""
    held = {"states": {p: _FakePack([]) for p in grid["pix_id"]}, "fed": [], "saves": 0}

    def fake_load(a, grid_fingerprint=None):
        return held["states"], {"last_observation": "2026-02-01 00:00:00+00:00"}

    def fake_save(a, st, meta):
        held["states"] = st
        held["saves"] += 1
        state_mod.save_meta(a, meta)

    def fake_update(states, new_obs, progress=None):
        held["fed"].append(new_obs.copy())
        for _, first in grid.groupby("utm_crs")["pix_id"].first().items():
            states[first] = _FakePack([BREAK])
        stats = {
            "n_pixels_updated": int(new_obs["pix_id"].nunique()),
            "n_pixels_without_state": 0,
            "n_update_failures": 0,
            "n_new_breaks": 0,
            "pixels_without_state": [],
        }
        return states, [], stats

    monkeypatch.setattr(state_mod, "load", fake_load)
    monkeypatch.setattr(state_mod, "save", fake_save)
    monkeypatch.setattr(pipeline.sccd_mod, "update", fake_update)
    return held


def test_pix_ids_embed_their_epsg_code_and_never_collide(grid):
    assert grid["pix_id"].is_unique
    assert set(grid["utm_crs"]) == set(TILES.values())
    assert (grid["pix_id"].str[:5] == grid["utm_crs"].str[5:]).all()
    # The same easting/northing on two lattices is two different pixels.
    assert grid["pix_id"].str.fullmatch(r"326\d\d_\d+_\d+").all()
    assert (
        grid["home_tile"] == grid["utm_crs"].map({v: k for k, v in TILES.items()})
    ).all()


def test_each_scene_is_offered_only_its_own_zone_and_tile(grid, offered):
    scenes = scene_rows(grid)
    pipeline.ingest(AOI, scenes, grid, workers=2, batch_size=3)

    assert set(offered) == set(scenes["id"]), "every scene must be tried"
    by_pix = grid.set_index("pix_id")
    for _, sc in scenes.iterrows():
        px = offered[sc["id"]]
        want = set(grid.loc[grid["home_tile"] == sc["tile"], "pix_id"])
        assert len(px) and set(px["pix_id"]) == want
        assert (by_pix.loc[px["pix_id"], "utm_crs"] == TILES[sc["tile"]]).all()


def test_observations_land_keyed_by_pixel_and_acquisition(grid, offered):
    pipeline.ingest(AOI, scene_rows(grid), grid, workers=2, batch_size=3)

    got = obs.load(AOI)
    assert len(got) == len(grid) * len(DAYS), "one row per pixel per acquisition"
    assert got.duplicated(subset=obs.KEY).sum() == 0
    assert set(got["pix_id"]) == set(grid["pix_id"])
    # A pixel's series only ever comes from its own tile's scenes.
    tile_of = grid.set_index("pix_id")["home_tile"]
    assert (
        got["scene_id"].str.extract(r"_T(\w{5})_")[0].to_numpy()
        == tile_of.loc[got["pix_id"]].to_numpy()
    ).all()
    # Both zones share each acquisition date without sharing a row.
    per_day = got.groupby("datetime")["pix_id"].nunique()
    assert (per_day == len(grid)).all()
    assert set(runs.processed_ids(AOI)) == set(scene_rows(grid)["id"])


def test_state_covers_every_grid_pixel_and_rerun_is_a_noop(grid, offered, model):
    scenes = scene_rows(grid)
    pipeline.ingest(AOI, scenes, grid, workers=2, batch_size=3)
    assert runs.unapplied_scene_ids(AOI) == set(scenes["id"])

    res = pipeline.apply_pending_observations(AOI, grid)

    assert res["status"] == "ok" and res["n_unapplied_scenes"] == len(scenes)
    assert runs.unapplied_scene_ids(AOI) == set()
    assert set(model["states"]) == set(grid["pix_id"])
    fed = pd.concat(model["fed"])
    assert set(fed["pix_id"]) == set(grid["pix_id"])
    # One break per zone, each keyed to a pixel of that zone.
    al = alerts_mod.load(AOI)
    assert len(al) == 2 and al["alert_id"].is_unique
    assert set(al["pix_id"].str[:5]) == {"32632", "32633"}
    assert al["alert_id"].str.startswith(f"{AOI}:3263").all()
    assert (al["status"] == "new").all()

    # Idempotent: nothing re-read, re-fed, re-saved or re-alerted.
    before = (len(obs.load(AOI)), model["saves"], len(alerts_mod.load(AOI)))
    again = pipeline.apply_pending_observations(AOI, grid)
    assert again["status"] == "nothing to apply"
    pipeline.ingest(AOI, scenes[~scenes["id"].isin(runs.processed_ids(AOI))], grid)
    after = (len(obs.load(AOI)), model["saves"], len(alerts_mod.load(AOI)))
    assert after == before
