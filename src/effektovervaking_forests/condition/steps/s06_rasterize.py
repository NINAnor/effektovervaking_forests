"""Step 6 -- write the per-pixel results out as GeoTIFFs, one per spatial block.

Rasters sit exactly on the AlphaEarth grid (10 m, UTM zone of the tile, no
resampling). One file per spatial block keeps the rasters small (the stands are
scattered across Norway); each block lies in one UTM zone. `index.csv` and one
`.vrt` per zone tie the files together.

Bands (float32, NaN outside the mapped stands): dor_global_<year> for every
year; dor_local, m_good_global and m_bad for `--year`; threshold_global (fitted
without the stand's own block, so `dor_global > threshold_global` is the
calibrated call); sen_slope, mk_p, trajectory (1 improving 2 stable 3
fluctuating 4 declining); stand_condition (1 good 2 indistinguishable 3 poor);
edge_m (metres inside the stand; pixels under 20 m are mixed); forest_type
(1 kalklindeskog 2 olivinskog); n_polygons. Read m_good alongside the ratio.

A pixel claimed by several stands takes the largest claiming stand's value, so
all bands come from one reference frame; `n_polygons` marks those pixels and the
spread across claimants is printed.

Reads steps 02b, 03 and 04 outputs (incl. `trajectory_pixels.parquet`).

Run:  forestmon condition step 06
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_origin

from effektovervaking_forests.condition.blocks import INTERIOR_M_DEFAULT
from effektovervaking_forests.condition.paths import (
    AEF_YEARS,
    CONDITION,
    OUT_DATA,
    OUT_TIF,
    PIXEL_CONDITION,
    PIXEL_POLYGONS,
    POLYGONS,
)

RES = 10.0
TRAJ_CODE = {"improving": 1, "stable": 2, "fluctuating": 3, "declining": 4}
COND_CODE = {"good_condition": 1, "indistinguishable": 2, "poor_condition": 3}
FTYPE_CODE = {"kalklindeskog": 1, "olivinskog": 2}
TRAJ_PIX = OUT_DATA / "trajectory_pixels.parquet"


def build_table(year: int) -> pd.DataFrame:
    """One row per pixel: coordinates, block, and every band's value."""
    pol = pd.read_parquet(POLYGONS)
    mem = pd.read_parquet(PIXEL_POLYGONS)
    sites = pd.read_parquet(OUT_DATA / "pixel_sites.parquet")
    pix = pd.read_parquet(PIXEL_CONDITION)
    cond = pd.read_parquet(CONDITION)
    traj = pd.read_parquet(TRAJ_PIX)

    meta = pol.set_index("poly_id")
    # Block and forest type are constant across claiming stands, so `first` is safe.
    per_pixel = (
        mem.assign(
            block=mem["poly_id"].map(meta["block"]),
            forest_type=mem["poly_id"].map(meta["forest_type"]),
        )
        .groupby("pixel_id")
        .agg(
            block=("block", "first"),
            forest_type=("forest_type", "first"),
            n_polygons=("poly_id", "size"),
            edge_m=("edge_m", "max"),
        )
    )

    # One claiming stand per pixel (the largest): one reference frame per pixel.
    primary = (
        mem.assign(area=mem["poly_id"].map(meta["area_ha"]))
        .sort_values(["area", "poly_id"], ascending=[False, True])
        .drop_duplicates("pixel_id")[["pixel_id", "poly_id"]]
    )
    own = pix.merge(primary, on=["pixel_id", "poly_id"], how="inner")

    wide = own.pivot_table(
        index="pixel_id", columns="year", values="dor_global", aggfunc="first"
    ).rename(columns=lambda y: f"dor_global_{y}")
    last = own[own["year"] == year].set_index("pixel_id")
    extra = last[["dor_local", "m_good_global", "m_bad"]].rename(
        columns={
            "dor_local": f"dor_local_{year}",
            "m_good_global": f"m_good_global_{year}",
            "m_bad": f"m_bad_{year}",
        }
    )

    cl = cond[cond["year"] == year][["poly_id", "condition_global", "threshold_global"]]
    big = primary.merge(cl, on="poly_id", how="left").set_index("pixel_id")

    t = (
        sites.set_index("pixel_id")
        .join(per_pixel)
        .join(wide)
        .join(extra)
        .join(traj.set_index("pixel_id")[["sen_slope", "mk_p", "trajectory"]])
        .join(big[["condition_global", "threshold_global"]])
    )
    t["trajectory"] = t["trajectory"].map(TRAJ_CODE)
    t["stand_condition"] = t["condition_global"].map(COND_CODE)
    t["forest_type_code"] = t["forest_type"].map(FTYPE_CODE)

    spread = (
        pix[pix["year"] == year]
        .groupby("pixel_id")["dor_global"]
        .agg(lambda s: s.max() - s.min())
    )
    n_shared = int((per_pixel["n_polygons"] > 1).sum())
    if n_shared:
        big_spread = spread[spread > 0.05]
        print(
            f"  {n_shared:,} pixels are claimed by more than one stand; the "
            f"raster takes the largest claimant. Median disagreement between "
            f"claimants {spread[spread > 0].median():.4f}, "
            f"{len(big_spread):,} pixels above 0.05"
        )
    return t.reset_index()


def bands_for(year: int) -> list[tuple[str, str]]:
    out = [(f"dor_global_{y}", f"condition {y}") for y in AEF_YEARS]
    out += [
        (f"dor_local_{year}", f"condition {year}, local reference scope"),
        (f"m_good_global_{year}", "cosine distance to the good pool"),
        (f"m_bad_{year}", "cosine distance to transformed land"),
        ("threshold_global", "decision threshold, fitted without this block"),
        ("sen_slope", "Theil-Sen slope, z-units per year"),
        ("mk_p", "Mann-Kendall p (reported, not the class rule)"),
        ("trajectory", "1 improving 2 stable 3 fluctuating 4 declining"),
        ("stand_condition", "1 good 2 indistinguishable 3 poor"),
        ("edge_m", f"metres inside the stand (<{INTERIOR_M_DEFAULT:g} m is mixed)"),
        ("forest_type_code", "1 kalklindeskog 2 olivinskog"),
        ("n_polygons", "mapped stands claiming this pixel"),
    ]
    return out


def write_block(t: pd.DataFrame, block, zone: str, bands, out_dir: Path) -> dict:
    x0, x1 = t["x"].min(), t["x"].max()
    y0, y1 = t["y"].min(), t["y"].max()
    w = int(round((x1 - x0) / RES)) + 1
    h = int(round((y1 - y0) / RES)) + 1
    col = np.rint((t["x"].to_numpy() - x0) / RES).astype(int)
    # north-up raster: row 0 is the northernmost line
    row = np.rint((y1 - t["y"].to_numpy()) / RES).astype(int)

    epsg = 32600 + int(str(zone)[:-1])
    # (x, y) is the pixel centre; the corner is half a cell out.
    transform = from_origin(x0 - RES / 2, y1 + RES / 2, RES, RES)
    path = out_dir / f"condition_block{int(block):03d}_{zone}.tif"
    profile = dict(
        driver="GTiff",
        height=h,
        width=w,
        count=len(bands),
        dtype="float32",
        crs=f"EPSG:{epsg}",
        transform=transform,
        nodata=np.nan,
        tiled=True,
        blockxsize=256,
        blockysize=256,
        compress="deflate",
        predictor=3,
        BIGTIFF="IF_SAFER",
    )
    with rasterio.open(path, "w", **profile) as dst:
        for i, (col_name, desc) in enumerate(bands, start=1):
            arr = np.full((h, w), np.nan, dtype=np.float32)
            if col_name in t.columns:
                arr[row, col] = t[col_name].to_numpy(dtype=np.float32)
            dst.write(arr, i)
            dst.set_band_description(i, col_name)
            dst.update_tags(i, DESCRIPTION=desc)
        dst.update_tags(
            BLOCK=str(block),
            UTM_ZONE=str(zone),
            PIXELS=str(len(t)),
            FOREST_TYPES=",".join(sorted(t["forest_type"].dropna().unique())),
        )
    return {
        "file": path.name,
        "block": int(block),
        "utm_zone": zone,
        "epsg": epsg,
        "width": w,
        "height": h,
        "n_pixels": int(len(t)),
        "forest_types": "+".join(sorted(t["forest_type"].dropna().unique())),
        "xmin": float(x0 - RES / 2),
        "ymin": float(y0 - RES / 2),
        "xmax": float(x1 + RES / 2),
        "ymax": float(y1 + RES / 2),
    }


def _build_vrt(path: Path, tifs: list[Path], bands) -> None:
    """Write a mosaic VRT directly (no GDAL bindings needed). Blocks in a zone
    share one 10 m grid and CRS, so this is pure offset placement."""
    metas = []
    for t in tifs:
        with rasterio.open(t) as src:
            metas.append((t, src.transform, src.width, src.height, src.crs))
    res = metas[0][1].a
    xmin = min(m[1].c for m in metas)
    ymax = max(m[1].f for m in metas)
    xmax = max(m[1].c + m[2] * res for m in metas)
    ymin = min(m[1].f - m[3] * res for m in metas)
    w, h = int(round((xmax - xmin) / res)), int(round((ymax - ymin) / res))

    root = ET.Element("VRTDataset", rasterXSize=str(w), rasterYSize=str(h))
    ET.SubElement(root, "SRS").text = metas[0][4].to_wkt()
    ET.SubElement(
        root, "GeoTransform"
    ).text = f"{xmin}, {res}, 0.0, {ymax}, 0.0, {-res}"
    for bi, (name, desc) in enumerate(bands, start=1):
        b = ET.SubElement(root, "VRTRasterBand", dataType="Float32", band=str(bi))
        ET.SubElement(b, "Description").text = name
        ET.SubElement(b, "NoDataValue").text = "nan"
        for t, tr, tw, th, _ in metas:
            src = ET.SubElement(b, "ComplexSource")
            ET.SubElement(src, "SourceFilename", relativeToVRT="1").text = t.name
            ET.SubElement(src, "SourceBand").text = str(bi)
            ET.SubElement(
                src, "SrcRect", xOff="0", yOff="0", xSize=str(tw), ySize=str(th)
            )
            ET.SubElement(
                src,
                "DstRect",
                xOff=str(int(round((tr.c - xmin) / res))),
                yOff=str(int(round((ymax - tr.f) / res))),
                xSize=str(tw),
                ySize=str(th),
            )
            ET.SubElement(src, "NODATA").text = "nan"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=False)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--year",
        type=int,
        default=max(AEF_YEARS),
        help="which year the single-year bands describe; the "
        "condition series carries every year regardless",
    )
    ap.add_argument("--out", type=Path, default=OUT_TIF)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    t = build_table(args.year)
    bands = bands_for(args.year)
    print(
        f"{len(t):,} pixels, {len(bands)} bands, {t['block'].nunique()} spatial blocks"
    )

    rows = []
    for (block, zone), g in t.groupby(["block", "utm_zone"], sort=True):
        rows.append(write_block(g, block, zone, bands, args.out))
    idx = pd.DataFrame(rows).sort_values(["utm_zone", "block"])
    idx.to_csv(args.out / "index.csv", index=False)

    total = sum((args.out / r["file"]).stat().st_size for r in rows)
    print(f"\n{len(rows)} GeoTIFFs -> {args.out}  ({total / 1e6:.0f} MB)")
    print(
        idx.groupby("utm_zone")
        .agg(files=("file", "size"), pixels=("n_pixels", "sum"))
        .to_string()
    )

    # One VRT per UTM zone: blocks only mosaic without resampling within a zone.
    for zone, g in idx.groupby("utm_zone"):
        vrt = args.out / f"condition_{zone}.vrt"
        try:
            _build_vrt(vrt, [args.out / f for f in g["file"]], bands)
            print(f"  {vrt.name}  ({len(g)} blocks)")
        except Exception as exc:  # noqa: BLE001 -- the mosaic is a convenience
            print(
                f"  ! could not build {vrt.name}: {exc}. The GeoTIFFs "
                "themselves are complete; open them individually."
            )

    print("\nband order:")
    for i, (name, desc) in enumerate(bands, start=1):
        print(f"  {i:>2}  {name:<22} {desc}")


if __name__ == "__main__":
    from effektovervaking_forests.condition.steps import run_step

    run_step("06", sys.argv[1:])
