"""Tests for the dashboard baker (`forestmon publish dashboard`).

A tiny synthetic source laid out like the published S3 prefix: pixels with a
known number of events, polygons with a known area. Nothing touches the network.
"""

from __future__ import annotations

import functools
import http.server
import json
import threading
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box
from typer.testing import CliRunner

from effektovervaking_forests import dashboard
from effektovervaking_forests.cli import app

runner = CliRunner()

# Olivinskog: two 100 m squares. Kalklindeskog: one 200 m square.

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
    """Write the synthetic alerts + C2C under root, like the real prefix, and the
    condition folder (as the dms dataset) at root/condition."""
    alerts, cond = root / "sccd/operational/alerts", root / "condition"
    alerts.mkdir(parents=True)
    cond.mkdir(parents=True)
    _alerts(_OLIVIN).to_parquet(alerts / "olivinskog_alerts.parquet")
    _alerts(_KALK).to_parquet(alerts / "kalklindeskog_alerts.parquet")
    # Only checked for: the app reads these from the condition folder.
    for name in ("condition_pixels.parquet", "condition_pixels.pmtiles", "b1.tif"):
        (cond / name).write_bytes(b"x")
    (cond / "index.csv").write_text("file,block\nb1.tif,1\n")
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
    ).to_parquet(cond / "polygons_geo.parquet")
    pd.DataFrame(
        {
            "poly_id": [r[0] for r in rows],
            "tilstand": "god",
            "survey_year": 2020,
            "condition_local": "good_condition",
            "condition_global": "good_condition",
            "threshold_global": 0.51,
            "trajectory": "stable",
            # 2017 .. 2025: 0.50, 0.51, ..., 0.58
            **{f"dor_global_{y}": 0.5 + (y - 2017) / 100 for y in range(2017, 2026)},
        }
    ).to_parquet(cond / "condition_stands.parquet")
    (root / "c2c/parquet").mkdir(parents=True)
    c2c = pd.DataFrame(
        [  # forest_type, pix_id, change_year, magnitude
            ("olivinskog", "a", 2019.996, -0.3),  # loss in 2020
            ("olivinskog", "a", 2019.996, -0.3),  # same cell in an overlapping polygon
            ("olivinskog", "b", 2020.996, -0.1),  # below the cut-off
            ("kalklindeskog", "c", 2017.996, -0.175),  # at the cut-off: loss in 2018
            ("kalklindeskog", "d", 2024.996, -0.05),  # weak, but sets the years to 2025
        ],
        columns=dashboard.C2C_COLS,
    )
    gpd.GeoDataFrame(
        c2c, geometry=gpd.points_from_xy([15.0] * 5, [59.6] * 5), crs=4326
    ).to_parquet(root / "c2c/parquet/c2c_pixels.parquet")
    return root


@pytest.fixture
def baked(tmp_path: Path):
    out = tmp_path / "out"
    src = make_source(tmp_path / "src")
    idx = dashboard.bake(str(src), str(src / "condition"), out)
    return idx, out


def _monthly(out: Path) -> dict[tuple[str, str], dict]:
    rows = json.loads((out / "monthly.json").read_text())
    return {(r["forest_type"], r["period"]): r for r in rows}


def test_pixel_with_two_losses_counts_once_and_maps_latest(baked) -> None:
    idx, out = baked
    assert idx["pixels"]["olivinskog"] == {"loss": 2, "gain": 1}
    assert idx["pixels"]["kalklindeskog"] == {"loss": 2, "gain": 0}
    assert idx["date_range"] == ["2024-01-15", "2024-03-10"]
    pts = json.loads((out / "points_olivinskog_loss.json").read_text())
    by_lon = dict(zip(pts["lon"], zip(pts["date"], pts["n"], strict=True), strict=True))
    assert by_lon[15.0001] == (20240310, 2)  # latest break, both events
    assert by_lon[15.0002] == (20240305, 1)
    assert pts["mag"] == [0.2, 0.2]  # |magnitude| of the latest break


def test_monthly_counts_new_area_by_first_break(baked) -> None:
    idx, out = baked
    m = _monthly(out)
    # Olivinskog: pixel 1 first breaks in Jan (its March event is not new area).
    # One area per certainty choice (all, medium and high, high): all score 0.9.
    assert m["olivinskog", "2024-01"]["loss_ha"] == [0.01] * 3
    assert m["olivinskog", "2024-02"]["loss_ha"] == [0.0] * 3
    assert m["olivinskog", "2024-02"]["gain_ha"] == [0.01] * 3
    assert m["olivinskog", "2024-03"]["loss_ha"] == [0.01] * 3
    assert m["kalklindeskog", "2024-02"]["loss_ha"] == [0.02] * 3
    assert idx["conf_tiers"] == list(dashboard.CONF_TIERS)


def test_monthly_splits_area_by_certainty() -> None:
    px = pd.DataFrame(
        {
            "aoi": "olivinskog",
            "kind": "loss",
            "first": pd.to_datetime(["2024-01-05"] * 4),
            "last": pd.to_datetime(["2024-01-05"] * 4),
            # 0.335 rounds to 0.34, as the map sees it; None is unscored.
            "conf": [0.1, 0.335, 0.7, None],
        }
    )
    (row,) = [r for r in dashboard.monthly(px) if r["forest_type"] == "olivinskog"]
    assert row["loss_ha"] == [0.04, 0.02, 0.01]
    assert row["gain_ha"] == [0.0, 0.0, 0.0]


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


def test_forest_area_counts_c2c_loss_once_per_year(baked) -> None:
    fa = baked[0]["forest_area"]
    assert fa["years"] == list(range(2018, 2026))  # from the data
    # Two 100 m squares (the third polygon repeats one); one 200 m square.
    assert fa["olivinskog"]["mapped_ha"] == 2.0
    assert fa["kalklindeskog"]["mapped_ha"] == 4.0
    loss = {a: dict(zip(fa["years"], fa[a]["loss_ha"])) for a in dashboard.AOIS}
    assert loss["olivinskog"][2020] == 0.01 and sum(loss["olivinskog"].values()) == 0.01
    assert (
        loss["kalklindeskog"][2018] == 0.01
        and sum(loss["kalklindeskog"].values()) == 0.01
    )


def test_no_c2c_in_the_source_hides_forest_area(tmp_path: Path) -> None:
    src = make_source(tmp_path / "src")
    (src / "c2c/parquet/c2c_pixels.parquet").unlink()
    idx = dashboard.bake(str(src), str(src / "condition"), tmp_path / "out")
    assert idx["forest_area"] is None
    assert "c2c" not in idx["downloads"]


def test_downloads_match_the_map(baked) -> None:
    idx, out = baked
    files = {
        k: {f: v["file"] for f, v in d.items()} for k, d in idx["downloads"].items()
    }
    assert files == {
        "stands": {"gpkg": "downloads/stands.gpkg.zip"},
        "alerts": {"gpkg": "downloads/alerts.gpkg.zip"},
        "c2c": {"geoparquet": "downloads/c2c.parquet"},
    }
    for d in idx["downloads"].values():
        for v in d.values():
            assert (out / v["file"]).stat().st_size == v["bytes"]
    stands = gpd.read_file(f"zip://{out}/downloads/stands.gpkg.zip!stands.gpkg")
    assert len(stands) == len(
        json.loads((out / "polygons.geojson").read_text())["features"]
    )
    alerts = gpd.read_file(f"zip://{out}/downloads/alerts.gpkg.zip!alerts.gpkg")
    # As the map: pixels once per kind, sub-gate and rejected breaks left out.
    assert sorted(zip(alerts["forest_type"], alerts["kind"])) == sorted(
        [("olivinskog", "loss")] * 2
        + [("olivinskog", "gain")]
        + [("kalklindeskog", "loss")] * 2
    )
    assert len(gpd.read_parquet(out / "downloads/c2c.parquet")) == 5


def test_missing_polygons_for_an_aoi_raises(tmp_path: Path) -> None:
    src = make_source(tmp_path / "src", forest_types=("olivinskog",))
    with pytest.raises(ValueError, match="kalklindeskog"):
        dashboard.bake(str(src), str(src / "condition"), tmp_path / "out")


def test_cli_publish_dashboard(tmp_path: Path) -> None:
    src, out = make_source(tmp_path / "src"), tmp_path / "out"
    result = runner.invoke(
        app,
        [
            "publish",
            "dashboard",
            "--source",
            str(src),
            "--condition",
            str(src / "condition"),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out / "index.json").is_file()


def test_cli_publish_stands_asset(tmp_path: Path) -> None:
    src, out = make_source(tmp_path / "src"), tmp_path / "stands.zip"
    result = runner.invoke(
        app,
        [
            "publish",
            "stands-asset",
            "--condition",
            str(src / "condition"),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    stands = gpd.read_file(f"zip://{out}")
    # Dissolved per forest type, in lon/lat for Earth Engine.
    assert sorted(stands["type"]) == ["kalklindeskog", "olivinskog"]
    assert stands.crs.to_epsg() == 4326


def test_missing_condition_file_is_refused_by_name(tmp_path: Path) -> None:
    src = make_source(tmp_path / "src")
    (src / "condition/b1.tif").unlink()  # a block index.csv lists
    (src / "condition/condition_pixels.pmtiles").unlink()
    with pytest.raises(ValueError, match="condition_pixels.pmtiles, b1.tif"):
        dashboard.bake(str(src), str(src / "condition"), tmp_path / "out")
    assert not (tmp_path / "out/index.json").exists()


def test_condition_url_gives_the_app_its_cells_and_downloads(tmp_path: Path) -> None:
    src = make_source(tmp_path / "src")
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(src / "condition")
    )
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        idx = dashboard.bake(str(src), url, tmp_path / "out")
    finally:
        srv.shutdown()
    assert idx["condition"] == url + "/"
    assert idx["downloads"]["condition"] == {
        "geoparquet": {"url": url + "/condition_pixels.parquet"},
        "cogs": {"url": url + "/index.csv"},
    }


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


def test_polygons_carry_stand_condition(baked):
    idx, out = baked
    feats = json.loads((out / "polygons.geojson").read_text())["features"]
    assert len(feats) == idx["polygon_count"] == sum(idx["polygon_counts"].values())
    p = feats[0]["properties"]
    # The yearly scores travel as one `series` list; 2025 also on its own.
    cols = [c for c in dashboard.STAND_COLS if not c.startswith("dor_global_")]
    for col in cols + ["dor_global_2025", "series", "lon", "lat"]:
        assert col in p
    assert isinstance(p["survey_year"], int)
    assert -180 <= p["lon"] <= 180 and -90 <= p["lat"] <= 90


def test_stand_series_and_no_radd(baked):
    idx, out = baked
    p = json.loads((out / "polygons.geojson").read_text())["features"][0]["properties"]
    assert p["series"] == pytest.approx([0.5 + i / 100 for i in range(9)])
    assert p["threshold_global"] == 0.51
    assert idx["years"] == list(range(2017, 2026))


def test_rebake_of_unchanged_data_changes_no_file(tmp_path: Path) -> None:
    src, out = make_source(tmp_path / "src"), tmp_path / "out"
    dashboard.bake(str(src), str(src / "condition"), out)
    before = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}
    dashboard.bake(str(src), str(src / "condition"), out)
    assert {p: p.read_bytes() for p in out.rglob("*") if p.is_file()} == before


def test_a_new_condition_year_needs_no_code(tmp_path: Path) -> None:
    src = make_source(tmp_path / "src")
    path = src / "condition/condition_stands.parquet"
    pd.read_parquet(path).assign(dor_global_2026=0.6).to_parquet(path)
    idx = dashboard.bake(str(src), str(src / "condition"), tmp_path / "out")
    assert idx["years"] == list(range(2017, 2027))
    feats = json.loads((tmp_path / "out/polygons.geojson").read_text())["features"]
    props = feats[0]["properties"]
    assert props["dor_global_2026"] == 0.6 and "dor_global_2025" not in props
    assert len(props["series"]) == 10
