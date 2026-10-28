"""Read-only smoke test of the deployed artefacts for real AOIs.

Opt-in: nothing runs unless FORESTMON_SMOKE_AOIS names the AOIs, e.g.

    FORESTMON_DATA_DIR=... FORESTMON_AOI_DIR=... \\
    FORESTMON_SMOKE_AOIS="olivinskog kalklindeskog" uv run pytest -q -m smoke

It uses the configured paths as they are (no sandbox) and only ever reads. It
checks that the grid, the S-CCD state, the alert table and the ledger of a
deployment still agree with one another -- the properties a multi-zone AOI such
as olivinskog (UTM 32/33/34/35) can silently lose.
"""

from __future__ import annotations

import os

import pandas as pd
import pytest

from effektovervaking_forests import alerts as alerts_mod
from effektovervaking_forests import aoi as aoi_mod
from effektovervaking_forests import runs, store
from effektovervaking_forests import state as state_mod

AOIS = os.environ.get("FORESTMON_SMOKE_AOIS", "").split()

pytestmark = [
    pytest.mark.smoke,
    pytest.mark.skipif(not AOIS, reason="FORESTMON_SMOKE_AOIS not set"),
]


@pytest.fixture(scope="module", params=AOIS or ["unset"])
def aoi(request):
    return request.param


@pytest.fixture(scope="module")
def grid(aoi):
    return aoi_mod.load_grid(aoi)


@pytest.fixture(scope="module")
def loaded_state(aoi):
    """(states, meta). The pickle is hundreds of MB: load it once per AOI."""
    return state_mod.load(aoi)


def test_grid_loads_with_unique_pixel_ids_that_embed_their_zone(grid):
    assert len(grid) and grid["pix_id"].is_unique
    assert (grid["pix_id"].str.split("_").str[0] == grid["utm_crs"].str[5:]).all()
    assert (grid["home_tile"].fillna("") != "").all(), "pixel without a home tile"


def test_state_covers_exactly_the_grid_and_was_fitted_on_it(grid, loaded_state):
    states, meta = loaded_state
    assert set(states) == set(grid["pix_id"])
    assert meta["grid_fingerprint"] == aoi_mod.grid_fingerprint(grid)
    assert meta["n_pixels"] == len(grid)


def test_alerts_have_the_documented_columns_and_unique_ids(aoi, grid):
    want = list(alerts_mod.load("__no_such_aoi__").columns)  # empty-table schema
    al = alerts_mod.load(aoi)
    assert len(al), f"no alerts for {aoi}: missing or empty alerts parquet"
    assert not set(want) - set(al.columns), set(want) - set(al.columns)
    assert al["alert_id"].is_unique
    assert al["pix_id"].isin(grid["pix_id"]).all(), "alert for a pixel not in the grid"
    assert (al["aoi"] == aoi).all()


def test_no_scene_is_stuck_below_state_applied(aoi):
    # `health` only warns about stored-not-applied scenes (the next run replays
    # them); a deployment at rest should have none. A live run legitimately does.
    if store.lease_holder(aoi):
        pytest.skip("a run holds the lease; unapplied scenes are expected")
    led = runs.load(aoi)
    assert len(led)
    stuck = led[(led["status"] == "ok") & (led["stage"] != runs.STAGE_APPLIED)]
    assert stuck.empty, f"{len(stuck)} scenes stored but not applied"
    assert runs.unapplied_scene_ids(aoi) == set()
    assert not pd.isna(led["datetime"]).any()
