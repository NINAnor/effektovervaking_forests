"""CDSE STAC scene index, and the acquisition schedule derived from it.

Two jobs.

**Index.** `sync` pulls every S2 L2A item intersecting an AOI into a parquet
table, upserting by item id so re-running is cheap and safe. This table is the
only thing that decides what work exists: `update` processes scenes that are in
the index and not yet in the run ledger.

**Schedule.** Sentinel-2 is not a stream that has to be polled blindly. Each
satellite repeats a given relative orbit every 10 days to the second, and each
STAC item carries `published` — the moment the product actually became
downloadable. So from the index alone we can say, per AOI, when the next
acquisition happens and when it is likely to be readable. `schedule` reports
that, and the CLI uses it so a scheduled run that fires early can exit quietly
instead of hammering the catalogue.

Note `published` is genuinely observed, not assumed: L2A latency varies with
reprocessing campaigns and ESA's own queue, so the lag is reported as a measured
distribution rather than the nominal PT24H timeliness in the item properties.
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

# Sentinel-2 orbital repeat: one satellite revisits the same relative orbit
# every 10 days exactly. With two operating satellites on the same orbit the
# effective revisit at the AOI is 5 days.
REPEAT_DAYS = 10

# How far back an incremental sync re-queries. A one-day overlap assumes every
# product is published within a day of acquisition; the measured lag
# distribution has a tail well past that, and reprocessed products appear
# arbitrarily late. Anything published outside this window is invisible to a
# sync-by-acquisition-date forever, so the window is a week and `reconcile()`
# exists for the rest.
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
    # CDSE STAC is Postgres on a hot standby; a long read can be
    # cancelled by replication ("conflict with recovery").
    "SerializationError",
    "conflict with recovery",
    "ConnectionDoesNotExistError",
)


def _retry(fn, tries: int = 6, base: float = 5.0):
    """CDSE fronts its STAC with a WAF that returns 429 under modest load.

    That is a throttle, not a failure, so back off exponentially rather than
    letting an unattended run die on it. Gateway errors (502/503/504) are the
    same kind of thing — a full-history search of one tile is ~25 pages, and
    olivinskog's first sync died on a 504 mid-pagination. Anything else is
    re-raised immediately — a 404 or a schema error should be loud.
    """
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - re-raised below
            msg = str(e)
            # pystac-client's APIError carries the HTTP status; any 5xx is the
            # server's problem (CDSE has produced 504s, Postgres replication
            # cancels and dropped DB connections), not a reason to die.
            status = getattr(e, "status_code", None) or 0
            if status < 500 and not any(t in msg for t in _TRANSIENT):
                raise
            if i == tries - 1:
                raise
            time.sleep(base * (2**i))


def tiles_path(aoi_name: str):
    return config.CATALOG_DIR / f"{aoi_name}_tiles.json"


def resolve_tiles(aoi_name: str, refresh: bool = False) -> list[str]:
    """The MGRS tiles that actually overlap the AOI polygons.

    Worth doing precisely: kalklindeskog's bounding box spans 12 tiles but its
    polygons touch only 4, so searching by bbox would triple the catalogue and
    the download volume for nothing. One 10-day window is enough to enumerate
    every tile/orbit combination, since that is a full orbital repeat.
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
        # Granule footprint bbox: lets the reader skip a tile that holds none of
        # our pixels without opening anything over the network.
        "bbox": list(item.bbox) if item.bbox else None,
        # Keep the per-band hrefs: they encode the granule path, which is not
        # otherwise derivable from the item id, and saves a STAC round trip at
        # read time.
        # Keep product_metadata too: BOA_ADD_OFFSET lives there, and it is what
        # reproduces Earth Engine's *_HARMONIZED scaling (see reader.boa_offsets).
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

    # Resume point PER TILE. With one AOI-wide watermark, an interrupted first
    # sync (some tiles done, some not) would restart from the newest scene of the
    # finished tiles and never fetch the others' history.
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
        # Yearly windows: a short request is cheap to retry, a nine-year
        # paginated one is not.
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
    """Re-query by PUBLICATION time, not acquisition time.

    `sync` walks forward from the newest acquisition it already holds, so it can
    only ever find products whose sensing date is recent. Two real cases escape
    it: a scene published very late, and a granule REPROCESSED under a new
    baseline — the latter has an old sensing date by definition and would never
    be looked for again.

    This is the periodic sweep that catches both. Run it weekly; it is one
    filtered STAC query per tile, and anything it finds is a scene the ingest
    path then picks up as pending in the normal way.
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

    Restricted to recent acquisitions on purpose. `published` is when the
    product entered the CDSE archive, and ESA reprocesses the back catalogue in
    campaigns — so a 2017 scene reprocessed under a new baseline in 2024 carries
    a "lag" of seven years. Including those puts the median around 19,000 hours
    and makes the number worse than useless for scheduling. Over the last
    quarter, no reprocessing has had time to happen, so `published` means what
    we want it to mean: when the scene actually became readable.
    """
    d = df.dropna(subset=["published"]).copy()
    acq = pd.to_datetime(d["datetime"], utc=True)
    if recent_days:
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=recent_days)
        keep = acq >= cutoff
        if keep.sum() >= 20:  # enough to be a real estimate
            d, acq = d[keep], acq[keep]
    lag = (pd.to_datetime(d["published"], utc=True) - acq).dt.total_seconds() / 3600.0
    # A negative lag is a metadata artefact, not a time machine.
    return lag[lag >= 0]


def schedule(aoi_name: str, horizon_days: int = 14) -> pd.DataFrame:
    """Predict upcoming acquisitions per (tile, relative orbit, platform).

    Each such track repeats every 10 days, so the next acquisitions follow from
    the most recent one by simple arithmetic. `expected_available` adds the
    median observed publication lag for that AOI.
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
        # Has this track gone quiet? A satellite retired or an orbit changed
        # shows up as a track whose last acquisition is many repeats old.
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
    """Keep one product per (sensing time, tile) — the most recently processed.

    ESA reprocesses granules, and CDSE serves every version: the same
    acquisition over the same tile appears as several products differing only in
    baseline and processing timestamp (e.g. ..._20260809T190509 and
    ..._20260809T170612). Ingesting both would enter one acquisition into the
    observation store twice under different scene_ids, which the (pix_id,
    scene_id) key cannot deduplicate. S-CCD would then see a doubled
    observation density and, where the two versions differ, contradictory
    values on the same day.

    The last field of a CDSE product id is its processing datetime, so sorting
    on (baseline, processing time) and keeping the last gives the newest
    version. Found when both versions matched a single Earth Engine
    observation.
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

    `max_cloud` filters on the scene-level cloud percentage. That is a coarse
    screen over a whole 110 km tile and says little about our few hundred
    hectares, so it defaults to off: the per-pixel SCL mask is the real filter,
    and a 97%-cloudy tile can still be clear over Vestfold.
    """
    df = dedupe_versions(load(aoi_name))
    if df.empty:
        return df
    out = df[~df["id"].isin(processed_ids)]
    if max_cloud is not None:
        out = out[out["cloud_cover"].fillna(0) <= max_cloud]
    return out.sort_values("datetime").reset_index(drop=True)
