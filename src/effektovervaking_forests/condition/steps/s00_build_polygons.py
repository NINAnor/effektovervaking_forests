"""Step 0 -- build the polygon manifest for both forest types.

Reads the six mapped layers out of `skog_kartlegginger.gpkg`, gives every
polygon a stable `poly_id`, attaches its forest type and its `tilstand` field
condition label, and assigns the reference role that steps 3-5 use:

    good        tilstand = god                -> reference pool
    bad         tilstand = daarlig / svaertRedusert -> reference pool
    holdout     tilstand = moderat            -> scored, never a reference
    unlabelled  no tilstand (HB13)            -> scored, never a reference

It also records two things without which the roles above cannot be used
honestly. `survey_year`, because these labels were assigned between 2015 and
2025, so a `god` from 2017 says nothing about 2025 and a stand cut in 2020 still
carries it. And `block`, the stand's spatial block: the six layers map the same
localities repeatedly and `god` stands cluster, so a reference pool that excludes
only the probe and its overlapping neighbours still contains a donor a few
hundred metres away on the same hillside. Excluding by block instead costs about
a third of the apparent stand-grain separation, which is the part that was
leakage (`condition/research/`, experiment E01). See `condition.blocks`.

The layers overlap on about a quarter of their combined area. That is not
resolved here -- the polygons stay as mapped -- but `02b_membership.py` builds
the pixel model that stops overlapping stands from taking each other's pixels.

Writes `outputs/data/polygons.parquet` (attributes) and `polygons_geo.parquet`
(GeoParquet, EPSG:25833) -- the geometry file is what the extraction step reads.
GeoParquet rather than GeoPackage because this project tree lives on a network
share where SQLite cannot take a write transaction (`pyogrio` fails with
"Failed to start transaction"); GeoParquet has no such problem and geopandas
reads it back with `read_parquet`.

Run:  forestmon condition step 00
"""

from __future__ import annotations

import sys

import geopandas as gpd
import pandas as pd

from effektovervaking_forests.condition.blocks import BLOCK_KM_DEFAULT, assign_blocks
from effektovervaking_forests.condition.paths import (
    ID_COLUMN,
    KARTLEGGING_GPKG,
    LAYER_TYPE,
    OUT_DATA,
    POLYGONS,
)
from effektovervaking_forests.condition.refs import assign_roles, survey_year

WORKING_CRS = 25833  # ETRS89 / UTM 33N -- the CRS the source layers already use
POLY_GEO = OUT_DATA / "polygons_geo.parquet"

# Attributes carried through where present. Everything else is dropped: the six
# layers have incompatible schemas and a union of all of them is unusable.
KEEP = [
    "tilstand",
    "naturtype",
    "naturtypeKode",
    "lokalitetskvalitet",
    "områdenavn",
    "omradenavn",
    "Områdemer",
    "year",
    "Kartlagtda",
    "Hovedtype",
]


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--block-km",
        type=float,
        default=BLOCK_KM_DEFAULT,
        help="single-linkage cut distance for the spatial blocks "
        "that reference pools are excluded by; the result is "
        "stable between 3 and 10 km (research E08)",
    )
    args = ap.parse_args(argv)

    if not KARTLEGGING_GPKG.exists():
        raise SystemExit(
            f"input not found: {KARTLEGGING_GPKG} (set FORESTS_KARTLEGGING_GPKG)"
        )

    frames = []
    for layer, ftype in LAYER_TYPE.items():
        gdf = gpd.read_file(KARTLEGGING_GPKG, layer=layer).to_crs(WORKING_CRS)
        idc = ID_COLUMN[layer]
        if idc not in gdf.columns:
            raise SystemExit(
                f"{layer}: expected id column {idc!r}, have {list(gdf.columns)}"
            )

        keep = [c for c in KEEP if c in gdf.columns]
        out = gdf[keep + ["geometry"]].copy()
        out.insert(0, "src_id", gdf[idc].astype(str))
        out.insert(0, "layer", layer)
        out.insert(0, "forest_type", ftype)
        # poly_id must survive re-runs and be unique across layers, since the two
        # HB13/MI layers can share an identifikasjon_lokalId.
        out.insert(0, "poly_id", layer + ":" + out["src_id"])
        frames.append(out)
        print(f"  {layer:<22} {len(out):>4} polygons")

    poly = gpd.GeoDataFrame(
        pd.concat(frames, ignore_index=True), crs=f"EPSG:{WORKING_CRS}"
    )
    if poly["poly_id"].duplicated().any():
        dup = poly.loc[poly["poly_id"].duplicated(keep=False), "poly_id"].unique()[:5]
        raise SystemExit(f"poly_id is not unique, e.g. {list(dup)}")

    # The six layers type their shared column names differently (`year` is text
    # in MI, numeric in HB13), which parquet will not accept. Normalise the
    # carried-through attributes to string; they are descriptive only.
    for c in KEEP:
        if c in poly.columns:
            poly[c] = poly[c].astype("string")

    poly["ref_role"] = assign_roles(poly)
    # When the label was assigned. `tilstand` describes the day the surveyor
    # stood there, not the record, so nothing downstream may use a label
    # without also knowing its year -- see condition/refs.py.
    poly["survey_year"] = survey_year(poly)
    poly["area_ha"] = poly.geometry.area / 10_000.0
    # 10 m pixels; a stand under ~0.05 ha will be represented by its centroid
    # pixel only (see aef._pixels_for_geometry), which is worth knowing about.
    poly["approx_pixels"] = (poly.geometry.area / 100.0).round().astype(int)
    # Spatial blocks, so a probe can be kept out of its own locality's pool.
    poly = poly.merge(
        assign_blocks(poly, km=args.block_km)[["poly_id", "block"]],
        on="poly_id",
        how="left",
    )

    OUT_DATA.mkdir(parents=True, exist_ok=True)
    poly.drop(columns="geometry").to_parquet(POLYGONS, index=False)
    poly.to_parquet(POLY_GEO, index=False)

    print(f"\n{len(poly)} polygons -> {POLYGONS.name}, {POLY_GEO.name}")
    bs = poly.groupby("block").size()
    print(
        f"\n{len(bs)} spatial blocks at {args.block_km:g} km single linkage "
        f"(median {int(bs.median())} stands, largest {int(bs.max())}). A "
        "reference pool excludes the probe's whole block, so a block that "
        "holds most of a forest type would leave its stands unscoreable:"
    )
    print(poly.groupby("forest_type")["block"].nunique().to_string())
    print("\nreference role x forest type:")
    print(pd.crosstab(poly["ref_role"], poly["forest_type"]).to_string())
    lab = poly[poly["ref_role"].isin(["good", "bad", "holdout"])]
    print(
        "\nsurvey year of the labelled polygons -- a label is evidence about "
        "its own year only, and AlphaEarth starts in 2017:"
    )
    print(lab["survey_year"].value_counts().sort_index().to_string())
    early = int((lab["survey_year"] < 2017).sum())
    if early:
        print(
            f"  {early} labelled polygons were surveyed before 2017 and can "
            "never anchor a reference; they are still scored."
        )
    print("\npolygon size (ha):")
    print(
        poly.groupby("forest_type")["area_ha"]
        .describe()[["count", "min", "50%", "max"]]
        .round(2)
        .to_string()
    )
    tiny = int((poly["approx_pixels"] < 5).sum())
    if tiny:
        print(
            f"\nnote: {tiny} polygons cover <5 AlphaEarth pixels; their scores rest on "
            "very few observations and are flagged `n_pixels` downstream."
        )


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("00", sys.argv[1:])
