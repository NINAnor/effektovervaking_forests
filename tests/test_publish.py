from pathlib import Path

import geopandas as gpd
from shapely.geometry import Point, box

from effektovervaking_forests import publish


def test_write_vector_builds_pmtiles_and_gpkg_without_gdal_cli(tmp_path: Path) -> None:
    feats = gpd.GeoDataFrame(
        {"alert_id": ["a", "b"], "magnitude": [0.3, 0.5]},
        geometry=[
            box(10.0, 59.0, 10.0001, 59.0001),
            box(10.001, 59.0, 10.0011, 59.0001),
        ],
        crs="EPSG:4326",
    )
    pts = feats.set_geometry([Point(10.00005, 59.00005), Point(10.00105, 59.00005)])
    made = publish.write_vector(
        feats, tmp_path / "web", "x_loss", "loss", gis_dir=tmp_path / "gis", points=pts
    )
    assert not [k for k in made if k.endswith("_error")], made
    assert (tmp_path / "web" / "x_loss.pmtiles").stat().st_size > 0
    assert (tmp_path / "web" / "x_loss_pts.pmtiles").stat().st_size > 0
    assert gpd.read_file(made["gpkg"], layer="loss").shape[0] == 2
