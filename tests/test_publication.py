"""Tests for the confidence tier and the published artefacts.

These do not test whether the tiers are *good* -- that needs labels, which is
what the review queue is being built to collect. They test the properties the
publication path has to hold regardless of how well calibrated it is:

* re-scoring an already-scored table does not corrupt it,
* the season rule can actually fire,
* an unknown confirmation count is not read as a confirmed one,
* a gain that clears the gate is publishable, and `below_gate` never is,
* one pixel with several breaks publishes as ONE feature carrying both dates,
* the raster round-trips exactly and says nothing where nothing happened.

Two of these are regressions for bugs found on 2026-08-20: re-scoring collided
on the merge and raised KeyError on the *second* run, and no gain was publishable
at all despite the review form offering a "gain" verdict.

Nothing here touches the network.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from effektovervaking_forests import alerts as alerts_mod
from effektovervaking_forests import (
    confidence as conf_mod,
)
from effektovervaking_forests import (
    config,
    store,
)
from effektovervaking_forests import (
    publish as publish_mod,
)

AOI = "testaoi"


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
    yield tmp_path


def _grid(n=4):
    """A tiny grid on the real S2 lattice, so the raster geometry is realistic."""
    x0, y0 = 428175.0, 6770415.0
    rows = []
    for i in range(n):
        rows.append(
            {
                "pix_id": f"32632_{42817 + i}_677041",
                "utm_x": x0 + i * 10.0,
                "utm_y": y0,
                "lon": 7.6693 + i * 0.00018,
                "lat": 61.0619,
                "aoi": AOI,
                "utm_crs": "EPSG:32632",
                "poly_id": 1,
            }
        )
    return pd.DataFrame(rows)


def _breaks(grid, specs):
    """specs: (pixel index, iso date, magnitude, kind)."""
    out = []
    for i, date, mag, kind in specs:
        o = dt.date.fromisoformat(date).toordinal()
        out.append(
            {
                "pix_id": grid.iloc[i]["pix_id"],
                "break_ordinal": o,
                "break_date": date,
                "break_year": 2026.0,
                "magnitude": mag,
                "kind": kind,
            }
        )
    return out


# ── confidence ───────────────────────────────────────────────────────────────


def test_rescoring_an_already_scored_table_does_not_duplicate_columns():
    """Regression: the merge suffixed n_confirm_x/_y and broke on the 2nd run."""
    grid = _grid()
    alerts_mod.record(AOI, _breaks(grid, [(0, "2026-06-01", -0.20, "loss")]), grid)

    first = alerts_mod.rescore(AOI)
    second = alerts_mod.rescore(AOI)

    for col in conf_mod.OUTPUT_COLS:
        assert col in second.columns
        assert f"{col}_x" not in second.columns
        assert f"{col}_y" not in second.columns
    assert list(first.columns) == list(second.columns)
    assert len(first) == len(second)


def test_a_winter_break_cannot_be_published_above_low():
    """The optical record stops Nov-Feb; no magnitude makes that trustworthy."""
    grid = _grid()
    # Enormous magnitude -- would otherwise score high on the magnitude term.
    alerts_mod.record(AOI, _breaks(grid, [(0, "2026-01-15", -0.90, "loss")]), grid)
    df = alerts_mod.load(AOI)
    row = df[df["kind"] == "loss"].iloc[0]

    assert row["season_flag"] == "winter"
    assert row["conf_score"] <= conf_mod.SEASON_CAP
    assert row["confidence"] == "low"


def test_missing_observation_data_is_not_read_as_confirmation():
    """Unknown must score as no evidence, never as full marks."""
    grid = _grid()
    # No observation store exists in the sandbox, so n_confirm is unknown.
    alerts_mod.record(AOI, _breaks(grid, [(0, "2026-06-01", -0.076, "loss")]), grid)
    row = alerts_mod.load(AOI).iloc[0]

    # Just over the gate and with no confirmation evidence, this must not be high.
    assert row["confidence"] != "high"
    assert row["conf_score"] < conf_mod.MEDIUM_MAX


def test_tier_boundaries_are_ordered():
    assert conf_mod.tier(0.0) == "low"
    assert conf_mod.tier(conf_mod.LOW_MAX) == "medium"
    assert conf_mod.tier(conf_mod.MEDIUM_MAX) == "high"
    assert conf_mod.tier(float("nan")) == "low"


# ── what may be published ────────────────────────────────────────────────────


def test_a_gain_clearing_the_gate_is_publishable_but_stays_untriaged():
    """Regression: nothing gain-shaped was publishable, so the form's "gain"
    verdict had nothing on screen to attach to."""
    grid = _grid()
    alerts_mod.record(
        AOI,
        _breaks(
            grid,
            [
                (0, "2026-06-01", 0.30, "gain"),  # clears the gate
                (1, "2026-06-01", 0.01, "gain"),  # does not
            ],
        ),
        grid,
    )

    pub = alerts_mod.publishable(AOI)
    kinds = pub.groupby("kind").size().to_dict()
    assert kinds.get("gain") == 1

    # ...and its status is untouched, so triage still sees it as un-triaged.
    assert set(pub[pub["kind"] == "gain"]["status"]) == {"below_gate"}

    assert alerts_mod.publishable(AOI, include_gain=False).empty


def test_below_gate_losses_are_kept_but_never_published():
    grid = _grid()
    alerts_mod.record(AOI, _breaks(grid, [(0, "2026-06-01", -0.001, "loss")]), grid)

    assert len(alerts_mod.load(AOI)) == 1  # kept: re-gating stays a query
    assert alerts_mod.publishable(AOI).empty  # but not shown


def test_a_pixel_with_two_breaks_publishes_once_carrying_both_dates():
    grid = _grid()
    alerts_mod.record(
        AOI,
        _breaks(
            grid,
            [
                (0, "2026-03-01", -0.20, "loss"),
                (0, "2026-06-01", -0.30, "loss"),
            ],
        ),
        grid,
    )

    px = publish_mod.pixel_table(AOI, "loss")
    assert len(px) == 1
    row = px.iloc[0]
    assert row["first_loss_date"] == "2026-03-01"
    assert row["last_loss_date"] == "2026-06-01"
    assert row["n_events"] == 2
    # The alert_id must be the event the map is colouring -- the latest one.
    assert row["alert_id"].endswith(str(dt.date(2026, 6, 1).toordinal()))


# ── the raster ───────────────────────────────────────────────────────────────


def test_cog_roundtrips_dates_exactly_and_is_empty_where_nothing_happened(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    grid = _grid()
    alerts_mod.record(AOI, _breaks(grid, [(1, "2026-06-01", -0.30, "loss")]), grid)
    px = publish_mod.pixel_table(AOI, "loss")

    out = tmp_path / "alerts.tif"
    publish_mod.write_cog(px, grid, out)

    expected = (dt.date(2026, 6, 1) - publish_mod.EPOCH).days
    with rasterio.open(out) as src:
        assert src.count == 4
        assert src.nodata == publish_mod.NODATA

        hit = grid.iloc[1]
        vals = list(src.sample([(hit["utm_x"], hit["utm_y"])]))[0]
        assert int(vals[0]) == expected  # last_loss_date
        assert int(vals[1]) == expected  # first_loss_date
        assert int(vals[2]) == conf_mod.TIER_CODE[px.iloc[0]["confidence"]]
        assert int(vals[3]) == 1  # inside the AOI

        # A monitored pixel with no event says nothing, as required.
        miss = grid.iloc[0]
        v = list(src.sample([(miss["utm_x"], miss["utm_y"])]))[0]
        assert int(v[0]) == publish_mod.NODATA
        assert int(v[3]) == 1  # but is still in the mask


# ── state sidecar ────────────────────────────────────────────────────────────


def test_save_meta_merges_and_cannot_drop_recipe_version():
    """Regression: an operational loop that bootstrapped fine, then failed on
    every subsequent cycle with "fitted under recipe None".

    `state.save()` stamps provenance onto a COPY of the caller's dict, so the
    caller never gains `recipe_version`. When `reconcile_alerts` then cleared
    `alerts_dirty` through `save_meta`, a replacing write deleted the field from
    disk — and `state.load` refuses to run without it, pointing at a
    re-bootstrap that was not actually needed.
    """
    from effektovervaking_forests import state as state_mod

    # No pixels needed: this is about the sidecar, not the pickle.
    states = {}
    caller_meta = {"grid_fingerprint": "abc", "alerts_dirty": True}
    state_mod.save(AOI, states, caller_meta)

    on_disk = state_mod.load_meta(AOI)
    assert on_disk["recipe_version"] == config.RECIPE_VERSION
    # The bug in miniature: save() enriched only its own copy.
    assert "recipe_version" not in caller_meta

    # Exactly what reconcile_alerts does.
    caller_meta["alerts_dirty"] = False
    state_mod.save_meta(AOI, caller_meta)

    after = state_mod.load_meta(AOI)
    assert after["alerts_dirty"] is False  # the update applied
    assert after["recipe_version"] == config.RECIPE_VERSION  # and nothing was lost
    assert after["grid_fingerprint"] == "abc"


def test_manifest_keeps_aois_that_were_not_part_of_this_run(tmp_path, monkeypatch):
    """The nightly cycle publishes one AOI at a time; the second must not erase
    the first from the manifest."""
    import json

    monkeypatch.setattr(
        config,
        "AOI_PARQUET",
        {
            "kalklindeskog": tmp_path / "kalklindeskog.parquet",
            "olivinskog": tmp_path / "olivinskog.parquet",
        },
    )
    publish_mod.write_manifest(
        [{"aoi": "kalklindeskog", "outputs": {"x": 1}}], out_dir=tmp_path
    )
    publish_mod.write_manifest(
        [{"aoi": "olivinskog", "outputs": {"x": 2}}], out_dir=tmp_path
    )
    got = json.loads((tmp_path / "manifest.json").read_text())["aois"]
    assert [r["aoi"] for r in got] == ["kalklindeskog", "olivinskog"]
    # republishing one replaces its own entry only
    publish_mod.write_manifest(
        [{"aoi": "kalklindeskog", "outputs": {"x": 3}}], out_dir=tmp_path
    )
    got = {
        r["aoi"]: r["outputs"]["x"]
        for r in json.loads((tmp_path / "manifest.json").read_text())["aois"]
    }
    assert got == {"kalklindeskog": 3, "olivinskog": 2}
