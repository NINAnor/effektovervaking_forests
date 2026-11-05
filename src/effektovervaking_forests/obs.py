"""The observation store: per-pixel reflectance, accumulated over time.

The persisted boundary between CDSE reads and S-CCD, so parameters can change
without re-downloading imagery. Rows are keyed by (pix_id, datetime): a
reprocessed scene replaces its earlier rows rather than duplicating them.

Write path: each batch writes new immutable part files per year and never
rewrites an existing file; `compact()` folds parts into the yearly file as a
separate, restartable step. A crash between the two leaves duplicates, which
the read path removes via `dedupe`.
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

# Dedupe key: a pixel has one home tile, so (pix_id, datetime) identifies an
# observation regardless of which (reprocessed) product delivered it.
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
    """The processing datetime in a CDSE product id (its last field).

    Decides which reprocessed version wins; ids without one sort first.
    """
    return scene_ids.astype(str).str.rsplit("_", n=1).str[-1].fillna("")


def dedupe(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (pixel, acquisition): the most recently processed product.

    `catalog.dedupe_versions` only covers the pending list; a version arriving
    after the old one was ingested must be resolved here.
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
    `config.STORE_ALL_OBSERVATIONS`) keeps cloud/shadow/snow rejects too.
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

    Sorted by (pix_id, datetime) so row-group statistics prune to a pixel's
    series. The compacted file is replaced atomically BEFORE its parts are
    removed, so an interruption leaves duplicates rather than a hole.
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

    `scene_ids` restricts to particular products (used by the replay path).
    `clear_only` drops stored rejects, which exist only for auditing.
    """
    files = _files(aoi_name, years)
    if not files:
        return pd.DataFrame(columns=columns or SCHEMA_COLS)

    need = None
    if columns is not None:
        need = sorted(set(columns) | set(KEY) | {"scene_id", "clear"})
    frames = []
    # `pix_range` = inclusive (lo, hi) on pix_id, pushed down to row groups so
    # a bootstrap can run in chunks.
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
    """Every scene with at least one stored row.

    A fully clouded scene leaves no trace here; `runs.py` is the authority on
    what has been processed.
    """
    ids: set[str] = set()
    for p in _files(aoi_name, None):
        ids |= set(pd.read_parquet(p, columns=["scene_id"])["scene_id"].unique())
    return ids


def observation_counts(aoi_name: str, pix_ids: set[str] | None = None) -> pd.Series:
    """Clear observations per pixel, over the whole store.

    Used to find pixels too sparse at bootstrap that have since crossed the
    threshold; pass `pix_ids` to avoid materialising the store.
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
