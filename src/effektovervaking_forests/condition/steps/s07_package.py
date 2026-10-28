"""Step 7 -- pack the whole result set into ONE GeoPackage for QGIS.

Step 6 writes 66 GeoTIFFs, one per spatial block, on exactly the AlphaEarth grid.
That is the analysis-grade product and it stays. It is not, however, something
you hand to someone who wants to look at the results: 66 files in five different
UTM zones need five separate mosaics and no single QGIS layer can show them all.

This step writes one file instead:

    condition_results.gpkg
      condition_pixels   168,832 ten-metre cells, the same 20 bands as the
                         GeoTIFFs, one band per attribute column
      condition_stands   737 mapped stands, the stand-level verdict, its
                         interval, and the nine-year condition series

**Why vector rather than one big raster.** The stands are scattered over most of
Norway in five UTM zones. A single raster has to pick one projection, and moving
a 10 m grid between zones means resampling -- the value in the cell would no
longer be the value that was scored. Reprojecting a *polygon* does not resample
anything: the cell becomes a slightly rotated quadrilateral covering the same
ground, carrying the same number. So every cell here is in ETRS89 / UTM 33N
(EPSG:25833, the Norwegian national grid) and every value is untouched.

It is also the more useful form for the questions people actually ask of this
layer -- "show me the declining stands", "show me cells below their own
threshold" are attribute queries, which a 20-band raster cannot answer.

The pixel table is built by importing step 6's own `build_table`, so the columns
here are the shipped bands by construction and cannot drift from them.

Run:  forestmon condition step 07
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import shapely

from effektovervaking_forests.condition.paths import (
    AEF_YEARS,
    CONDITION,
    OUT_DATA,
    OUT_TIF,
)
from effektovervaking_forests.condition.steps import s06_rasterize as step6

OUT_CRS = 25833  # ETRS89 / UTM 33N -- the Norwegian national grid
RES = 10.0

# How many decimals each column keeps. Rounding is not cosmetic: SQLite stores
# every REAL in 8 bytes whatever its value, but the GeoPackage compresses far
# better without seventeen digits of float noise, and no number here is
# meaningful past its fourth decimal anyway.
ROUND = {"dor": 4, "m_": 5, "threshold": 5, "sen_slope": 5, "mk_p": 4, "edge_m": 1}
INT_COLS = ["trajectory", "stand_condition", "forest_type_code", "n_polygons"]

TRAJ_NAME = {1: "improving", 2: "stable", 3: "fluctuating", 4: "declining"}
COND_NAME = {1: "good_condition", 2: "indistinguishable", 3: "poor_condition"}
FTYPE_NAME = {1: "kalklindeskog", 2: "olivinskog"}


def _round(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.columns:
        if df[col].dtype.kind != "f":
            continue
        for prefix, nd in ROUND.items():
            if col.startswith(prefix):
                df[col] = df[col].round(nd)
                break
    return df


def pixel_layer(year: int) -> gpd.GeoDataFrame:
    """Every scored 10 m cell as a square, in EPSG:25833."""
    t = step6.build_table(year)
    bands = [name for name, _ in step6.bands_for(year)]

    keep = ["pixel_id", "block", "x", "y", "utm_zone", *bands]
    t = t[[c for c in keep if c in t.columns]].copy()

    parts = []
    for zone, g in t.groupby("utm_zone", sort=True):
        x = g["x"].to_numpy()
        y = g["y"].to_numpy()
        # (x, y) is the cell CENTRE, so the square runs half a cell either way
        cells = shapely.box(x - RES / 2, y - RES / 2, x + RES / 2, y + RES / 2)
        epsg = 32600 + int(str(zone)[:-1])
        parts.append(
            gpd.GeoDataFrame(
                g.drop(columns=["x", "y"]), geometry=cells, crs=f"EPSG:{epsg}"
            ).to_crs(epsg=OUT_CRS)
        )
    out = pd.concat(parts, ignore_index=True)

    # Readable names beside the codes: QGIS shows the attribute table to people,
    # not to a lookup table in a README.
    out["trajectory_class"] = out["trajectory"].map(TRAJ_NAME)
    out["stand_condition_class"] = out["stand_condition"].map(COND_NAME)
    out["forest_type"] = out["forest_type_code"].map(FTYPE_NAME)
    out["below_threshold"] = (
        out[f"dor_global_{year}"] < out["threshold_global"]
    ).astype("Int8")
    out = _round(out)
    for c in [*INT_COLS, "block"]:
        out[c] = out[c].astype("Int16")
    return gpd.GeoDataFrame(out, geometry="geometry", crs=f"EPSG:{OUT_CRS}")


def stand_layer(year: int) -> gpd.GeoDataFrame:
    """The 737 mapped stands with the stand-level verdict and its series."""
    geo = gpd.read_parquet(OUT_DATA / "polygons_geo.parquet").to_crs(epsg=OUT_CRS)
    cond = pd.read_parquet(CONDITION)
    traj = pd.read_parquet(OUT_DATA / "trajectory.parquet")

    now = cond[cond["year"] == year][
        [
            "poly_id",
            "block",
            "n_pixels",
            "dor_global",
            "ci_lo_global",
            "ci_hi_global",
            "threshold_global",
            "condition_global",
            "dor_global_median",
            "dor_global_frac_poor",
            "dor_local",
            "ci_lo_local",
            "ci_hi_local",
            "threshold_local",
            "condition_local",
            "dor_local_median",
            "dor_local_frac_poor",
            "n_donors_global",
            "n_donors_local",
        ]
    ]

    series = (
        cond.pivot_table(
            index="poly_id", columns="year", values="dor_global", aggfunc="first"
        )
        .rename(columns=lambda y: f"dor_global_{y}")
        .reset_index()
    )

    tr = traj[["poly_id", "sen_slope", "mk_p", "mk_z", "sd", "trajectory"]]

    keep = [
        "poly_id",
        "forest_type",
        "layer",
        "tilstand",
        "naturtype",
        "ref_role",
        "survey_year",
        "area_ha",
        "geometry",
    ]
    out = (
        geo[[c for c in keep if c in geo.columns]]
        .merge(now, on="poly_id", how="left")
        .merge(series, on="poly_id", how="left")
        .merge(tr, on="poly_id", how="left")
    )
    out = _round(out)
    for c in ("ci_lo_global", "ci_hi_global", "ci_lo_local", "ci_hi_local"):
        out[c] = out[c].round(4)
    out["ci_width_global"] = (out["ci_hi_global"] - out["ci_lo_global"]).round(4)
    # the priority quadrant, precomputed so it is one filter in QGIS
    out["priority"] = (
        (out["condition_global"] == "good_condition")
        & (out["trajectory"] == "declining")
    ).astype("Int8")
    out["block"] = out["block"].astype("Int16")
    return gpd.GeoDataFrame(out, geometry="geometry", crs=f"EPSG:{OUT_CRS}")


def readme_table(year: int) -> pd.DataFrame:
    """A plain-language column dictionary, carried inside the file itself.

    A GeoPackage travels; a README does not. QGIS lists non-spatial tables in
    the browser, so this opens in the same window as the data.
    """
    rows = [
        (
            "_about",
            "condition_results.gpkg",
            "Forest condition of mapped olivinskog and kalklindeskog stands in "
            f"Norway, from AlphaEarth annual embeddings 2017-{year}.",
        ),
        ("_crs", "EPSG:25833", "ETRS89 / UTM zone 33N."),
        (
            "_cells",
            "10 m",
            "Cells are the AlphaEarth grid, reprojected as "
            "polygons; no value was resampled.",
        ),
        ("condition_pixels", "layer", "One row per scored 10 m cell."),
        ("condition_stands", "layer", "One row per mapped stand."),
        (
            "dor_global_YYYY",
            "0-1",
            "Condition that year. Higher is closer to "
            "the good-condition reference stands.",
        ),
        (
            "dor_local_" + str(year),
            "0-1",
            "The same score against the nearest good stands only. Missing where "
            "a cell has too few good stands of its own forest type within reach.",
        ),
        (
            "threshold_global",
            "0-1",
            "This stand's own decision threshold, fitted without its 5 km block. "
            "Compare each cell to THIS, not to a single AOI-wide number.",
        ),
        ("below_threshold", "0/1", "dor_global < threshold_global."),
        (
            "m_good_global_" + str(year),
            "distance",
            "Cosine distance to the good reference stands. Read with m_bad.",
        ),
        (
            "m_bad_" + str(year),
            "distance",
            "Cosine distance to transformed land. Read with m_good.",
        ),
        (
            "sen_slope",
            "per year",
            "Theil-Sen slope of the condition series. Negative is declining.",
        ),
        ("mk_p", "0-1", "Mann-Kendall p. Reported only; it decides nothing."),
        ("trajectory", "1-4", "1 improving, 2 stable, 3 fluctuating, 4 declining."),
        (
            "stand_condition",
            "1-3",
            "1 good, 2 indistinguishable, 3 poor, from the stand's interval.",
        ),
        (
            "edge_m",
            "metres",
            "Distance inside the stand boundary. Below 20 m the cell is a mixture "
            "of the stand and its surroundings, and its score is partly the "
            "neighbours'.",
        ),
        (
            "n_polygons",
            "count",
            "Mapped stands claiming this cell. Where more than one does, every "
            "value here comes from the largest of them.",
        ),
        ("priority", "0/1", "Stands in good condition today whose trend is declining."),
        (
            "_caution",
            "read the report",
            "Within one locality and controlling for stand size the score is "
            "close to chance. It ranks a population of stands usefully; it often "
            "cannot resolve one stand on its own.",
        ),
    ]
    return pd.DataFrame(rows, columns=["item", "units", "meaning"])


# --------------------------------------------------------------------------
# QGIS styles, stored in the GeoPackage so the file opens already rendered.
# QGIS reads a `layer_styles` table if one is present; any QGIS that does not
# simply ignores it and the data still loads.
# --------------------------------------------------------------------------
def _fill(rgb: str) -> str:
    return (
        f'<layer class="SimpleFill" enabled="1" locked="0" pass="0">'
        f'<Option type="Map">'
        f'<Option name="color" type="QString" value="{rgb}"/>'
        f'<Option name="outline_style" type="QString" value="no"/>'
        f'<Option name="style" type="QString" value="solid"/>'
        f"</Option></layer>"
    )


def _symbols(colours: list[str]) -> str:
    return "".join(
        f'<symbol name="{i}" type="fill" alpha="1" clip_to_extent="1" '
        f'force_rhr="0">{_fill(c)}</symbol>'
        for i, c in enumerate(colours)
    )


def _qml(renderer: str) -> str:
    return (
        "<!DOCTYPE qgis PUBLIC 'http://mapserver.org/dtd/qgis.dtd' 'SYSTEM'>"
        '<qgis version="3.34" styleCategories="Symbology">'
        f"{renderer}</qgis>"
    )


def pixel_qml(year: int) -> str:
    """Graduated on the condition score, cut at the calibrated threshold."""
    edges = [0.0, 0.45, 0.50, 0.5204, 0.55, 0.60, 1.0]
    colours = [
        "166,54,3,255",
        "217,95,14,255",
        "254,153,41,255",
        "161,218,180,255",
        "65,182,196,255",
        "34,94,168,255",
    ]
    ranges = "".join(
        f'<range lower="{edges[i]}" upper="{edges[i + 1]}" symbol="{i}" '
        f'label="{edges[i]:.4g} - {edges[i + 1]:.4g}" render="true"/>'
        for i in range(len(colours))
    )
    r = (
        f'<renderer-v2 type="graduatedSymbol" attr="dor_global_{year}" '
        f'graduatedMethod="GraduatedColor" symbollevels="0" forceraster="0" '
        f'enableorderby="0"><ranges>{ranges}</ranges>'
        f"<symbols>{_symbols(colours)}</symbols></renderer-v2>"
    )
    return _qml(r)


def stand_qml() -> str:
    """Categorised on the stand verdict."""
    cats = [
        ("good_condition", "good condition", "26,152,80,255"),
        ("indistinguishable", "indistinguishable", "200,200,200,255"),
        ("poor_condition", "poor condition", "215,48,39,255"),
    ]
    cat_xml = "".join(
        f'<category render="true" value="{v}" symbol="{i}" label="{lab}"/>'
        for i, (v, lab, _) in enumerate(cats)
    )
    r = (
        f'<renderer-v2 type="categorizedSymbol" attr="condition_global" '
        f'symbollevels="0" forceraster="0" enableorderby="0">'
        f"<categories>{cat_xml}</categories>"
        f"<symbols>{_symbols([c for _, _, c in cats])}</symbols></renderer-v2>"
    )
    return _qml(r)


def write_styles(gpkg: Path, styles: list[tuple[str, str, str]]) -> None:
    con = sqlite3.connect(gpkg)
    con.execute("""CREATE TABLE IF NOT EXISTS layer_styles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        f_table_catalog TEXT, f_table_schema TEXT, f_table_name TEXT,
        f_geometry_column TEXT, styleName TEXT, styleQML TEXT, styleSLD TEXT,
        useAsDefault BOOLEAN, description TEXT, owner TEXT, ui TEXT,
        update_time DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    con.execute("DELETE FROM layer_styles")
    for layer, name, qml in styles:
        con.execute(
            "INSERT INTO layer_styles(f_table_catalog,f_table_schema,f_table_name,"
            "f_geometry_column,styleName,styleQML,styleSLD,useAsDefault,description)"
            " VALUES('','',?,?,?,?,'',1,?)",
            (layer, "geom", name, qml, "default style shipped with the package"),
        )
    con.commit()
    con.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, default=max(AEF_YEARS))
    ap.add_argument(
        "--out", type=Path, default=OUT_TIF.parent / "condition_results.gpkg"
    )
    args = ap.parse_args(argv)

    if args.out.exists():
        args.out.unlink()

    # A GeoPackage is a SQLite database, and SQLite cannot take the locks it
    # needs on the network share this project lives on ("failed to start
    # transaction"). Build it on local disk, then copy the finished file over.
    tmp_dir = Path(tempfile.mkdtemp(prefix="condition_gpkg_"))
    tmp = tmp_dir / args.out.name

    pix = pixel_layer(args.year)
    print(f"condition_pixels  {len(pix):,} cells, {len(pix.columns) - 1} columns")
    pix.to_file(tmp, layer="condition_pixels", driver="GPKG")

    st = stand_layer(args.year)
    print(f"condition_stands  {len(st):,} stands, {len(st.columns) - 1} columns")
    st.to_file(tmp, layer="condition_stands", driver="GPKG")

    rt = readme_table(args.year)
    con = sqlite3.connect(tmp)
    rt.to_sql("readme", con, index=False, if_exists="replace")
    con.execute(
        "INSERT INTO gpkg_contents(table_name, data_type, identifier, "
        "description) VALUES('readme','attributes','readme',"
        "'what every column means')"
    )
    con.commit()
    con.close()

    write_styles(
        tmp,
        [
            ("condition_pixels", f"condition {args.year}", pixel_qml(args.year)),
            ("condition_stands", "stand verdict", stand_qml()),
        ],
    )

    con = sqlite3.connect(tmp)
    con.execute("VACUUM")
    con.close()
    shutil.copyfile(tmp, args.out)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    print(f"\n{args.out}  ({args.out.stat().st_size / 1e6:.1f} MB)")
    print(f"  priority stands (good but declining): {int(st['priority'].sum())}")


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("07", sys.argv[1:])
