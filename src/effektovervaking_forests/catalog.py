"""CDSE STAC scene index, and the acquisition schedule derived from it.

`sync` upserts every S2 L2A item intersecting an AOI into a parquet table keyed
by item id. This index alone decides what work exists: `update` processes
scenes that are indexed and not yet in the run ledger. `schedule` predicts the
next acquisitions (10-day orbit repeat) and when they should be readable, using
the observed `published` lag.
"""

from __future__ import annotations

import datetime as dt
import json
import time

import geopandas as gpd
import pandas as pd
from pystac_client import Client
from shapely.geometry import shape

from . import aoi as aoi_mod
from . import config, store

# One satellite revisits a relative orbit every 10 days exactly.
REPEAT_DAYS = 10

# Incremental sync re-queries this far back; products published later than
# that are caught by `reconcile()`.
SYNC_OVERLAP_DAYS = 7

_FIELDS = [
    "id",
    "datetime",
    "published",
    "grid:code",
    "sat:relative_orbit",
    "sat:orbit_state",
    "platform",
    "eo:cloud_cover",
    "eo:snow_cover",
    "processing:version",
    "product:timeliness",
]


def _client() -> Client:
    return Client.open(config.STAC_URL)


_TRANSIENT = (
    "429",
    "Rate limit",
    "502 Bad Gateway",
    "503 Service",
    "504 Gateway",
    "Gateway Time-out",
    "timed out",
    # Postgres hot standby: long reads can be cancelled by replication.
    "SerializationError",
    "conflict with recovery",
    "ConnectionDoesNotExistError",
)


def _retry(fn, tries: int = 6, base: float = 5.0):
    """Call `fn`, retrying transient errors (429, 5xx) with exponential backoff.

    Anything else is re-raised immediately.
    """
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - re-raised below
            msg = str(e)
            # pystac-client's APIError carries the HTTP status; any 5xx retries.
            status = getattr(e, "status_code", None) or 0
            if status < 500 and not any(t in msg for t in _TRANSIENT):
                raise
            if i == tries - 1:
                raise
            time.sleep(base * (2**i))


def tiles_path(aoi_name: str):
    return config.CATALOG_DIR / f"{aoi_name}_tiles.json"


def resolve_tiles(aoi_name: str, refresh: bool = False) -> list[str]:
    """The MGRS tiles that overlap the AOI polygons (not just its bbox), cached.

    One 10-day window covers every tile/orbit combination (a full repeat).
    """
    p = tiles_path(aoi_name)
    if p.exists() and not refresh:
        return json.loads(p.read_text())["tiles"]

    bbox = aoi_mod.aoi_bbox(aoi_name)
    end = dt.date.today()
    start = end - dt.timedelta(days=REPEAT_DAYS + 1)
    items = _retry(
        lambda: list(
            _client()
            .search(
                collections=[config.S2_L2A_COLLECTION],
                bbox=bbox,
                datetime=f"{start}/{end}",
                limit=100,
            )
            .items()
        )
    )
    if not items:
        raise RuntimeError(
            f"{aoi_name}: no S2 items in the last {REPEAT_DAYS + 1} days"
        )

    foot = gpd.GeoDataFrame(
        pd.DataFrame(
            [
                {
                    "tile": i.properties["grid:code"].replace("MGRS-", ""),
                    "geom": shape(i.geometry),
                }
                for i in items
            ]
        ),
        geometry="geom",
        crs=4326,
    )
    polys = gpd.read_parquet(config.aoi_path(aoi_name)).to_crs(4326)
    tiles = sorted(gpd.sjoin(foot, polys, predicate="intersects")["tile"].unique())
    if not tiles:
        raise RuntimeError(f"{aoi_name}: no tile footprint intersects the polygons")

    p.write_text(
        json.dumps(
            {
                "aoi": aoi_name,
                "tiles": tiles,
                "resolved": dt.datetime.now(dt.UTC).isoformat(),
            },
            indent=1,
        )
    )
    return tiles


def catalog_path(aoi_name: str):
    return config.CATALOG_DIR / f"{aoi_name}_scenes.parquet"


def load(aoi_name: str) -> pd.DataFrame:
    p = catalog_path(aoi_name)
    if not p.exists():
        return pd.DataFrame(
            columns=[
                "id",
                "datetime",
                "published",
                "tile",
                "rel_orbit",
                "platform",
                "cloud_cover",
                "snow_cover",
                "baseline",
                "assets",
            ]
        )
    return pd.read_parquet(p)


def _item_row(item) -> dict:
    p = item.properties
    return {
        "id": item.id,
        "datetime": pd.Timestamp(p["datetime"]),
        "published": pd.Timestamp(p["published"]) if p.get("published") else pd.NaT,
        "tile": str(p.get("grid:code", "")).replace("MGRS-", ""),
        "rel_orbit": p.get("sat:relative_orbit"),
        "orbit_state": p.get("sat:orbit_state"),
        "platform": p.get("platform"),
        "cloud_cover": p.get("eo:cloud_cover"),
        "snow_cover": p.get("eo:snow_cover"),
        "baseline": p.get("processing:version"),
        # Granule footprint: lets the reader skip tiles without any network I/O.
        "bbox": list(item.bbox) if item.bbox else None,
        # Band hrefs (granule path is not derivable from the id) and
        # product_metadata, which holds BOA_ADD_OFFSET (see reader.boa_offsets).
        "assets": {
            k: item.assets[k].href
            for k in (
                list(config.ASSET_FOR_BAND.values())
                + [config.SCL_ASSET, "granule_metadata", "product_metadata"]
            )
            if k in item.assets
        },
    }


def sync(
    aoi_name: str, start: str | None = None, end: str | None = None, progress=None
) -> pd.DataFrame:
    """Fetch STAC items for the AOI and upsert them into the index.

    Defaults to an incremental sync: from the day of the newest indexed scene
    (re-fetching that day, since items can be republished) to now.
    """
    existing = load(aoi_name)
    if end is None:
        end = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).strftime("%Y-%m-%d")

    # Resume point per tile: an AOI-wide watermark would skip the history of
    # tiles left unfinished by an interrupted first sync.
    newest = {}
    if len(existing):
        newest = (
            pd.to_datetime(existing["datetime"])
            .groupby(existing["tile"])
            .max()
            .to_dict()
        )

    client = _client()
    combined = existing
    for tile in resolve_tiles(aoi_name):
        if start is not None:
            t0 = start
        elif tile in newest:
            t0 = (newest[tile] - pd.Timedelta(days=SYNC_OVERLAP_DAYS)).strftime(
                "%Y-%m-%d"
            )
        else:
            t0 = config.HISTORY_START
        rows = []
        # Yearly windows keep each retryable request short.
        for w0, w1 in _windows(t0, end):
            page = _retry(
                lambda t=tile, a=w0, b=w1: list(
                    client.search(
                        collections=[config.S2_L2A_COLLECTION],
                        filter={
                            "op": "=",
                            "args": [{"property": "grid:code"}, f"MGRS-{t}"],
                        },
                        datetime=f"{a}/{b}",
                        limit=100,
                    ).items()
                )
            )
            rows.extend(_item_row(i) for i in page)
        if progress is not None:
            progress(tile, len(rows))
        if not rows:
            continue
        # Commit after every tile, so an interruption keeps what was fetched.
        combined = (
            pd.concat([combined, pd.DataFrame(rows)], ignore_index=True)
            .drop_duplicates(subset="id", keep="last")
            .sort_values("datetime")
            .reset_index(drop=True)
        )
        store.write_parquet(combined, catalog_path(aoi_name))
    return combined


def _windows(start: str, end: str) -> list[tuple[str, str]]:
    """[start, end] cut at calendar-year boundaries (one window if short)."""
    a, b = pd.Timestamp(start), pd.Timestamp(end)
    cuts = (
        [a]
        + [pd.Timestamp(year=y, month=1, day=1) for y in range(a.year + 1, b.year + 1)]
        + [b]
    )
    return [
        (c0.strftime("%Y-%m-%d"), c1.strftime("%Y-%m-%d"))
        for c0, c1 in zip(cuts[:-1], cuts[1:])
        if c1 > c0
    ]


def reconcile(aoi_name: str, days: int = 30, progress=None) -> dict:
    """Re-query by publication time to catch late-published or reprocessed scenes.

    `sync` only looks forward from the newest acquisition, so it misses both;
    run this periodically. Found scenes are then picked up as pending.
    """
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    client = _client()
    rows = []
    for tile in resolve_tiles(aoi_name):
        page = _retry(
            lambda t=tile: list(
                client.search(
                    collections=[config.S2_L2A_COLLECTION],
                    filter={
                        "op": "and",
                        "args": [
                            {
                                "op": "=",
                                "args": [{"property": "grid:code"}, f"MGRS-{t}"],
                            },
                            {
                                "op": ">=",
                                "args": [
                                    {"property": "published"},
                                    since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                ],
                            },
                        ],
                    },
                    limit=100,
                ).items()
            )
        )
        rows.extend(_item_row(i) for i in page)
        if progress is not None:
            progress(tile, len(page))

    existing = load(aoi_name)
    fresh = pd.DataFrame(rows)
    if fresh.empty:
        return {"aoi": aoi_name, "n_seen": 0, "n_new": 0, "days": days}
    known = set(existing["id"]) if len(existing) else set()
    n_new = int((~fresh["id"].isin(known)).sum())
    combined = (
        pd.concat([existing, fresh], ignore_index=True)
        .drop_duplicates(subset="id", keep="last")
        .sort_values("datetime")
        .reset_index(drop=True)
    )
    store.write_parquet(combined, catalog_path(aoi_name))
    return {
        "aoi": aoi_name,
        "n_seen": len(fresh),
        "n_new": n_new,
        "days": days,
        "oldest_acquisition_seen": str(pd.to_datetime(fresh["datetime"]).min()),
    }


def publication_lag(df: pd.DataFrame, recent_days: int = 90) -> pd.Series:
    """Observed acquisition -> availability lag, in hours, over recent scenes.

    Restricted to recent acquisitions because reprocessed back-catalogue scenes
    carry a `published` date years after acquisition.
    """
    d = df.dropna(subset=["published"]).copy()
    acq = pd.to_datetime(d["datetime"], utc=True)
    if recent_days:
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=recent_days)
        keep = acq >= cutoff
        if keep.sum() >= 20:  # enough for a real estimate
            d, acq = d[keep], acq[keep]
    lag = (pd.to_datetime(d["published"], utc=True) - acq).dt.total_seconds() / 3600.0
    # A negative lag is a metadata artefact, not a time machine.
    return lag[lag >= 0]


def schedule(aoi_name: str, horizon_days: int = 14) -> pd.DataFrame:
    """Predict upcoming acquisitions per (tile, relative orbit, platform).

    Each track repeats every 10 days; `expected_available` adds the median
    observed publication lag.
    """
    df = load(aoi_name)
    if df.empty:
        raise ValueError(f"no catalogue for {aoi_name}; run `catalog sync` first")

    df = df.copy()
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    lag_h = publication_lag(df).median()
    now = pd.Timestamp.now(tz="UTC")

    out = []
    for (tile, orbit, plat), g in df.groupby(["tile", "rel_orbit", "platform"]):
        last = g["datetime"].max()
        # Skip tracks gone quiet (retired satellite, changed orbit).
        if (now - last) > pd.Timedelta(days=3 * REPEAT_DAYS):
            continue
        nxt = last
        while nxt <= now:
            nxt += pd.Timedelta(days=REPEAT_DAYS)
        while nxt <= now + pd.Timedelta(days=horizon_days):
            out.append(
                {
                    "aoi": aoi_name,
                    "tile": tile,
                    "rel_orbit": orbit,
                    "platform": plat,
                    "expected_acquisition": nxt,
                    "expected_available": nxt + pd.Timedelta(hours=float(lag_h)),
                    "last_seen": last,
                    "n_observed": len(g),
                }
            )
            nxt += pd.Timedelta(days=REPEAT_DAYS)

    res = pd.DataFrame(out)
    return (
        res.sort_values("expected_acquisition").reset_index(drop=True)
        if len(res)
        else res
    )


def next_availability(aoi_name: str) -> pd.Timestamp | None:
    """When the next scene over this AOI is expected to be readable."""
    s = schedule(aoi_name, horizon_days=REPEAT_DAYS + 2)
    return None if s.empty else s["expected_available"].min()


def dedupe_versions(df: pd.DataFrame) -> pd.DataFrame:
    """Keep one product per (sensing time, tile): the most recently processed.

    CDSE serves every reprocessed version of a granule under a different
    scene_id, which the (pix_id, scene_id) key cannot deduplicate. The last
    field of a product id is its processing datetime, so sort on (baseline,
    processing time) and keep the last.
    """
    if df.empty:
        return df
    d = df.copy()
    d["_sensing"] = d["id"].str.split("_").str[2]
    d["_proc"] = d["id"].str.split("_").str[-1]
    d = (
        d.sort_values(["baseline", "_proc"])
        .drop_duplicates(subset=["_sensing", "tile"], keep="last")
        .drop(columns=["_sensing", "_proc"])
    )
    return d.sort_values("datetime").reset_index(drop=True)


def pending(
    aoi_name: str, processed_ids: set[str], max_cloud: float | None = None
) -> pd.DataFrame:
    """Indexed scenes not yet processed, oldest first.

    `max_cloud` (scene-level %) defaults to off: the per-pixel SCL mask is the
    real filter.
    """
    df = dedupe_versions(load(aoi_name))
    if df.empty:
        return df
    out = df[~df["id"].isin(processed_ids)]
    if max_cloud is not None:
        out = out[out["cloud_cover"].fillna(0) <= max_cloud]
    return out.sort_values("datetime").reset_index(drop=True)
