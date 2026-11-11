"""Tests for the dashboard baker (`forestmon publish dashboard`).

A tiny synthetic source laid out like the published S3 prefix: pixels with a
known number of events, polygons with a known area. Nothing touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box
from typer.testing import CliRunner

from effektovervaking_forests import dashboard
from effektovervaking_forests.cli import app

runner = CliRunner()

# Olivinskog: two 100 m squares = 2 ha. Kalklindeskog: one 200 m square = 4 ha.
OLIVIN_HA, KALK_HA = 2.0, 4.0

_OLIVIN = [  # pix_id, break_date, kind[, status, magnitude]
    (1, "2024-01-15", "loss"),
    (1, "2024-03-10", "loss"),  # same pixel again: area counts it once
    (2, "2024-03-05", "loss"),
    (3, "2024-02-01", "gain", "below_gate", 0.2),  # gains clearing the gate show
    # Never shown: sub-gate breaks, and losses a reviewer rejected.
    (4, "2024-03-20", "loss", "below_gate", -0.01),
    (5, "2024-03-21", "loss", "rejected", -0.2),
    (6, "2024-02-02", "gain", "below_gate", 0.01),
]
_KALK = [(10, "2024-02-20", "loss"), (11, "2024-02-25", "loss")]


def _alerts(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "pix_id": [r[0] for r in rows],
            "lon": [15.0 + r[0] * 1e-4 for r in rows],
            "lat": [59.6] * len(rows),
            "break_date": [r[1] for r in rows],
            "kind": [r[2] for r in rows],
            "conf_score": [0.9] * len(rows),
            "status": [r[3] if len(r) > 3 else "historical" for r in rows],
            "magnitude": [r[4] if len(r) > 4 else -0.2 for r in rows],
            "gate": [0.075] * len(rows),
        }
    )


def make_source(root: Path, forest_types=("olivinskog", "kalklindeskog")) -> Path:
    """Write the synthetic alerts + polygons under root, like the real prefix."""
    (root / "alerts/parquet").mkdir(parents=True)
    (root / "condition/parquet").mkdir(parents=True)
    _alerts(_OLIVIN).to_parquet(root / "alerts/parquet/olivinskog_alerts_all.parquet")
    _alerts(_KALK).to_parquet(root / "alerts/parquet/kalklindeskog_alerts_all.parquet")
    x, y = 300_000, 6_600_000
    shapes = {
        "olivinskog": [
            box(x, y, x + 100, y + 100),
            box(x + 200, y, x + 300, y + 100),
            box(x, y, x + 100, y + 100),  # another mapping layer, same stand
        ],
        "kalklindeskog": [box(x, y + 500, x + 200, y + 700)],
    }
    rows = [
        (f"{ft}-{i}", ft, "naturtype", "Skog", g)
        for ft in forest_types
        for i, g in enumerate(shapes[ft])
    ]
    gpd.GeoDataFrame(
        rows,
        columns=["poly_id", "forest_type", "layer", "naturtype", "geometry"],
        crs=25833,
    ).to_parquet(root / "condition/parquet/polygons_geo.parquet")
    pd.DataFrame(
        {
            "poly_id": [r[0] for r in rows],
            "tilstand": "god",
            "survey_year": 2020,
            "condition_local": "good_condition",
            "condition_global": "good_condition",
            "dor_global_2025": 0.6,
            "trajectory": "stable",
        }
    ).to_parquet(root / "condition/parquet/condition_stands.parquet")
    return root


@pytest.fixture
def baked(tmp_path: Path):
    out = tmp_path / "out"
    idx = dashboard.bake(str(make_source(tmp_path / "src")), out)
    return idx, out


def _monthly(out: Path) -> dict[tuple[str, str], dict]:
    rows = json.loads((out / "monthly.json").read_text())
    return {(r["forest_type"], r["period"]): r for r in rows}


def test_pixel_with_two_losses_counts_once_and_maps_latest(baked) -> None:
    idx, out = baked
    assert idx["pixels"]["olivinskog"] == {"loss": 2, "gain": 1}
    assert idx["pixels"]["kalklindeskog"] == {"loss": 2, "gain": 0}
    pts = json.loads((out / "points_olivinskog_loss.json").read_text())
    by_lon = dict(
        zip(pts["lon"], zip(pts["month"], pts["n"], strict=True), strict=True)
    )
    assert by_lon[15.0001] == ("2024-03", 2)  # latest month, both events
    assert by_lon[15.0002] == ("2024-03", 1)


def test_monthly_cover_is_baseline_minus_cumulative_loss(baked) -> None:
    idx, out = baked
    assert idx["baseline_ha"] == {"olivinskog": OLIVIN_HA, "kalklindeskog": KALK_HA}
    assert idx["period_range"] == ["2024-01", "2024-03"]
    m = _monthly(out)
    # Olivinskog: pixel 1 first breaks in Jan (its March event is not new area).
    assert m["olivinskog", "2024-01"]["loss_ha"] == 0.01
    assert m["olivinskog", "2024-02"]["loss_ha"] == 0.0
    assert m["olivinskog", "2024-02"]["gain_ha"] == 0.01
    assert m["olivinskog", "2024-03"]["loss_ha"] == 0.01
    assert m["olivinskog", "2024-03"]["cumulative_loss_ha"] == 0.02
    assert m["olivinskog", "2024-03"]["area_ha"] == OLIVIN_HA - 0.02
    assert m["kalklindeskog", "2024-01"]["area_ha"] == KALK_HA
    assert m["kalklindeskog", "2024-03"]["cumulative_loss_ha"] == 0.02
    assert m["kalklindeskog", "2024-03"]["area_ha"] == KALK_HA - 0.02


def test_index_lists_existing_files_and_columns_align(baked) -> None:
    idx, out = baked
    assert idx["schema"] == dashboard.SCHEMA
    assert json.loads((out / "index.json").read_text())["schema"] == dashboard.SCHEMA
    named = [idx["files"]["polygons"], idx["files"]["monthly"]]
    for kinds in idx["files"]["points"].values():
        assert set(kinds) == set(dashboard.KINDS)
        named += kinds.values()
    assert len(named) == 2 + 4
    for name in named:
        assert (out / name).is_file(), name
    for kinds in idx["files"]["points"].values():
        for name in kinds.values():
            cols = json.loads((out / name).read_text())
            assert len({len(v) for v in cols.values()}) == 1


def test_missing_polygons_for_an_aoi_raises(tmp_path: Path) -> None:
    src = make_source(tmp_path / "src", forest_types=("olivinskog",))
    with pytest.raises(ValueError, match="kalklindeskog"):
        dashboard.bake(str(src), tmp_path / "out")


def test_cli_publish_dashboard(tmp_path: Path) -> None:
    src, out = make_source(tmp_path / "src"), tmp_path / "out"
    result = runner.invoke(
        app, ["publish", "dashboard", "--source", str(src), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert (out / "index.json").is_file()


def test_cli_publish_dashboard_needs_a_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FORESTMON_DASHBOARD_SOURCE", raising=False)
    result = runner.invoke(app, ["publish", "dashboard", "--out", str(tmp_path / "o")])
    assert result.exit_code != 0
    assert "FORESTMON_DASHBOARD_SOURCE" in result.output
    assert not (tmp_path / "o").exists()


def test_unpublishable_breaks_and_overlaps_do_not_count(baked):
    idx, out = baked
    assert idx["pixels"]["olivinskog"] == {"loss": 2, "gain": 1}
    assert idx["baseline_ha"]["olivinskog"] == pytest.approx(OLIVIN_HA)
