"""Persistence for per-pixel S-CCD state.

This is what makes the pipeline near-real-time rather than a batch job wearing a
cron hat. `sccd_detect` refits an entire history every time; `sccd_update` takes
the model state from last run and advances it with only the new observations.
That state — an `SccdOutput` namedtuple of numpy structured arrays per pixel —
has to survive between runs, so it lives here.

Stored as one pickle per AOI rather than a file per pixel: 38k tiny files would
be slower to read and a nuisance to back up, and the whole state for
kalklindeskog is comfortably in-memory.

What is pickled is the FIELDS of each `SccdOutput`, not the object. pyxccd's
namedtuple reports its `__module__` as `_frozen_importlib`, so pickling the
object itself raises `Can't pickle <class '_frozen_importlib.SccdOutput'>`. Its
fields are plain numpy (structured) arrays and pickle without complaint, so we
store `{pix_id: {field: array}}` and rebuild the namedtuple on load. Writes are
atomic (temp file + rename) because a run interrupted mid-write must not leave
truncated state — the model cannot be reconstructed from a partial
file, only refitted from scratch.

The sidecar metadata records the grid fingerprint and recipe version the state
was fitted under. Pairing state with a changed grid or a changed masking recipe
is silently wrong rather than loudly broken, so it is checked on load.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pickle
from typing import Any

import pandas as pd

from . import config


def state_path(aoi_name: str):
    return config.STATE_DIR / f"{aoi_name}_sccd_state.pkl"


def meta_path(aoi_name: str):
    return config.STATE_DIR / f"{aoi_name}_sccd_state.json"


def _to_storable(pack) -> dict:
    """SccdOutput -> a plain dict of its fields (see module docstring)."""
    return {f: getattr(pack, f) for f in pack._fields}


def _from_storable(d: dict):
    from pyxccd.utils import SccdOutput

    return SccdOutput(**d)


def save(aoi_name: str, states: dict[str, Any], meta: dict) -> None:
    sp, mp = state_path(aoi_name), meta_path(aoi_name)
    tmp = sp.with_suffix(".pkl.tmp")
    payload = {pid: _to_storable(pack) for pid, pack in states.items()}
    with open(tmp, "wb") as fh:
        pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, sp)

    meta = dict(meta)
    meta.update(
        {
            "aoi": aoi_name,
            "n_pixels": len(states),
            "recipe_version": config.RECIPE_VERSION,
            "sccd_params": {
                "p_cg": config.SCCD_P_CG,
                "conse": config.SCCD_CONSE,
                "lam": config.PYX_LAM,
            },
            "saved_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
    )
    tmp = mp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, indent=1, default=str))
    os.replace(tmp, mp)


def load_meta(aoi_name: str) -> dict:
    mp = meta_path(aoi_name)
    return json.loads(mp.read_text()) if mp.exists() else {}


def save_meta(aoi_name: str, meta: dict) -> None:
    """Update the sidecar without rewriting the (much larger) state pickle.

    Used to clear the `alerts_dirty` flag once alerts have been written: the
    model has already consumed the observations at that point, so re-pickling
    500 MB of unchanged state to record one boolean would be all risk and no
    benefit.

    THIS MERGES; it must never replace. `save()` stamps the provenance fields
    (`recipe_version`, `sccd_params`, `n_pixels`, `saved_at`) onto a *copy* of
    the caller's dict, so the caller's own dict never gains them. A blind
    overwrite here therefore silently deleted `recipe_version` from disk on the
    first `alerts_dirty` clear after a bootstrap -- and `load()` refuses to run
    without it. The result was an operational loop that bootstrapped fine and
    then failed on every subsequent cycle with "fitted under recipe None",
    pointing at a re-bootstrap that was not actually needed.
    """
    mp = meta_path(aoi_name)
    merged = load_meta(aoi_name)
    merged.update(meta)
    tmp = mp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(merged, indent=1, default=str))
    os.replace(tmp, mp)


def load(
    aoi_name: str, grid_fingerprint: str | None = None
) -> tuple[dict[str, Any], dict]:
    sp, mp = state_path(aoi_name), meta_path(aoi_name)
    if not sp.exists():
        raise FileNotFoundError(
            f"no S-CCD state for {aoi_name}; run `sccd bootstrap {aoi_name}` first"
        )
    meta = json.loads(mp.read_text()) if mp.exists() else {}

    if meta.get("recipe_version") != config.RECIPE_VERSION:
        raise RuntimeError(
            f"state for {aoi_name} was fitted under recipe "
            f"{meta.get('recipe_version')!r} but config says "
            f"{config.RECIPE_VERSION!r}. Re-bootstrap, or the model is being "
            "advanced with observations it was not fitted for."
        )
    if grid_fingerprint and meta.get("grid_fingerprint") not in (
        None,
        grid_fingerprint,
    ):
        raise RuntimeError(
            f"state for {aoi_name} was fitted on grid "
            f"{meta.get('grid_fingerprint')} but the current grid is "
            f"{grid_fingerprint}. Re-build the grid or re-bootstrap."
        )

    with open(sp, "rb") as fh:
        payload = pickle.load(fh)
    return {pid: _from_storable(d) for pid, d in payload.items()}, meta


def exists(aoi_name: str) -> bool:
    return state_path(aoi_name).exists()


def last_observation_date(aoi_name: str) -> pd.Timestamp | None:
    """The newest observation already folded into state."""
    mp = meta_path(aoi_name)
    if not mp.exists():
        return None
    v = json.loads(mp.read_text()).get("last_observation")
    return pd.Timestamp(v) if v else None
