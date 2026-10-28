"""S-CCD: initial fit, online update, and the breaks that come out of it.

S-CCD (Stochastic Continuous Change Detection) is the only member of the CCDC
family in this project that is genuinely recursive: it carries a Kalman-filtered
model of each pixel forward and tests each new observation against it, instead
of refitting the whole history. That is what allows near-real-time operation,
and it is why `update()` here calls `sccd_update` rather than `sccd_detect`.

The numerical recipe is copied deliberately from `scripts/c2c_ts_server.py`
(`_pyxccd_breaks_and_fit`): the same band order, the same clipping to 1-10000,
the same all-clear QA vector, and the same label-tuned p_cg/conse. An
operational alert should be reproducible in the inspector a scientist is
looking at, so the two must not drift.

A caveat worth stating plainly: S-CCD scored worst of the six segmenters against
this project's human labels — cross-validated loss F1 **0.185** for the
configuration actually deployed here (p_cg 0.90, conse 6, with the 0.075
magnitude gate), against 0.79 for VeRDET.

Be careful which number gets quoted. `results/label_tuning_cv.csv` reports three:
`cv_f1_tuned` 0.074 (the grid-searched config, which `tune_on_labels.py`
REJECTED as overfitted), `cv_f1_current` 0.138 (these parameters, ungated) and
`cv_f1_gate_only` 0.185 (these parameters plus the tuned gate — what runs here).
Earlier drafts of the design doc quoted 0.074 against the deployed system, which
understated it 2.5x. The ranking is unchanged either way.

It earns its place here by being deployable in near-real-time at all, not by
being the most accurate detector we have. Its output should be
treated as a trigger for review, and the confirmation logic that turns breaks
into alerts is where the precision will have to come from.
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

# Below this many clear observations a harmonic fit is meaningless. Matches the
# inspector's threshold so the two agree about which pixels are modellable.
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

    Same-day duplicates are collapsed to their median. They arise where MGRS
    tiles overlap: a pixel in the seam is imaged twice in one pass and appears
    twice with slightly different values. Feeding both would let a purely
    geometric artefact drive the change statistic.
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
    # Observations are pre-masked, so every one is "clear" as far as pyxccd is
    # concerned; the SCL screening already happened in the reader.
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

    S-CCD keeps only CONFIRMED, closed segments in `rec_cg`; an in-progress
    break lives in `nrt_model` and is not reported here. The magnitude is the
    NIR/SWIR2 change rescaled to the NBR range, matching every other detector in
    this project so a single gate threshold is comparable across them.
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
    """The pixel's current S-CCD mode.

    pyxccd encodes what the recursive model is doing right now — stable
    monitoring, or accumulating consecutive anomalous observations toward a
    break. It is the closest thing to "something may be happening here" before a
    break is confirmed, so it is carried through to the alert table even though
    v1 does not alert on it.
    """
    v = getattr(pack, "nrt_mode", None)
    if v is None:
        return None
    try:
        return int(np.asarray(v).ravel()[0])
    except (ValueError, IndexError, TypeError):
        return None


def all_breaks(states: dict) -> list[dict]:
    """Every confirmed break currently held in state, across all pixels.

    The alert table is meant to be a view of this. Writing alerts and saving
    state cannot be one transaction, so a run that saves state and then fails to
    write alerts would otherwise lose those breaks forever — `update()` only
    ever reports the DELTA. Recomputing the full set from state is what makes
    that recoverable (see pipeline.reconcile_alerts).
    """
    out: list[dict] = []
    for pix_id, pack in states.items():
        out.extend(breaks_from_pack(pack, pix_id))
    return out


def bootstrap(
    observations: pd.DataFrame, progress=None
) -> tuple[dict, list[dict], dict]:
    """Fit initial state for every pixel with enough clear observations.

    Also used mid-life, on a subset: pixels too sparse to fit at bootstrap that
    have since crossed MIN_OBS, and pixels whose history changed underneath them
    (a reprocessed product landing behind the state watermark). Both are refits
    from stored observations, not reads from CDSE.
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

    "New" is decided by comparing each pixel's break set before and after the
    update, keyed on the break ordinal. S-CCD can confirm a break that occurred
    some observations ago — confirmation needs `conse` subsequent looks — so a
    break appearing in this run may be dated weeks back. That lag is inherent to
    the method and is reported as `detection_lag_days` rather than hidden.
    """
    if not HAVE_PYXCCD:
        raise RuntimeError("pyxccd is not installed (pip install 'pyxccd>=1.0.3')")

    fresh, unknown, failed = [], 0, 0
    unfitted: list[str] = []
    now_ord = dt.date.today().toordinal()

    for pix_id, g in new_obs.groupby("pix_id", sort=False):
        pack = states.get(pix_id)
        if pack is None:
            # No state: the pixel was below MIN_OBS when the fit was made. It is
            # not permanently unknowable — it is a pixel waiting for its 20th
            # clear look — so name it rather than just counting it, and let the
            # caller fit it once the store says it qualifies.
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

    `sccd_update` is recursive: it advances a Kalman-filtered model forward in
    time. Handing it an observation dated BEFORE the newest one already folded
    into state is not merely inefficient, it corrupts the model — the change
    statistic is computed against a fit that has already moved past that date.

    This happens for real, not just in theory: ESA reprocesses granules, and a
    2024 product republished today is a genuinely new scene_id carrying an old
    sensing date. Those observations are routed to a full refit of the affected
    pixels instead, which is the only correct way to fold them in.
    """
    if watermark is None or new_obs.empty:
        return new_obs, new_obs.iloc[0:0]
    ts = pd.to_datetime(new_obs["datetime"], utc=True)
    late = ts <= pd.Timestamp(watermark)
    # Only pixels that actually carry state can be "behind" it.
    late &= new_obs["pix_id"].isin(states.keys())
    return new_obs[~late], new_obs[late]
