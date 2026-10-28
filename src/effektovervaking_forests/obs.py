"""The observation store: per-pixel reflectance, accumulated over time.

S-CCD is fed observations, not images, so this is the boundary between "talking
to CDSE" and "detecting change". Keeping it as an explicit, persisted layer
rather than streaming scenes straight into the model buys three things:

* **Re-fittability.** Changing an S-CCD parameter means re-fitting from stored
  observations, not re-downloading nine years of Sentinel-2.
* **Idempotence.** Rows are keyed by (pix_id, acquisition), so a scene that CDSE
  republishes after reprocessing REPLACES its earlier rows instead of entering
  the same acquisition twice under a new product id.
* **Auditability.** When an alert is questioned, the exact observations behind
  it are on disk, with their SCL values and the scene they came from.

**Write path: immutable parts, compacted later.** Appending used to mean reading
the year's parquet, concatenating, and rewriting it. Over a 15k-scene backfill
that is O(n^2) bytes — the 2026 partition would be rewritten hundreds of times,
growing to millions of rows — and every rewrite is a window in which the
partition can be lost. Instead each batch writes one new part file per year and
never touches an existing one; `compact()` folds parts into the yearly file as a
separate, restartable step. Crashing between the two leaves duplicate rows on
disk, which the read path already removes.

Partitioned by year because that suits both paths: the incremental run only ever
writes a part in the current year, and a backfill can compact years in parallel.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pandas as pd

from . import config, store

SCHEMA_COLS = [
    "pix_id",
    "scene_id",
    "datetime",
    *config.BANDS,
    "scl",
    "nodata",
    "clear",
    "proc",
]

# Dedupe key. One pixel is imaged once per acquisition (a pixel has exactly one
# home tile, see aoi.assign_home_tiles), so (pix_id, datetime) identifies an
# observation independently of which PRODUCT delivered it — which is the point:
# reprocessed products have new ids but the same sensing time.
KEY = ["pix_id", "datetime"]


def obs_dir(aoi_name: str) -> Path:
    d = config.OBS_DIR / aoi_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def parts_dir(aoi_name: str) -> Path:
    d = obs_dir(aoi_name) / "parts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def obs_path(aoi_name: str, year: int) -> Path:
    """The compacted file for a year."""
    return obs_dir(aoi_name) / f"{aoi_name}_obs_{year}.parquet"


def part_path(aoi_name: str, year: int, part_id: str) -> Path:
    return parts_dir(aoi_name) / f"{aoi_name}_obs_{year}__{part_id}.parquet"


def parts_for(aoi_name: str, year: int | None = None) -> list[Path]:
    pat = f"{aoi_name}_obs_{year if year is not None else '*'}__*.parquet"
    return sorted(parts_dir(aoi_name).glob(pat))


def years_present(aoi_name: str) -> list[int]:
    d = config.OBS_DIR / aoi_name
    if not d.exists():
        return []
    out: set[int] = set()
    for p in list(d.glob(f"{aoi_name}_obs_*.parquet")) + parts_for(aoi_name):
        stem = p.stem.split("__")[0]
        try:
            out.add(int(stem.split("_")[-1]))
        except ValueError:
            continue
    return sorted(out)


def _proc_time(scene_ids: pd.Series) -> pd.Series:
    """The processing datetime encoded in a CDSE product id (its last field).

    Used to decide which version of a reprocessed acquisition wins. Products
    whose id does not carry one sort first, so a real version always beats an
    unparseable one.
    """
    return scene_ids.astype(str).str.rsplit("_", n=1).str[-1].fillna("")


def dedupe(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (pixel, acquisition): the most recently processed product.

    ESA reprocesses granules and CDSE serves every version. `catalog.dedupe_versions`
    keeps only the newest in the *pending* list, but a version processed after we
    already ingested the old one arrives as a new scene_id — so the store has to
    resolve it too, or the pixel's series carries the same day twice.
    """
    if df.empty:
        return df
    if "proc" not in df.columns:
        df = df.assign(proc=_proc_time(df["scene_id"]))
    return df.sort_values(["proc", "scene_id"], kind="stable").drop_duplicates(
        subset=KEY, keep="last"
    )


def append(
    aoi_name: str,
    df: pd.DataFrame,
    part_id: str | None = None,
    store_all: bool | None = None,
) -> int:
    """Write a batch of observations as new immutable part files.

    Returns the number of rows written. `store_all` (default from
    `config.STORE_ALL_OBSERVATIONS`) decides whether cloud/shadow/snow rejects
    are kept: they are ~7x the volume of the clear rows and no model reads them,
    but they are the only record of WHY a gap in a series exists, and they cost
    ~1.3 GB across both AOIs. Re-reading them from CDSE later would cost ~2 TB
    of traffic, so this is a decision to take before the backfill, not after.
    """
    if df.empty:
        return 0
    if store_all is None:
        store_all = config.STORE_ALL_OBSERVATIONS
    df = df if store_all else df[df["clear"]]
    if df.empty:
        return 0

    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    df["proc"] = _proc_time(df["scene_id"])
    part_id = part_id or uuid.uuid4().hex[:12]

    written = 0
    for year, part in df.groupby(df["datetime"].dt.year):
        part = part[SCHEMA_COLS].sort_values(KEY).reset_index(drop=True)
        store.write_parquet(part, part_path(aoi_name, int(year), part_id))
        written += len(part)
    return written


def compact(
    aoi_name: str, years: list[int] | None = None, progress=None
) -> dict[int, int]:
    """Fold part files into one sorted parquet per year.

    Sorted by (pix_id, datetime) so that DuckDB / pyarrow row-group statistics
    prune to the handful of groups holding a pixel's series, which is the access
    pattern for everything downstream.

    Restartable: the compacted file is replaced atomically BEFORE its parts are
    removed, so an interruption leaves duplicates (which `dedupe` handles on
    read) rather than a hole.
    """
    out: dict[int, int] = {}
    for y in years if years is not None else years_present(aoi_name):
        parts = parts_for(aoi_name, y)
        if not parts:
            continue
        frames = [pd.read_parquet(p) for p in parts]
        target = obs_path(aoi_name, y)
        if target.exists():
            frames.append(pd.read_parquet(target))
        merged = pd.concat(frames, ignore_index=True)
        merged["datetime"] = pd.to_datetime(merged["datetime"], utc=True)
        merged = dedupe(merged)[SCHEMA_COLS].sort_values(KEY).reset_index(drop=True)
        store.write_parquet(merged, target)
        for p in parts:
            p.unlink()
        out[y] = len(merged)
        if progress:
            progress(f"  compacted {y}: {len(parts)} parts -> {len(merged):,} rows")
    return out


def _files(aoi_name: str, years: list[int] | None) -> list[Path]:
    yrs = years if years is not None else years_present(aoi_name)
    files = []
    for y in yrs:
        p = obs_path(aoi_name, y)
        if p.exists():
            files.append(p)
        files.extend(parts_for(aoi_name, y))
    return files


def load(
    aoi_name: str,
    years: list[int] | None = None,
    pix_ids: set[str] | None = None,
    scene_ids: set[str] | None = None,
    columns: list[str] | None = None,
    clear_only: bool = True,
    pix_range: tuple[str, str] | None = None,
) -> pd.DataFrame:
    """Read the store, part files included, deduplicated and sorted.

    `scene_ids` restricts to particular products — the replay path uses it to
    re-feed the observations of scenes that reached the store but never reached
    the model, without going back to CDSE.

    `clear_only` defaults True because every consumer in this package models
    clear-sky series; the rejects are kept (when `config.STORE_ALL_OBSERVATIONS`
    is on) for auditing, not for modelling.
    """
    files = _files(aoi_name, years)
    if not files:
        return pd.DataFrame(columns=columns or SCHEMA_COLS)

    need = None
    if columns is not None:
        need = sorted(set(columns) | set(KEY) | {"scene_id", "clear"})
    frames = []
    # `pix_range` = inclusive (lo, hi) on pix_id. The store is written sorted by
    # pixel, so the range is pushed down to the row groups and only that slice is
    # ever read -- this is what lets a bootstrap run in chunks instead of
    # materialising 117 M rows (olivinskog) at once.
    flt = (
        [("pix_id", ">=", pix_range[0]), ("pix_id", "<=", pix_range[1])]
        if pix_range
        else None
    )
    for p in files:
        df = pd.read_parquet(p, columns=need, filters=flt)
        if pix_ids is not None:
            df = df[df["pix_id"].isin(pix_ids)]
        if scene_ids is not None:
            df = df[df["scene_id"].isin(scene_ids)]
        if len(df):
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=columns or SCHEMA_COLS)

    out = pd.concat(frames, ignore_index=True)
    out["datetime"] = pd.to_datetime(out["datetime"], utc=True)
    out = dedupe(out)
    if clear_only and "clear" in out.columns:
        out = out[out["clear"]]
    if columns is not None:
        out = out[[c for c in columns if c in out.columns]]
    return out.sort_values(KEY).reset_index(drop=True)


def scene_ids_present(aoi_name: str) -> set[str]:
    """Every scene already represented in the store.

    Note this only sees scenes that contributed at least one stored row, so a
    fully clouded scene leaves no trace here when only clear rows are stored.
    `runs.py` is the authority on what has been processed.
    """
    ids: set[str] = set()
    for p in _files(aoi_name, None):
        ids |= set(pd.read_parquet(p, columns=["scene_id"])["scene_id"].unique())
    return ids


def observation_counts(aoi_name: str, pix_ids: set[str] | None = None) -> pd.Series:
    """Clear observations per pixel, over the whole store.

    Used to find pixels that were too sparse to fit at bootstrap and have since
    crossed the threshold. Pass `pix_ids` — the caller normally only cares about
    the unfitted handful, and reading four columns for those is a fraction of
    the cost of materialising the store.
    """
    frames = []
    for p in _files(aoi_name, None):
        df = pd.read_parquet(p, columns=["pix_id", "datetime", "scene_id", "clear"])
        if "clear" in df.columns:
            df = df[df["clear"]]
        if pix_ids is not None:
            df = df[df["pix_id"].isin(pix_ids)]
        if len(df):
            frames.append(df)
    if not frames:
        return pd.Series(dtype="int64")
    all_df = pd.concat(frames, ignore_index=True)
    all_df["datetime"] = pd.to_datetime(all_df["datetime"], utc=True)
    return dedupe(all_df).groupby("pix_id").size().sort_index()
