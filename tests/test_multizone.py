"""Multi-UTM-zone AOIs (olivinskog spans zones 32-35).

The single-zone assumption was baked into the grid, the tile assignment, the
reader's pixel offer and the published geometry. Each test pins one of those
against a two-zone synthetic AOI. Nothing here touches the network.
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
from effektovervaking_forests import config, reader, store
from effektovervaking_forests import publish as publish_mod

AOI = "zoneaoi"


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
    # Two ~100 m stands: one near Bergen (5.3 E — zone 31 by the naive formula,
    # 32 under the MGRS Norway exception) and one in Nordland (15 E, zone 33).
    polys = gpd.GeoDataFrame(
        {"union_id": [10, 20]},
        geometry=[
            box(5.300, 60.400, 5.302, 60.401),
            box(15.000, 68.000, 15.003, 68.001),
        ],
        crs=4326,
    )
    p = tmp_path / "zoneaoi.parquet"
    polys.to_parquet(p)
    monkeypatch.setitem(config.AOI_PARQUET, AOI, p)
    yield tmp_path


def test_mgrs_zone_follows_the_norway_exception():
    assert aoi_mod.mgrs_utm_zone(5.3, 60.4) == 32  # 32V widened to 3-12 E
    assert aoi_mod.mgrs_utm_zone(5.3, 65.0) == 31  # north of 64 N: standard
    assert aoi_mod.mgrs_utm_zone(10.0, 59.0) == 32
    assert aoi_mod.mgrs_utm_zone(13.0, 60.0) == 33
    assert aoi_mod.mgrs_utm_zone(25.1, 69.9) == 35


def test_grid_puts_each_polygon_on_its_own_zone_lattice():
    g = aoi_mod.build_grid(AOI)
    by = g.groupby("poly_idx")["utm_crs"].unique()
    assert list(by[0]) == ["EPSG:32632"]
    assert list(by[1]) == ["EPSG:32633"]
    # pix_id carries the EPSG code, and the lattice is the S2 10 m one.
    assert (g["pix_id"].str[:5] == g["utm_crs"].str[5:]).all()
    assert ((g["utm_x"] - 5) % 10 == 0).all() and ((g["utm_y"] - 5) % 10 == 0).all()
    # Pixel centres land back inside their own polygon.
    assert g.loc[g.poly_idx == 0, "lon"].between(5.300, 5.302).all()
    assert g.loc[g.poly_idx == 1, "lat"].between(68.000, 68.001).all()


def test_home_tiles_never_cross_zones():
    aoi_mod.save_grid(AOI, aoi_mod.build_grid(AOI))
    huge = (-1e7, -1e8, 1e7, 1e8)  # each tile "covers" every coordinate
    df = aoi_mod.assign_home_tiles(
        AOI, {"32VKN": (huge, "EPSG:32632"), "33WVQ": (huge, "EPSG:32633")}
    )
    assert (df.loc[df.utm_crs == "EPSG:32632", "home_tile"] == "32VKN").all()
    assert (df.loc[df.utm_crs == "EPSG:32633", "home_tile"] == "33WVQ").all()


def test_reader_is_offered_only_its_own_zone():
    g = aoi_mod.build_grid(AOI)  # no home_tile column: the zone filter alone
    sc = pd.Series({"id": "s", "tile": "33WVQ"})
    px = reader.pixels_for_scene(g, sc)
    assert len(px) and (px["utm_crs"] == "EPSG:32633").all()
    assert reader.tile_utm_crs("32VNM") == "EPSG:32632"
    with pytest.raises(ValueError, match="CRS"):
        reader.read_scene(pd.Series({"id": "s", "assets": {config.SCL_ASSET: "x"}}), g)


def test_published_geometry_is_reprojected_per_zone():
    g = aoi_mod.build_grid(AOI)
    one = g.groupby("poly_idx").head(1).reset_index(drop=True)
    gdf = publish_mod._pixel_points(one[["pix_id"]], g)
    assert list(gdf["pix_id"]) == list(one["pix_id"])  # order kept
    assert np.allclose(gdf.geometry.x, one["lon"], atol=1e-7)
    assert np.allclose(gdf.geometry.y, one["lat"], atol=1e-7)


def test_cog_is_one_zone_and_keeps_dates_above_255(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    g = aoi_mod.build_grid(AOI)
    with pytest.raises(ValueError, match="one CRS"):
        publish_mod.write_cog(pd.DataFrame(), g, tmp_path / "x.tif")

    z = g[g.utm_crs == "EPSG:32633"].reset_index(drop=True)
    hit = z.iloc[3]
    alerts_mod.record(
        AOI,
        [
            {
                "pix_id": hit["pix_id"],
                "break_ordinal": dt.date(2025, 10, 16).toordinal(),
                "break_date": "2025-10-16",
                "break_year": 2025.8,
                "magnitude": -0.3,
                "kind": "loss",
            }
        ],
        g,
    )
    px = publish_mod.pixel_table(AOI, "loss")
    out = tmp_path / "alerts.tif"
    publish_mod.write_cog(px, z, out)
    with rasterio.open(out) as src:
        v = list(src.sample([(hit["utm_x"], hit["utm_y"])]))[0]
        # 3210 days: the old writer wrapped this to 138 (mod 256) at real extents.
        assert int(v[0]) == (dt.date(2025, 10, 16) - publish_mod.EPOCH).days == 3210
        assert int(v[3]) == 1
        assert src.descriptions[0] == "last_loss_date_days_since_2017"


def test_polygon_takes_the_zone_of_the_tile_that_actually_covers_it():
    """North of 64 N at 12.6-12.9 E there is no zone-33 tile; the stands there
    are imaged only by the overhang of zone-32 tiles (32WPS/32WPT)."""
    polys = gpd.GeoDataFrame(geometry=[box(12.60, 65.40, 12.62, 65.41)], crs=4326)
    assert aoi_mod.polygon_crs(polys).iloc[0] == "EPSG:32633"  # MGRS alone
    x, y = polys.to_crs(32632).geometry.iloc[0].centroid.coords[0]
    only32 = {"32WPT": ((x - 5e4, y - 5e4, x + 5e4, y + 5e4), "EPSG:32632")}
    assert aoi_mod.polygon_crs(polys, only32).iloc[0] == "EPSG:32632"
    x3, y3 = polys.to_crs(32633).geometry.iloc[0].centroid.coords[0]
    both = only32 | {"33WUP": ((x3 - 5e4, y3 - 5e4, x3 + 5e4, y3 + 5e4), "EPSG:32633")}
    assert (
        aoi_mod.polygon_crs(polys, both).iloc[0] == "EPSG:32633"
    )  # MGRS wins if covered


def test_dark_pixel_is_clamped_to_zero_but_is_not_nodata(monkeypatch):
    """Harmonized L2A is max(DN - 1000, 0): a dark pixel reads 0, like Earth
    Engine, and is still a real observation (nodata means raw DN 0)."""
    import contextlib

    monkeypatch.setattr(reader, "_gdal_env", lambda **k: contextlib.nullcontext())
    monkeypatch.setattr(
        reader, "boa_offsets_for_scene", lambda sc: {b: -1000 for b in config.BANDS}
    )
    raw = {"B02": [907, 1500, 0], "scl": [2, 4, 0]}

    def fake_read(path, xs, ys, crs):
        key = "scl" if "SCL" in path else ("B02" if "B02" in path else None)
        v = np.array(raw[key] if key else [1500, 1500, 0], dtype="int32")
        return v, np.ones(3, dtype=bool)

    monkeypatch.setattr(reader, "_read_at", fake_read)
    assets = {
        a: f"s3://t/{a}.jp2"
        for a in [*config.ASSET_FOR_BAND.values(), config.SCL_ASSET]
    }
    scene = pd.Series(
        {"id": "s", "datetime": pd.Timestamp("2026-10-14", tz="UTC"), "assets": assets}
    )
    px = pd.DataFrame(
        {
            "pix_id": ["a", "b", "c"],
            "utm_x": [0.0, 1, 2],
            "utm_y": [0.0, 0, 0],
            "utm_crs": ["EPSG:32632"] * 3,
        }
    )
    out = reader.read_scene(scene, px, scl_first=False).set_index("pix_id")
    assert out.loc["a", "B02"] == 0 and not out.loc["a", "nodata"]  # clamped, kept
    assert out.loc["b", "B02"] == 500 and not out.loc["b", "nodata"]
    assert out.loc["c", "B02"] == 0 and out.loc["c", "nodata"]  # true nodata
