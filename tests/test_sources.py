"""Sentinel-2 source routing: CDSE history, EOPF from EOPF_START."""

import pystac
import pytest

from effektovervaking_forests import catalog, config, reader

ZARR = (
    "https://data.eodc.eu/collections/EOPF_ZARR/products/cpm_v270/S02MSIL2A/2026/"
    "10/31/S2B_MSIL2A_20261031T105159_N0513_R051_T32VPM_20261031T125524.zarr"
)


def test_gdal_path_per_source():
    assert (
        reader.gdal_path("s3://eodata/a/B02_10m.jp2") == "/vsis3/eodata/a/B02_10m.jp2"
    )
    assert (
        reader.gdal_path(f"{ZARR}/measurements/reflectance/r10m/b02")
        == f'ZARR:"/vsicurl/{ZARR}":/measurements/reflectance/r10m/b02'
    )


@pytest.mark.parametrize(
    ("source", "start", "end", "expected"),
    [
        (
            "eopf",
            "2017-01-01",
            "2026-03-01",
            [
                ("cdse", "2017-01-01", "2026-01-01"),
                ("eopf", "2026-01-01", "2026-03-01"),
            ],
        ),
        ("eopf", "2026-02-01", "2026-03-01", [("eopf", "2026-02-01", "2026-03-01")]),
        ("eopf", "2017-01-01", "2025-06-01", [("cdse", "2017-01-01", "2025-06-01")]),
        ("cdse", "2017-01-01", "2026-03-01", [("cdse", "2017-01-01", "2026-03-01")]),
    ],
)
def test_spans_split_history_and_recent(monkeypatch, source, start, end, expected):
    monkeypatch.setattr(config, "S2_SOURCE", source)
    monkeypatch.setattr(config, "EOPF_START", "2026-01-01")
    name = {config.STAC_URL: "cdse", config.EOPF_STAC_URL: "eopf"}
    got = [(name[u], a, b) for u, a, b in catalog._spans(start, end)]
    assert got == expected


def _item(href: str, **extra) -> pystac.Item:
    it = pystac.Item("x", None, None, pystac.utils.str_to_datetime("2026-10-31"), {})
    it.add_asset("B02_10m", pystac.Asset(href, extra_fields=extra))
    return it


def test_boa_offset_from_eopf_asset_only():
    eopf = _item(f"{ZARR}/b02", **{"raster:offset": -0.1, "raster:scale": 0.0001})
    assert catalog._boa_offset(eopf) == -1000
    cdse = _item("s3://eodata/b02.jp2", **{"raster:offset": -0.1, "raster:scale": 1e-4})
    assert catalog._boa_offset(cdse) is None, "CDSE rows read the XML"


def test_catalogue_offset_wins_over_metadata_xml():
    import pandas as pd

    scene = pd.Series(
        {"id": "s", "boa_offset": -1000, "assets": {}, "baseline": "05.13"}
    )
    assert reader.boa_offset_for(scene, "B02") == -1000
    scene["boa_offset"] = float("nan")  # CDSE row: falls back to the baseline rule
    assert reader.boa_offset_for(scene, "B02") == -1000
