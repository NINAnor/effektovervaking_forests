"""Persistence for per-pixel S-CCD state, which must survive between runs.

One pickle per AOI, plus a JSON sidecar recording the grid fingerprint and
recipe version the state was fitted under (checked on load, since a mismatch
would be silently wrong). The pickle holds the FIELDS of each `SccdOutput`
(`{pix_id: {field: array}}`), because pyxccd's namedtuple cannot be pickled
directly. Writes are atomic (temp file + rename).
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
    """Update the sidecar without rewriting the state pickle.

    This MERGES, never replaces: `save()` stamps provenance fields (e.g.
    `recipe_version`) onto a copy of the caller's dict, and `load()` refuses
    state without them.
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
