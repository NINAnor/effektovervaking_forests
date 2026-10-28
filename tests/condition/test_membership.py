"""Overlapping polygons: the pixel model that stops them eating each other."""

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from effektovervaking_forests.condition.membership import (
    build_membership,
    dedupe_pixels,
    pixel_roles,
    polygon_overlaps,
)
from effektovervaking_forests.condition.paths import BANDS


def _polys():
    """Two stands that overlap on a 20x20 m square, plus one apart from both."""
    return gpd.GeoDataFrame(
        {
            "poly_id": ["A", "B", "C"],
            "forest_type": ["kalklindeskog"] * 3,
            "ref_role": ["good", "bad", "unlabelled"],
            "geometry": [
                box(0, 0, 40, 40),
                box(20, 20, 60, 60),
                box(200, 200, 240, 240),
            ],
        },
        crs="EPSG:32632",
    )


def _pixels(coords, years=(2024,)):
    rows = []
    for year in years:
        for i, (x, y) in enumerate(coords):
            rec = {"poly_id": "?", "year": year, "utm_zone": "32N", "x": x, "y": y}
            rec.update({b: float(i) for b in BANDS})
            rows.append(rec)
    return pd.DataFrame(rows)


def test_a_pixel_in_two_polygons_gets_two_memberships_and_one_embedding():
    # The shared square holds (25, 25); each stand also has ground of its own.
    coords = [(5, 5), (25, 25), (55, 55)]
    px = _pixels(coords)
    pixels, sites = dedupe_pixels(px)
    assert len(sites) == 3
    assert len(pixels) == 3  # one row per (pixel, year), not per (polygon, pixel, year)

    mem = build_membership(sites, _polys())
    shared = sites.loc[(sites.x == 25) & (sites.y == 25), "pixel_id"].iloc[0]
    assert set(mem.loc[mem.pixel_id == shared, "poly_id"]) == {"A", "B"}
    # and each stand keeps its own ground as well as the shared pixel
    assert len(mem[mem.poly_id == "A"]) == 2
    assert len(mem[mem.poly_id == "B"]) == 2


def test_the_same_ground_read_in_two_years_is_one_pixel_with_two_observations():
    px = _pixels([(5, 5)], years=(2023, 2024))
    pixels, sites = dedupe_pixels(px)
    assert len(sites) == 1
    assert sorted(pixels["year"]) == [2023, 2024]


def test_contradictory_labels_make_a_pixel_a_reference_for_neither_pool():
    px = _pixels([(5, 5), (25, 25), (55, 55)])
    _, sites = dedupe_pixels(px)
    poly = _polys()
    roles = pixel_roles(build_membership(sites, poly), poly).set_index("pixel_id")
    shared = sites.loc[(sites.x == 25) & (sites.y == 25), "pixel_id"].iloc[0]
    only_a = sites.loc[(sites.x == 5) & (sites.y == 5), "pixel_id"].iloc[0]
    # A calls it good, B calls it bad -- so it is evidence for neither
    assert roles.loc[shared, "role"] == "conflict"
    assert roles.loc[only_a, "role"] == "good"


def test_an_unlabelled_layer_does_not_override_a_labelled_one():
    poly = _polys()
    poly.loc[poly.poly_id == "B", "ref_role"] = "unlabelled"
    px = _pixels([(25, 25)])
    _, sites = dedupe_pixels(px)
    roles = pixel_roles(build_membership(sites, poly), poly)
    assert roles["role"].iloc[0] == "good"


def test_overlap_is_asymmetric_and_reports_the_first_polygons_share():
    ov = polygon_overlaps(_polys(), buffer_m=0.0).set_index(["poly_id", "poly_id2"])
    # 20x20 shared out of 40x40 each -> a quarter of either
    assert ov.loc[("A", "B"), "frac"] == pytest.approx(0.25)
    assert ov.loc[("B", "A"), "frac"] == pytest.approx(0.25)
    assert ("A", "C") not in ov.index


def test_the_buffer_catches_stands_that_do_not_quite_touch():
    poly = _polys()
    poly.loc[poly.poly_id == "C", "geometry"] = box(45, 0, 85, 40)  # 5 m from A
    assert ("A", "C") not in set(
        map(tuple, polygon_overlaps(poly, buffer_m=0.0)[["poly_id", "poly_id2"]].values)
    )
    assert ("A", "C") in set(
        map(
            tuple, polygon_overlaps(poly, buffer_m=20.0)[["poly_id", "poly_id2"]].values
        )
    )
