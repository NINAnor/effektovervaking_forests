"""Does CDSE give us the same pixels Earth Engine does?

Everything downstream assumes yes. The inspector, the labels, the tuned gates
and the fitted S-CCD state all come from Earth Engine's
`COPERNICUS/S2_SR_HARMONIZED`; the operational stream comes from CDSE's L2A
SAFE archive. If those two disagree even slightly, bootstrapping state from one
and updating it with the other injects a step change at the handover date --
and a step change is precisely what S-CCD is built to report. It would look
like a disturbance in every pixel, on the same day.

So this module compares them at the only level that settles it: RAW per-band DN
at a specific pixel on a specific acquisition.

Two deliberate choices:

* **No cloud masking on either side.** The GEE recipe uses s2cloudless, which
  CDSE does not carry (see config.CLOUD_MASK_NOTE). Masking would confound a
  reflectance question with an availability question, so parity is measured on
  unmasked reflectance and mask disagreement is reported separately.
* **Join on (sensing time, tile), not on date.** Two granules of the same day
  from different tiles genuinely differ over their overlap; comparing them
  would manufacture a mismatch that is not there.
"""

from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pandas as pd

from . import aoi as aoi_mod
from . import catalog, config, reader

GEE_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"


def _ee():
    import ee

    try:
        ee.Number(1).getInfo()
    except Exception:
        ee.Initialize(project=config.EE_PROJECT)
    return ee


def gee_series(lon: float, lat: float, start: str, end: str) -> pd.DataFrame:
    """Unmasked 6-band DN at one pixel, per scene, straight from Earth Engine.

    Unmasked on purpose: getRegion drops masked samples, so a masked pull would
    silently select the scenes rather than compare them.
    """
    ee = _ee()
    pt = ee.Geometry.Point([lon, lat])
    col = (
        ee.ImageCollection(GEE_COLLECTION)
        .filterDate(start, end)
        .filterBounds(pt)
        .select([config.GEE_BAND_ALIAS[b] for b in config.BANDS] + ["SCL"])
    )
    rows = col.getRegion(pt, config.GRID_RES).getInfo()
    if not rows or len(rows) < 2:
        return pd.DataFrame()

    head, body = rows[0], rows[1:]
    df = pd.DataFrame(body, columns=head)
    # GEE's system:index for S2 is "<sensing>_<processing>_T<tile>".
    parts = df["id"].str.split("_")
    df["sensing"] = pd.to_datetime(parts.str[0], format="%Y%m%dT%H%M%S", utc=True)
    df["tile"] = parts.str[-1].str.lstrip("T")
    ren = {config.GEE_BAND_ALIAS[b]: f"gee_{b}" for b in config.BANDS}
    df = df.rename(columns=ren | {"SCL": "gee_scl"})
    return df[["sensing", "tile"] + [f"gee_{b}" for b in config.BANDS] + ["gee_scl"]]


def _sensing_time(scene_id: str) -> pd.Timestamp:
    """Sensing datetime out of a CDSE product id (field 3, e.g. 20250730T104041)."""
    return pd.to_datetime(scene_id.split("_")[2], format="%Y%m%dT%H%M%S", utc=True)


def sample_pixels(aoi_name: str, n: int, seed: int = 0) -> pd.DataFrame:
    """Pixels to test, spread across polygons rather than clustered in one."""
    grid = aoi_mod.load_grid(aoi_name)
    # One pixel per polygon, then thin to n. GroupBy.sample rather than
    # groupby().apply(): the apply form needs `include_groups`, whose accepted
    # values differ between pandas 2 and 3.
    picked = grid.groupby("poly_idx", group_keys=False).sample(n=1, random_state=seed)
    if len(picked) > n:
        picked = picked.sample(n, random_state=seed)
    return picked.reset_index(drop=True)


def check(
    aoi_name: str,
    n_pixels: int = 12,
    start: str | None = None,
    end: str | None = None,
    max_scenes: int = 40,
    progress=None,
) -> dict:
    """Compare CDSE and GEE over a sample of pixels and scenes.

    Returns a report dict; `write_report` persists it. The verdict is
    intentionally strict: L2A DNs are integers, so a correct pipeline matches
    EXACTLY. Anything else means an offset, a resampling or a grid error, none
    of which should be averaged away as "close enough".
    """
    if end is None:
        end = dt.date.today().isoformat()
    if start is None:
        start = (dt.date.fromisoformat(end) - dt.timedelta(days=120)).isoformat()

    pixels = sample_pixels(aoi_name, n_pixels)
    scenes = catalog.load(aoi_name)
    if scenes.empty:
        raise ValueError(f"no catalogue for {aoi_name}; run `catalog sync` first")
    scenes = scenes[
        (pd.to_datetime(scenes["datetime"], utc=True) >= pd.Timestamp(start, tz="UTC"))
        & (pd.to_datetime(scenes["datetime"], utc=True) <= pd.Timestamp(end, tz="UTC"))
    ]
    # One product per acquisition: the archive holds reprocessed duplicates, and
    # matching both against a single Earth Engine observation manufactures
    # mismatches that are really a join artefact.
    scenes = catalog.dedupe_versions(scenes).sort_values("datetime").tail(max_scenes)
    if scenes.empty:
        raise ValueError(f"no catalogued scenes between {start} and {end}")

    # --- CDSE side: one read per scene, all sample pixels at once -------------
    cdse_rows = []
    for i, (_, sc) in enumerate(scenes.iterrows()):
        px = reader.pixels_for_scene(pixels, sc)
        if px.empty:
            continue
        try:
            df = reader.read_scene(sc, px, scl_first=False)
        except Exception as e:  # noqa: BLE001
            if progress:
                progress(f"  skip {sc['id']}: {type(e).__name__}: {e}")
            continue
        df["tile"] = sc["tile"]
        df["sensing"] = _sensing_time(sc["id"])
        cdse_rows.append(df)
        if progress:
            progress(f"  cdse {i + 1}/{len(scenes)} {sc['id']} -> {len(df)} px")
    if not cdse_rows:
        raise RuntimeError("no CDSE observations were read")
    cdse = pd.concat(cdse_rows, ignore_index=True)
    cdse = cdse.rename(
        columns={b: f"cdse_{b}" for b in config.BANDS} | {"scl": "cdse_scl"}
    )

    # --- GEE side: one getRegion per sample pixel ----------------------------
    gee_rows = []
    for _, px in pixels.iterrows():
        g = gee_series(px["lon"], px["lat"], start, end)
        if g.empty:
            continue
        g["pix_id"] = px["pix_id"]
        gee_rows.append(g)
        if progress:
            progress(f"  gee  {px['pix_id']} -> {len(g)} obs")
    if not gee_rows:
        raise RuntimeError("no GEE observations were read")
    gee = pd.concat(gee_rows, ignore_index=True)

    # --- join and compare ----------------------------------------------------
    m = cdse.merge(gee, on=["pix_id", "sensing", "tile"], how="inner")
    # Drop pixels that fall inside the square tile but outside the swath: their
    # DN is 0 (nodata), which is not an observation and must not be compared
    # against a real value another granule supplied for the same pixel.
    n_nodata = int(m["nodata"].sum())
    m = m[~m["nodata"]].reset_index(drop=True)

    per_band = {}
    for b in config.BANDS:
        a, c = m[f"cdse_{b}"].to_numpy("int32"), m[f"gee_{b}"].to_numpy("float64")
        both = ~np.isnan(c)
        diff = a[both] - c[both]
        per_band[b] = {
            "n": int(both.sum()),
            "exact_match": float((diff == 0).mean()) if both.sum() else None,
            "within_1_dn": float((np.abs(diff) <= 1).mean()) if both.sum() else None,
            "mean_diff": float(diff.mean()) if both.sum() else None,
            "median_diff": float(np.median(diff)) if both.sum() else None,
            "p95_abs_diff": float(np.percentile(np.abs(diff), 95))
            if both.sum()
            else None,
            "max_abs_diff": float(np.abs(diff).max()) if both.sum() else None,
        }

    scl_both = m["gee_scl"].notna()
    scl_agree = (
        float((m.loc[scl_both, "cdse_scl"] == m.loc[scl_both, "gee_scl"]).mean())
        if scl_both.any()
        else None
    )

    exact = [
        v["exact_match"] for v in per_band.values() if v["exact_match"] is not None
    ]
    worst = min(exact) if exact else 0.0
    report = {
        "generated": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "aoi": aoi_name,
        "recipe_version": config.RECIPE_VERSION,
        "window": [start, end],
        "n_sample_pixels": int(len(pixels)),
        "n_scenes_read": int(len(cdse_rows)),
        "n_matched_observations": int(len(m)),
        "n_nodata_excluded": n_nodata,
        "n_cdse_observations": int(len(cdse)),
        "n_gee_observations": int(len(gee)),
        "per_band": per_band,
        "scl_agreement": scl_agree,
        "worst_band_exact_match": worst,
        "verdict": "PASS"
        if worst >= 0.999
        else ("MARGINAL" if worst >= 0.99 else "FAIL"),
    }
    return report, m


def report_path(aoi_name: str, window: tuple | list | None = None):
    """Where a report lives. Window-keyed, because parity is not one fact.

    Reports used to share one path per AOI, so checking a second period
    OVERWROTE the first. That is not a filing inconvenience: a PASS over
    2024-2026 and a FAIL over 2018 are both true and describe different parts of
    the archive, and losing either one loses the reason a decision was made.
    Found the hard way on 2026-08-20, when a 2018 check silently replaced the
    2024 PASS that `sccd bootstrap` gates on.
    """
    if window:
        tag = f"{str(window[0])[:10]}_{str(window[1])[:10]}".replace("-", "")
        return config.PARITY_DIR / f"{aoi_name}_parity_{tag}.json"
    return config.PARITY_DIR / f"{aoi_name}_parity.json"


def obs_path(aoi_name: str, window: tuple | list | None = None):
    p = report_path(aoi_name, window)
    return p.with_name(p.stem + "_obs.parquet")


def write_report(report: dict, matched: pd.DataFrame | None = None) -> None:
    """Persist under the window-keyed name AND as the AOI's current report."""
    win = report.get("window")
    for path in (report_path(report["aoi"], win), report_path(report["aoi"])):
        path.write_text(json.dumps(report, indent=1))
    if matched is not None:
        for path in (obs_path(report["aoi"], win), obs_path(report["aoi"])):
            matched.to_parquet(path, index=False)


def load_report(aoi_name: str, window: tuple | list | None = None) -> dict | None:
    p = report_path(aoi_name, window)
    return json.loads(p.read_text()) if p.exists() else None


def all_reports(aoi_name: str) -> list[dict]:
    """Every window this AOI has been checked over, newest window first."""
    out = []
    for p in sorted(config.PARITY_DIR.glob(f"{aoi_name}_parity_*.json")):
        if p.name.endswith("_obs.parquet"):
            continue
        try:
            out.append(json.loads(p.read_text()))
        except json.JSONDecodeError:
            continue
    return sorted(out, key=lambda r: str(r.get("window", ["", ""])[0]), reverse=True)
