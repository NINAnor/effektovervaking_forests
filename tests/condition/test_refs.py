"""Reference pools: same forest type, anchored in time, never the probe itself."""

import inspect

import numpy as np
import pandas as pd
import pytest

from effektovervaking_forests.condition.paths import ANNULUS_INNER_M, BANDS
from effektovervaking_forests.condition.refs import (
    EXCLUDE_KM_DEFAULT,
    PoolBuilder,
    anchor_donors,
    survey_year,
)

YEARS = tuple(range(2017, 2026))


def _pixels(spec, years=YEARS):
    """spec: {pixel_id: value}. Every pixel observed in every year."""
    rows = []
    for year in years:
        for pid, v in spec.items():
            rec = {"pixel_id": pid, "year": year}
            # Encode the year so a test can tell WHICH year's pixel a pool holds.
            rec.update({b: float(v) for b in BANDS})
            rec["A00"] = float(v)
            rec["A01"] = float(year)
            rows.append(rec)
    return pd.DataFrame(rows)


def _builder(**kw):
    """Four stands in a row: three god donors at 0/1/2 km, one probe at the origin."""
    polys = pd.DataFrame(
        {
            "poly_id": ["probe", "d0", "d1", "far"],
            "forest_type": ["kalklindeskog"] * 4,
            "ref_role": ["holdout", "good", "good", "good"],
            "year": ["2020", "2019", "2021", "2018"],
        }
    )
    membership = pd.DataFrame(
        {
            "pixel_id": [1, 2, 3, 4, 5, 6, 7, 8],
            "poly_id": ["probe", "probe", "d0", "d0", "d1", "d1", "far", "far"],
        }
    )
    roles = pd.DataFrame(
        {
            "pixel_id": [1, 2, 3, 4, 5, 6, 7, 8],
            "forest_type": ["kalklindeskog"] * 8,
            "role": [
                "holdout",
                "holdout",
                "good",
                "good",
                "good",
                "good",
                "good",
                "good",
            ],
            "n_polygons": [1] * 8,
        }
    )
    pixels = _pixels({i: i for i in range(1, 9)})
    bad = _pixels({-1: -1, -2: -2, -3: -3})
    bad_mem = pd.DataFrame(
        {
            "pixel_id": [-1, -2, -3, -1, -2, -3],
            "poly_id": ["probe"] * 3 + ["d0"] * 3,
        }
    )
    cent = pd.DataFrame(
        {
            "poly_id": ["probe", "d0", "d1", "far"],
            "cx": [0.0, 1_000.0, 2_000.0, 300_000.0],
            "cy": [0.0, 0.0, 0.0, 0.0],
        }
    )
    donors = anchor_donors(polys, roles, membership, YEARS, min_pixels=1)
    kw.setdefault("n_donors", 8)
    kw.setdefault("exclude_block", False)
    # Donors here are 1 km and 2 km away, inside the 3 km production radius.
    # Tests about anchoring, ordering and self-exclusion opt out of the radius
    # so they isolate their own subject; the spatial tests below set it.
    kw.setdefault("exclude_km", 0.0)
    return PoolBuilder(
        pixels=pixels,
        bad_pixels=bad,
        donors=donors,
        membership=membership,
        pixel_roles=roles,
        bad_membership=bad_mem,
        neighbours=pd.DataFrame(columns=["poly_id", "poly_id2"]),
        centroids=cent,
        **kw,
    ), donors


def test_survey_year_prefers_the_full_date_over_the_year_column():
    df = pd.DataFrame({"year": ["2019", "2021"], "Kartlagtda": ["2017-09-07", None]})
    assert list(survey_year(df)) == [2017, 2021]


def test_a_donor_surveyed_before_the_record_cannot_anchor_its_label():
    _, donors = _builder()
    assert set(donors["poly_id"]) == {"d0", "d1", "far"}
    polys = pd.DataFrame(
        {
            "poly_id": ["old"],
            "forest_type": ["kalklindeskog"],
            "ref_role": ["good"],
            "year": ["2015"],
        }
    )
    mem = pd.DataFrame({"pixel_id": [1], "poly_id": ["old"]})
    roles = pd.DataFrame(
        {
            "pixel_id": [1],
            "forest_type": ["kalklindeskog"],
            "role": ["good"],
            "n_polygons": [1],
        }
    )
    assert anchor_donors(polys, roles, mem, YEARS, min_pixels=1).empty


def test_a_donor_contributes_only_its_survey_year_pixels():
    b, _ = _builder()
    # A01 carries the year, so the pool shows which year each donor came from.
    # Globally that is all three donors, at their own survey years -- 2019, 2021
    # and, for the stand 300 km away, 2018.
    g_all, n_all = b.good_pool("probe", "kalklindeskog", "global")
    assert n_all == 3
    assert sorted(set(g_all[:, 1].tolist())) == [2018.0, 2019.0, 2021.0]
    # Locally only the two donors inside the radius, still at their own years.
    good, n = b.good_pool("probe", "kalklindeskog", "local")
    assert n == 2
    assert sorted(set(good[:, 1].tolist())) == [2019.0, 2021.0]


def test_the_good_pool_does_not_move_with_the_probe_year():
    b, _ = _builder()
    a, _ = b.good_pool("probe", "kalklindeskog", "local")
    # There is no year argument by design: the yardstick is fixed.
    assert np.array_equal(a, b.good_pool("probe", "kalklindeskog", "local")[0])


def test_a_stand_is_never_in_its_own_pool():
    b, _ = _builder()
    for scope in ("local", "global"):
        good, _ = b.good_pool("d0", "kalklindeskog", scope)
        assert 3.0 not in set(good[:, 0].tolist())  # d0 owns pixels 3 and 4
        assert 4.0 not in set(good[:, 0].tolist())
        assert 5.0 in set(good[:, 0].tolist())  # but d1 is still there


def test_a_touching_stand_is_never_in_the_pool_even_though_it_has_its_own_id():
    """The overlap case: excluding by poly_id alone would let d0's ground back in."""
    pixels = _pixels({i: i for i in range(1, 9)})
    b, donors = _builder()
    nb = pd.DataFrame({"poly_id": ["probe"], "poly_id2": ["d0"]})
    b2 = PoolBuilder(
        pixels=pixels,
        bad_pixels=_pixels({-1: -1, -2: -2, -3: -3}),
        donors=donors,
        membership=pd.DataFrame(
            {
                "pixel_id": [1, 2, 3, 4, 5, 6, 7, 8],
                "poly_id": ["probe", "probe", "d0", "d0", "d1", "d1", "far", "far"],
            }
        ),
        pixel_roles=pd.DataFrame(
            {
                "pixel_id": list(range(1, 9)),
                "forest_type": ["kalklindeskog"] * 8,
                "role": ["holdout"] * 2 + ["good"] * 6,
                "n_polygons": [1] * 8,
            }
        ),
        bad_membership=pd.DataFrame({"pixel_id": [-1], "poly_id": ["probe"]}),
        neighbours=nb,
        centroids=pd.DataFrame(
            {
                "poly_id": ["probe", "d0", "d1", "far"],
                "cx": [0.0, 1e3, 2e3, 3e5],
                "cy": [0.0] * 4,
            }
        ),
        exclude_block=False,
    )
    for scope in ("local", "global"):
        good, _ = b2.good_pool("probe", "kalklindeskog", scope)
        assert not {3.0, 4.0} & set(good[:, 0].tolist())


def test_the_local_pool_is_the_nearest_donors_and_respects_the_radius():
    b, _ = _builder(n_donors=1, max_km=50.0)
    good, n = b.good_pool("probe", "kalklindeskog", "local")
    assert n == 1
    assert set(good[:, 0].tolist()) == {3.0, 4.0}  # d0, the closest
    # "far" is 300 km away and is in the global pool but never the local one
    g_all, n_all = b.good_pool("probe", "kalklindeskog", "global")
    assert n_all == 3
    assert 7.0 in set(g_all[:, 0].tolist())


def test_the_radius_pool_takes_everyone_inside_it_not_the_nearest_n():
    """`donor_km` is the E44/E59 rule: radius first, `n_donors` only as a floor."""
    b, _ = _builder(n_donors=1, donor_km=2.5, max_km=50.0)
    good, n = b.good_pool("probe", "kalklindeskog", "local")
    assert n == 2  # d0 at 1 km AND d1 at 2 km
    assert set(good[:, 0].tolist()) == {3.0, 4.0, 5.0, 6.0}


def test_the_count_is_the_floor_where_the_radius_is_empty():
    """A probe in a sparse locality falls back to its nearest donors rather than
    being handed nothing -- and the fallback never reaches past `max_km`."""
    b, _ = _builder(n_donors=2, donor_km=0.5, max_km=50.0)
    good, n = b.good_pool("probe", "kalklindeskog", "local")
    assert n == 2  # nobody within 500 m
    assert set(good[:, 0].tolist()) == {3.0, 4.0, 5.0, 6.0}  # d0 and d1, not far
    b2, _ = _builder(n_donors=8, donor_km=0.5, max_km=2.5)
    assert b2.good_pool("probe", "kalklindeskog", "local")[1] == 2


def test_donor_km_zero_is_exactly_the_nearest_n_rule():
    """The shipped default must not be a re-implementation of itself."""
    for n in (1, 2, 3):
        a = _builder(n_donors=n, max_km=50.0)[0].good_pool(
            "probe", "kalklindeskog", "local"
        )
        b = _builder(n_donors=n, donor_km=0.0, max_km=50.0)[0].good_pool(
            "probe", "kalklindeskog", "local"
        )
        assert a[1] == b[1]
        assert np.array_equal(a[0], b[0])


def test_a_named_forest_type_gets_its_own_pool_rule():
    """v2: olivinskog is drawn by radius, kalklindeskog by count (research E60)."""
    b, _ = _builder(
        n_donors=32, donor_km=0.0, pool_by_type={"olivinskog": (8, 20.0)}, max_km=50.0
    )
    assert b.pool_rule("olivinskog") == (8, 20.0)
    assert b.pool_rule("kalklindeskog") == (32, 0.0)  # falls through to globals


def test_the_per_type_rule_changes_only_the_type_it_names():
    """The v2 change must be provably invisible to kalklindeskog."""
    plain, _ = _builder(n_donors=1, max_km=50.0)
    split, _ = _builder(n_donors=1, max_km=50.0, pool_by_type={"olivinskog": (8, 20.0)})
    a = plain.good_pool("probe", "kalklindeskog", "local")
    b = split.good_pool("probe", "kalklindeskog", "local")
    assert a[1] == b[1] and np.array_equal(a[0], b[0])


def test_a_stand_with_no_donor_in_range_gets_an_empty_local_pool_not_a_distant_one():
    b, _ = _builder(max_km=0.5)
    good, n = b.good_pool("probe", "kalklindeskog", "local")
    assert n == 0 and len(good) == 0


def test_the_bad_pool_is_this_stands_own_annulus_and_moves_with_the_probe_year():
    b, _ = _builder()
    for year in (2018, 2024):
        bad = b.bad_pool("probe", year)
        assert len(bad) == 3
        assert set(bad[:, 1].tolist()) == {float(year)}
    assert len(b.bad_pool("d1", 2024)) == 0  # d1 was given no annulus pixels


def test_pixels_two_layers_disagree_about_enter_no_pool():
    polys = pd.DataFrame(
        {
            "poly_id": ["d0"],
            "forest_type": ["kalklindeskog"],
            "ref_role": ["good"],
            "year": ["2019"],
        }
    )
    mem = pd.DataFrame({"pixel_id": [3, 4], "poly_id": ["d0", "d0"]})
    roles = pd.DataFrame(
        {
            "pixel_id": [3, 4],
            "forest_type": ["kalklindeskog"] * 2,
            "role": ["good", "conflict"],
            "n_polygons": [1, 2],
        }
    )
    d = anchor_donors(polys, roles, mem, YEARS, min_pixels=1)
    assert int(d["n_good_pixels"].iloc[0]) == 1


def test_the_bad_pool_draw_does_not_depend_on_how_many_stands_came_before_it():
    """A shared generator would make a stand's pool depend on iteration order,
    so re-scoring one stand would not reproduce the full run's number."""
    b, _ = _builder(max_bad=2)
    first = b.bad_pool("probe", 2024)
    for _ in range(5):  # advance any shared generator
        b.bad_pool("d0", 2024)
    assert np.array_equal(first, b.bad_pool("probe", 2024))


def test_the_bad_pool_draw_reproduces_across_processes():
    """crc32, not hash(): Python randomises string hashing per process."""
    import subprocess
    import sys

    code = (
        "import zlib, numpy as np;"
        "print(int(np.random.default_rng((42, zlib.crc32(b'probe'), 2024))"
        ".integers(0, 10_000)))"
    )
    runs = {
        subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={"PYTHONHASHSEED": s},
        ).stdout.strip()
        for s in ("0", "1", "random")
    }
    assert len(runs) == 1, f"draw varied with PYTHONHASHSEED: {runs}"


def test_an_unknown_scope_is_an_error_not_a_silent_local_pool():
    b, _ = _builder()
    with pytest.raises(ValueError, match="local.*global"):
        b.good_pool("probe", "kalklindeskog", "regional")


# --- spatial exclusion of the good pool --------------------------------------
# The pools used to exclude only the probe and the stands overlapping it, which
# left a donor a few hundred metres away on the same hillside -- often partly
# the same ground (research E01). The fix shipped first as leave-spatial-block-
# out; research E20 measured that against a plain inner radius at the shipped
# estimator and the radius won in both scopes, so the DEFAULT is now a 3 km
# radius -- the transformed pool's own annulus inner edge -- and `exclude_block`
# is the opt-in. These tests pin both, and that the default is not zero.


def test_the_default_radius_is_the_transformed_annulus_inner_edge():
    # One rule for both pools is the whole point; a second tunable is not.
    assert EXCLUDE_KM_DEFAULT == ANNULUS_INNER_M / 1000.0 == 3.0
    assert (
        inspect.signature(PoolBuilder.__init__).parameters["exclude_km"].default
        == EXCLUDE_KM_DEFAULT
    )


def test_a_donor_inside_the_radius_is_excluded_and_a_distant_one_is_kept():
    # d0 at 1 km and d1 at 2 km are both inside 3 km; `far` at 300 km survives.
    b, _ = _builder(exclude_km=EXCLUDE_KM_DEFAULT)
    _, owners, n = b.good_pool("probe", "kalklindeskog", "global", with_owners=True)
    assert set(owners) == {"far"}, "near donor leaked into the global pool"
    assert n == 1


def test_the_radius_can_empty_the_local_pool_and_that_is_the_right_answer():
    # `far` is the only donor the radius leaves, and it is past max_km, so the
    # local scope has nothing in range. An empty pool -> no local score is the
    # documented behaviour; substituting a 300 km donor would be worse. This is
    # not hypothetical: it is why 17 stands carry `global` and no `local` score.
    b, _ = _builder(exclude_km=EXCLUDE_KM_DEFAULT, max_km=50.0)
    good, owners, n = b.good_pool("probe", "kalklindeskog", "local", with_owners=True)
    assert len(good) == 0 and n == 0 and len(owners) == 0


def test_the_radius_is_a_dial_not_a_switch():
    # 1.5 km keeps d1 (2 km) and drops d0 (1 km) -- the behaviour E20 swept.
    b, _ = _builder(exclude_km=1.5)
    _, owners, _ = b.good_pool("probe", "kalklindeskog", "global", with_owners=True)
    assert set(owners) == {"d1", "far"}


def test_radius_and_block_compose_rather_than_replacing_each_other():
    # d1 is 2 km away (inside the radius) AND in its own block; `far` is
    # outside both. Turning on the block must not turn the radius off.
    blocks = pd.Series({"probe": 1, "d0": 2, "d1": 3, "far": 4})
    b, _ = _builder(blocks=blocks, exclude_block=True, exclude_km=3.0)
    _, owners, _ = b.good_pool("probe", "kalklindeskog", "global", with_owners=True)
    assert set(owners) == {"far"}


def test_removing_every_spatial_rule_warns_rather_than_doing_it_quietly():
    with pytest.warns(UserWarning, match="no spatial exclusion"):
        _builder(exclude_km=0.0, exclude_block=False)


def test_the_probes_own_block_is_excluded_from_both_pools():
    blocks = pd.Series({"probe": 1, "d0": 1, "d1": 2, "far": 3})
    b, _ = _builder(blocks=blocks, exclude_block=True, exclude_km=0.0)
    for scope in ("local", "global"):
        good, owners, n = b.good_pool("probe", "kalklindeskog", scope, with_owners=True)
        assert "d0" not in set(owners), f"{scope}: same-block donor leaked in"
        assert set(owners) <= {"d1", "far"}
        assert n == len(set(owners))


def test_spatial_exclusion_can_be_turned_off_to_reproduce_the_old_numbers():
    blocks = pd.Series({"probe": 1, "d0": 1, "d1": 2, "far": 3})
    b, _ = _builder(blocks=blocks, exclude_block=False, exclude_km=0.0)
    _, owners, _ = b.good_pool("probe", "kalklindeskog", "global", with_owners=True)
    assert "d0" in set(owners)


def test_asking_for_block_exclusion_without_blocks_is_an_error_not_a_silent_pass():
    # Silently scoring with leakage would be worse than refusing to score.
    with pytest.raises(ValueError, match="blocks"):
        _builder(exclude_block=True, blocks=None, exclude_km=0.0)


def test_good_pool_owners_line_up_with_the_pool_rows():
    # The stand-level CI resamples donors, so every reference row has to know
    # which donor it came from or the cluster bootstrap is meaningless.
    blocks = pd.Series({"probe": 1, "d0": 2, "d1": 3, "far": 4})
    b, _ = _builder(blocks=blocks, exclude_block=True, exclude_km=0.0)
    good, owners, n = b.good_pool("probe", "kalklindeskog", "global", with_owners=True)
    assert len(owners) == len(good)
    assert n == len(set(owners))
    # A00 encodes the pixel value, which is unique per donor in this fixture.
    for owner in set(owners):
        vals = good[owners == owner][:, 0]
        assert len(set(vals.tolist())) <= 2


def test_rows_for_matches_dict_semantics():
    from effektovervaking_forests.condition.refs import _index_embeddings, _rows_for

    px = pd.DataFrame(
        {"pixel_id": [5, 3, 5, 9, 3, 5], "year": [2020, 2020, 2021, 2020, 2020, 2020]}
    )
    for b in BANDS:
        px[b] = 0.0
    _, index = _index_embeddings(px)
    ref = {k: i for i, k in enumerate(zip(px["pixel_id"], px["year"]))}
    q = np.array([9, 5, 7, 3, 5])
    want = [ref[(int(p), 2020)] for p in q if (int(p), 2020) in ref]
    assert _rows_for(index, q, 2020).tolist() == want  # duplicates -> last row
    assert _rows_for(index, q, 2021).tolist() == [2, 2]
    assert _rows_for(index, q, 1999).tolist() == []
