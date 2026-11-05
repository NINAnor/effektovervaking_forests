"""S-CCD: initial fit, online update, and the breaks that come out of it.

S-CCD is recursive: it carries a Kalman-filtered model per pixel forward and
tests each new observation against it, so `update()` calls `sccd_update`
rather than refitting history with `sccd_detect`. The recipe (band order,
clipping to 1-10000, all-clear QA, p_cg/conse) mirrors the inspector so alerts
are reproducible there. Breaks are triggers for review, not final answers; see
docs/ for detector accuracy.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from . import config

try:
    from pyxccd import sccd_detect, sccd_update

    HAVE_PYXCCD = True
except ImportError:  # pragma: no cover
    HAVE_PYXCCD = False

# Below this many clear observations a harmonic fit is meaningless (matches
# the inspector).
MIN_OBS = 20


def _to_ordinal(ts: pd.Series) -> np.ndarray:
    return np.array(
        [d.toordinal() for d in pd.to_datetime(ts, utc=True).dt.date], dtype=np.int64
    )


def frac_year(ordinal: int) -> float:
    d = dt.date.fromordinal(int(ordinal))
    base = dt.date(d.year, 1, 1).toordinal()
    return round(d.year + (ordinal - base) / 365.25, 3)


def pixel_arrays(g: pd.DataFrame) -> tuple:
    """One pixel's observations as the arrays pyxccd wants.

    Same-day duplicates (MGRS tile overlap) are collapsed to their median.
    """
    g = g.sort_values("datetime")
    ordinals = _to_ordinal(g["datetime"])
    df = pd.DataFrame({b: g[b].to_numpy() for b in config.BANDS})
    df["ord"] = ordinals
    if df["ord"].duplicated().any():
        df = df.groupby("ord", as_index=False).median(numeric_only=True)
    df = df.sort_values("ord")

    dts = df["ord"].to_numpy(dtype=np.int64)
    lo, hi = config.PYX_VALID_RANGE
    arr = {
        b: np.clip(df[b].to_numpy(dtype=np.int64), max(lo, 1), hi) for b in config.BANDS
    }
    # Observations are pre-masked by the reader, so all are "clear" to pyxccd.
    qa = np.zeros(len(dts), dtype=np.int64)
    return dts, arr, qa


def fit_pixel(g: pd.DataFrame):
    """Initial S-CCD fit over a pixel's full history."""
    dts, a, qa = pixel_arrays(g)
    if len(dts) < MIN_OBS:
        return None
    return sccd_detect(
        dts,
        a["B02"],
        a["B03"],
        a["B04"],
        a["B08"],
        a["B11"],
        a["B12"],
        qa,
        p_cg=config.SCCD_P_CG,
        conse=config.SCCD_CONSE,
        lam=config.PYX_LAM,
    )


def update_pixel(pack, g: pd.DataFrame):
    """Advance an existing fit with new observations only."""
    dts, a, qa = pixel_arrays(g)
    if len(dts) == 0:
        return pack
    return sccd_update(
        pack,
        dts,
        a["B02"],
        a["B03"],
        a["B04"],
        a["B08"],
        a["B11"],
        a["B12"],
        qa,
        p_cg=config.SCCD_P_CG,
        conse=config.SCCD_CONSE,
        lam=config.PYX_LAM,
    )


def breaks_from_pack(pack, pix_id: str) -> list[dict]:
    """Confirmed breaks in a pixel's state, as alert candidates.

    Only confirmed segments in `rec_cg` are reported (in-progress breaks live
    in `nrt_model`). Magnitude is the NIR/SWIR2 change rescaled to the NBR range.
    """
    rec = getattr(pack, "rec_cg", None)
    if rec is None or len(rec) == 0:
        return []
    out = []
    for i in range(len(rec)):
        seg = rec[i]
        tb = int(seg["t_break"])
        if tb <= 0:
            continue
        mag = np.asarray(seg["magnitude"], dtype=float)
        nbr_delta = (mag[3] - mag[5]) / 10000.0  # NIR idx 3, SWIR2 idx 5
        out.append(
            {
                "pix_id": pix_id,
                "break_ordinal": tb,
                "break_date": dt.date.fromordinal(tb).isoformat(),
                "break_year": frac_year(tb),
                "magnitude": round(float(nbr_delta), 4),
                "kind": "loss" if nbr_delta < 0 else "gain",
            }
        )
    return out


def nrt_status(pack) -> int | None:
    """The pixel's current pyxccd NRT mode (stable vs accumulating anomalies)."""
    v = getattr(pack, "nrt_mode", None)
    if v is None:
        return None
    try:
        return int(np.asarray(v).ravel()[0])
    except (ValueError, IndexError, TypeError):
        return None


def all_breaks(states: dict) -> list[dict]:
    """Every confirmed break currently held in state, across all pixels.

    Lets a failed alert write be recovered, since `update()` only reports the
    delta (see pipeline.reconcile_alerts).
    """
    out: list[dict] = []
    for pix_id, pack in states.items():
        out.extend(breaks_from_pack(pack, pix_id))
    return out


def bootstrap(
    observations: pd.DataFrame, progress=None
) -> tuple[dict, list[dict], dict]:
    """Fit initial state for every pixel with enough clear observations.

    Also used on subsets: pixels that crossed MIN_OBS since bootstrap, and
    pixels refit after a reprocessed product landed behind the watermark.
    """
    if not HAVE_PYXCCD:
        raise RuntimeError("pyxccd is not installed (pip install 'pyxccd>=1.0.3')")

    states, all_breaks = {}, []
    skipped = 0
    groups = list(observations.groupby("pix_id", sort=False))
    for i, (pix_id, g) in enumerate(groups):
        try:
            pack = fit_pixel(g)
        except Exception as e:  # noqa: BLE001
            if progress:
                progress(f"  fit failed {pix_id}: {type(e).__name__}: {e}")
            skipped += 1
            continue
        if pack is None:
            skipped += 1
            continue
        states[pix_id] = pack
        all_breaks.extend(breaks_from_pack(pack, pix_id))
        if progress and (i + 1) % 2000 == 0:
            progress(f"  fitted {i + 1}/{len(groups)} pixels")

    stats = {
        "n_fitted": len(states),
        "n_skipped": skipped,
        "n_breaks": len(all_breaks),
        "min_obs": MIN_OBS,
    }
    return states, all_breaks, stats


def update(
    states: dict, new_obs: pd.DataFrame, progress=None
) -> tuple[dict, list[dict], dict]:
    """Advance state with new observations; return newly confirmed breaks.

    "New" = break ordinals absent before the update. Confirmation needs `conse`
    further looks, so a new break may be dated weeks back; that lag is reported
    as `detection_lag_days`.
    """
    if not HAVE_PYXCCD:
        raise RuntimeError("pyxccd is not installed (pip install 'pyxccd>=1.0.3')")

    fresh, unknown, failed = [], 0, 0
    unfitted: list[str] = []
    now_ord = dt.date.today().toordinal()

    for pix_id, g in new_obs.groupby("pix_id", sort=False):
        pack = states.get(pix_id)
        if pack is None:
            # No state (below MIN_OBS at fit time): return it so the caller can
            # fit it once it qualifies.
            unknown += 1
            unfitted.append(pix_id)
            continue
        before = {b["break_ordinal"] for b in breaks_from_pack(pack, pix_id)}
        try:
            pack = update_pixel(pack, g)
        except Exception as e:  # noqa: BLE001
            if progress:
                progress(f"  update failed {pix_id}: {type(e).__name__}: {e}")
            failed += 1
            continue
        states[pix_id] = pack
        for b in breaks_from_pack(pack, pix_id):
            if b["break_ordinal"] not in before:
                b["detected_ordinal"] = now_ord
                b["detection_lag_days"] = int(now_ord - b["break_ordinal"])
                b["nrt_mode"] = nrt_status(pack)
                fresh.append(b)

    stats = {
        "n_pixels_updated": int(new_obs["pix_id"].nunique()) - unknown - failed,
        "n_pixels_without_state": unknown,
        "n_update_failures": failed,
        "n_new_breaks": len(fresh),
        "pixels_without_state": unfitted,
    }
    return states, fresh, stats


def split_by_watermark(
    new_obs: pd.DataFrame, states: dict, watermark: pd.Timestamp | None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Separate observations S-CCD can absorb from ones that need a refit.

    `sccd_update` is recursive: an observation dated at or before the state
    watermark would corrupt the model. Such observations (e.g. a reprocessed old
    product under a new scene_id) go to a full refit of the pixel instead.
    """
    if watermark is None or new_obs.empty:
        return new_obs, new_obs.iloc[0:0]
    ts = pd.to_datetime(new_obs["datetime"], utc=True)
    late = ts <= pd.Timestamp(watermark)
    # Only pixels with state can be behind it.
    late &= new_obs["pix_id"].isin(states.keys())
    return new_obs[~late], new_obs[late]
