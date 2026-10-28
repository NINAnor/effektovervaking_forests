"""Benchmark the ways of getting the same pixels, so the choice is measured.

Two workloads with genuinely different shapes:

* **Operational** — one or two new scenes, a few times a day. Wall-clock latency
  for a single scene is what matters; total throughput is irrelevant because
  there is almost nothing to do.
* **Backfill** — ~15k scenes, once. Throughput under concurrency is everything,
  and a per-scene fixed cost that is trivial operationally (say 2 s) becomes
  8.5 hours across the archive.

An option can easily win one and lose the other, which is why they are reported
separately rather than as a single "fastest" verdict.

Methods
-------
``cdse_jp2``     CDSE JPEG 2000 over /vsis3, block-aligned windowed reads.
                 The current pipeline.
``e84_cog``      Element84 sentinel-cogs COGs over /vsis3 (us-west-2), same
                 windowed-read strategy. Proven pixel-identical to CDSE.
``e84_vzarr``    The same COGs through virtual-tiff -> VirtualiZarr -> xarray,
                 i.e. the aef_loader_plus stack, with an optional on-disk
                 manifest cache so the cold/warm difference is visible.

Geography is a first-class variable here: CDSE is hosted in Europe and
sentinel-cogs in us-west-2, so from Norway the two are not competing on equal
network terms. That shows up in the per-scene latency, which is exactly what the
operational workload is sensitive to.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import rowcol
from rasterio.windows import Window

from . import aoi as aoi_mod
from . import catalog, config, reader

E84_STAC = "https://earth-search.aws.element84.com/v1"
E84_ASSET = {
    "B02": "blue",
    "B03": "green",
    "B04": "red",
    "B08": "nir",
    "B11": "swir16",
    "B12": "swir22",
}


def _e84_env() -> rasterio.Env:
    """sentinel-cogs is public — unsigned requests, no credentials."""
    return rasterio.Env(
        AWS_NO_SIGN_REQUEST="YES",
        GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
        GDAL_HTTP_MAX_RETRY="5",
        GDAL_HTTP_RETRY_DELAY="2",
        VSI_CACHE="TRUE",
        VSI_CACHE_SIZE="26214400",
    )


def _read_points(path: str, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Block-aligned windowed read — the same strategy reader.py uses."""
    with rasterio.open(path) as ds:
        rows, cols = rowcol(ds.transform, xs, ys)
        rows, cols = np.asarray(rows, "int64"), np.asarray(cols, "int64")
        ok = (rows >= 0) & (rows < ds.height) & (cols >= 0) & (cols < ds.width)
        out = np.zeros(len(xs), "int32")
        if not ok.any():
            return out
        bh, bw = ds.block_shapes[0]
        br, bc = np.where(ok, rows // bh, -1), np.where(ok, cols // bw, -1)
        for r_blk, c_blk in {t for t in zip(br[ok], bc[ok])}:
            sel = ok & (br == r_blk) & (bc == c_blk)
            r0, c0 = r_blk * bh, c_blk * bw
            win = Window(c0, r0, min(bw, ds.width - c0), min(bh, ds.height - r0))
            blk = ds.read(1, window=win)
            out[sel] = blk[rows[sel] - r0, cols[sel] - c0]
        return out


def read_cdse(scene: pd.Series, px: pd.DataFrame) -> int:
    df = reader.read_scene(scene, px)
    return len(df)


def read_e84_cog(item, px: pd.DataFrame) -> int:
    xs, ys = px["utm_x"].to_numpy(), px["utm_y"].to_numpy()
    n = 0
    with _e84_env():
        for _band, asset in E84_ASSET.items():
            n = len(_read_points(item.assets[asset].href, xs, ys))
        _read_points(item.assets["scl"].href, xs, ys)
    return n


def read_e84_vzarr(item, px: pd.DataFrame, cache_dir=None) -> int:
    """The aef_loader_plus path: COG header -> manifest -> zarr -> windowed read.

    Reads through xarray rather than GDAL. Point sampling is not what this stack
    is designed for — it shines on contiguous windows — so a poor showing here
    is a statement about our access pattern, not about the stack.
    """
    import xarray as xr
    from obspec_utils.registry import ObjectStoreRegistry
    from obstore.store import S3Store
    from virtual_tiff import VirtualTIFF

    store = S3Store("sentinel-cogs", region="us-west-2", skip_signature=True)
    reg = ObjectStoreRegistry({"s3://sentinel-cogs": store})
    parser = VirtualTIFF(ifd=0)
    xs, ys = px["utm_x"].to_numpy(), px["utm_y"].to_numpy()
    n = 0
    for _band, asset in E84_ASSET.items():
        href = item.assets[asset].href
        url = "s3://sentinel-cogs/" + href.split("amazonaws.com/", 1)[1]
        ms = parser(url=url, registry=reg)
        ds = xr.open_zarr(ms, consolidated=False, zarr_format=3, chunks={})
        var = list(ds.data_vars)[0]
        da = ds[var]
        # Map map-coords to array indices via the COG's own transform.
        with _e84_env(), rasterio.open(href) as rio:
            rows, cols = rowcol(rio.transform, xs, ys)
        rows = np.clip(np.asarray(rows), 0, da.shape[-2] - 1)
        cols = np.clip(np.asarray(cols), 0, da.shape[-1] - 1)
        r0, r1 = int(rows.min()), int(rows.max()) + 1
        c0, c1 = int(cols.min()), int(cols.max()) + 1
        _block = da[..., r0:r1, c0:c1].values
        n = len(rows)
    return n


def _e84_match(es, scene: pd.Series):
    """The Element84 item for a CDSE scene.

    Matched on (date, tile, platform): the two archives timestamp an acquisition
    differently — CDSE reports the datatake start, Element84 the per-tile granule
    sensing time, ~4 minutes apart — so an instant-based match finds nothing.
    """
    day = pd.Timestamp(scene["datetime"]).strftime("%Y-%m-%d")
    plat = scene["platform"].replace("sentinel-", "S").upper()
    tile = scene["tile"]
    cands = [
        i
        for i in es.search(
            collections=["sentinel-2-l2a"],
            datetime=f"{day}T00:00:00Z/{day}T23:59:59Z",
            query={"grid:code": {"eq": f"MGRS-{tile}"}},
            max_items=10,
        ).items()
        if i.id.startswith(plat)
    ]
    if not cands:
        return None
    return sorted(cands, key=lambda i: int(i.id.split("_")[-2]))[-1]


def run(
    aoi_name: str,
    n_scenes: int = 6,
    methods=("cdse_jp2", "e84_cog", "e84_vzarr"),
    progress=None,
) -> pd.DataFrame:
    """Time each method on the same scenes, one scene at a time (latency)."""
    from pystac_client import Client

    grid = aoi_mod.load_grid(aoi_name)
    cat = catalog.dedupe_versions(catalog.load(aoi_name))
    cat = cat.sort_values("datetime").tail(300)
    es = Client.open(E84_STAC)

    rows = []
    used = 0
    for _, sc in cat.iloc[::-1].iterrows():
        px = reader.pixels_for_scene(grid, sc)
        if len(px) < 500:
            continue
        item = _e84_match(es, sc) if any(m.startswith("e84") for m in methods) else None
        if item is None and any(m.startswith("e84") for m in methods):
            continue
        for m in methods:
            t0 = time.time()
            try:
                if m == "cdse_jp2":
                    _npx = read_cdse(sc, px)
                elif m == "e84_cog":
                    _npx = read_e84_cog(item, px)
                elif m == "e84_vzarr":
                    _npx = read_e84_vzarr(item, px)
                else:
                    continue
                rows.append(
                    {
                        "scene": sc["id"],
                        "tile": sc["tile"],
                        "method": m,
                        "seconds": round(time.time() - t0, 2),
                        "n_pixels": int(len(px)),
                        "ok": True,
                    }
                )
            except Exception as e:  # noqa: BLE001
                rows.append(
                    {
                        "scene": sc["id"],
                        "tile": sc["tile"],
                        "method": m,
                        "seconds": round(time.time() - t0, 2),
                        "n_pixels": int(len(px)),
                        "ok": False,
                        "error": f"{type(e).__name__}: {e}"[:120],
                    }
                )
            if progress:
                progress(rows[-1])
        used += 1
        if used >= n_scenes:
            break
    return pd.DataFrame(rows)


def concurrency(
    aoi_name: str,
    method: str = "cdse_jp2",
    n_scenes: int = 16,
    workers_list=(1, 4, 8, 16),
    progress=None,
) -> pd.DataFrame:
    """Throughput vs worker count — the backfill question.

    Latency per scene barely matters for a 15k-scene backfill; what matters is
    scenes per minute at the concurrency the source tolerates. Sources throttle,
    so this is measured rather than assumed to scale linearly.
    """
    grid = aoi_mod.load_grid(aoi_name)
    cat = (
        catalog.dedupe_versions(catalog.load(aoi_name))
        .sort_values("datetime")
        .tail(400)
    )
    scenes = []
    for _, sc in cat.iloc[::-1].iterrows():
        if len(reader.pixels_for_scene(grid, sc)) >= 500:
            scenes.append(sc)
        if len(scenes) >= n_scenes:
            break

    out = []
    for w in workers_list:
        t0 = time.time()
        okc = 0
        with ThreadPoolExecutor(max_workers=w) as pool:
            futs = [
                pool.submit(read_cdse, sc, reader.pixels_for_scene(grid, sc))
                for sc in scenes
            ]
            for f in as_completed(futs):
                try:
                    f.result()
                    okc += 1
                except Exception:  # noqa: BLE001
                    pass
        el = time.time() - t0
        rec = {
            "method": method,
            "workers": w,
            "n_scenes": len(scenes),
            "ok": okc,
            "seconds": round(el, 1),
            "scenes_per_min": round(len(scenes) / el * 60, 1),
        }
        out.append(rec)
        if progress:
            progress(rec)
    return pd.DataFrame(out)


def report_path(aoi_name: str):
    return config.OPS / f"{aoi_name}_bench.json"


def summarise(
    latency: pd.DataFrame, conc: pd.DataFrame | None, total_scenes: int
) -> dict:
    """Per-scene latency, and what it implies for a full backfill."""
    lat = (
        latency[latency.ok]
        .groupby("method")["seconds"]
        .agg(["count", "median", "mean", "min", "max"])
        .round(2)
    )
    est = {}
    if conc is not None and len(conc):
        best = conc.sort_values("scenes_per_min").iloc[-1]
        est = {
            "best_workers": int(best.workers),
            "scenes_per_min": float(best.scenes_per_min),
            "backfill_hours": round(total_scenes / best.scenes_per_min / 60, 1),
        }
    return {
        "latency_per_scene": json.loads(lat.to_json(orient="index")),
        "backfill_estimate": est,
        "total_scenes": int(total_scenes),
    }


# ═══════════════════════════════════════════════════════════════════════════
# EOPF Zarr, and what "optimal usage" of it actually means
# ═══════════════════════════════════════════════════════════════════════════
EOPF_STAC = "https://stac.core.eopf.eodc.eu"

# EOPF band -> array path inside the product's single Zarr store. Note that
# every one of these lives in ONE store: unlike JP2 or COG, where each band is a
# separate object needing its own open, a Zarr product is a hierarchy whose
# metadata can be fetched once for the whole thing. That is the entire basis of
# the optimisation below.
EOPF_ARRAY = {
    "B02": "measurements/reflectance/r10m/b02",
    "B03": "measurements/reflectance/r10m/b03",
    "B04": "measurements/reflectance/r10m/b04",
    "B08": "measurements/reflectance/r10m/b08",
    "B11": "measurements/reflectance/r20m/b11",
    "B12": "measurements/reflectance/r20m/b12",
}
EOPF_SCL = "conditions/mask/l2a_classification/r20m/scl"
EOPF_GRP_RES = {"r10m": 10, "r20m": 20}


def eopf_match(scene: pd.Series):
    """The EOPF item for a CDSE scene — matched on product id where possible.

    EOPF republishes the *same ESA product*, so the id is usually identical to
    CDSE's. It is still matched by (day, tile, platform) rather than by id
    equality, because a reprocessing can leave the two archives on different
    processing timestamps for one acquisition.
    """
    from pystac_client import Client

    day = pd.Timestamp(scene["datetime"]).strftime("%Y-%m-%d")
    plat = str(scene["id"]).split("_")[0]
    items = list(
        Client.open(EOPF_STAC)
        .search(
            collections=["sentinel-2-l2a"],
            datetime=f"{day}T00:00:00Z/{day}T23:59:59Z",
            query={"grid:code": {"eq": f"MGRS-{scene['tile']}"}},
            max_items=20,
        )
        .items()
    )
    cands = [i for i in items if i.id.startswith(plat)]
    if not cands:
        return None
    same = [i for i in cands if i.id[:44] == str(scene["id"])[:44]]
    return (same or sorted(cands, key=lambda i: i.id))[-1]


def _eopf_root(item) -> str:
    """The product's root .zarr href — the thing worth opening once."""
    if "product" in item.assets:
        return item.assets["product"].href
    href = item.assets["SR_10m"].href
    return href.split(".zarr")[0] + ".zarr"


def read_eopf_serial(item, px: pd.DataFrame) -> tuple[int, dict]:
    """The same reads, one after another — the baseline the optimisation beats.

    Only the concurrency is removed, so the difference against
    `read_eopf_optimised` is attributable to it alone rather than to also
    changing how much data is fetched.

    A finding worth recording separately: the *obvious* structure-following
    approach — open each band group (`SR_10m`, `SR_20m`, `SCL_20m`) as its own
    dataset, which is how the STAC assets invite you to read the product — does
    not work at all here. `xr.open_zarr` and `zarr.open_group` on those asset
    hrefs both return an EMPTY group: the child arrays are only discoverable
    through the product root's consolidated metadata. So "open the root once" is
    not merely the fast path for EOPF, it is the only path, and any benchmark
    that reported per-group opens was paying for metadata it then could not use.
    """
    import zarr

    t0 = time.time()
    g = zarr.open_consolidated(_eopf_root(item), mode="r")
    open_s = time.time() - t0

    xs, ys = px["utm_x"].to_numpy("float64"), px["utm_y"].to_numpy("float64")
    t_c = time.time()
    coords = {
        res: (
            g[f"measurements/reflectance/{res}/y"][:],
            g[f"measurements/reflectance/{res}/x"][:],
        )
        for res in ("r10m", "r20m")
    }
    coord_s = time.time() - t_c

    t_r = time.time()
    n, n_blocks = 0, 0
    for _band, path in list(EOPF_ARRAY.items()) + [("SCL", EOPF_SCL)]:
        grp = "r20m" if "r20m" in path else "r10m"
        yc, xc = coords[grp]
        arr = g[path]
        rows, cols = _eopf_rowcol(yc, xc, xs, ys, EOPF_GRP_RES[grp])
        ch_y, ch_x = arr.chunks
        for br, bc in {(int(r), int(c)) for r, c in zip(rows // ch_y, cols // ch_x)}:
            r0, c0 = br * ch_y, bc * ch_x
            arr[r0 : min(r0 + ch_y, arr.shape[0]), c0 : min(c0 + ch_x, arr.shape[1])]
            n_blocks += 1
        n = len(rows)
    return n, {
        "open_s": round(open_s, 2),
        "coord_s": round(coord_s, 2),
        "read_s": round(time.time() - t_r, 2),
        "total_s": round(time.time() - t0, 2),
        "n_chunk_reads": n_blocks,
        "n_opens": 1,
    }


def _eopf_rowcol(
    yc: np.ndarray, xc: np.ndarray, xs: np.ndarray, ys: np.ndarray, res: int
) -> tuple[np.ndarray, np.ndarray]:
    """Map coords -> array indices using the array's own pixel-CENTRE coords.

    EOPF stores centres (x[0] = 499985 for a tile whose west edge is 499980),
    while GDAL's `rowcol` works off the corner-based affine transform. Doing this
    by arithmetic on the first coordinate rather than by nearest-neighbour search
    keeps the two definitions of "the containing cell" identical, which is what
    makes the value comparison meaningful.
    """
    x0, y0 = float(xc[0]), float(yc[0])
    cols = np.floor((xs - (x0 - res / 2)) / res).astype("int64")
    rows = np.floor(((y0 + res / 2) - ys) / res).astype("int64")
    np.clip(cols, 0, len(xc) - 1, out=cols)
    np.clip(rows, 0, len(yc) - 1, out=rows)
    return rows, cols


def read_eopf_optimised(
    item, px: pd.DataFrame, workers: int = 7
) -> tuple[int, dict, pd.DataFrame]:
    """One consolidated metadata read for the whole product, then chunk-aligned
    concurrent gathers.

    Three changes from the naive path, each targeting a measured cost:

    1. **`zarr.open_consolidated` on the product ROOT**, not `open_zarr` per
       band group. EOPF ships consolidated metadata at the root, so a single
       request describes every array in the product — all six bands, SCL and the
       coordinate vectors. This replaces three group opens (2.2 s measured) with
       one (~0.6 s), and it is the direct analogue of what made CDSE 4x faster:
       kill the per-band fixed cost.

    2. **Chunk-aligned reads.** The arrays are chunked 2048x2048, so a read is
       only ever cheap if it is expressed as whole chunks. Our pixels are
       scattered, so we group them by chunk and fetch each touched chunk once —
       the same strategy `reader._read_at` uses on JP2 blocks.

    3. **Concurrency across chunk fetches**, not across band opens. With one
       metadata read there is nothing left to parallelise at the band level; the
       remaining latency is chunk GETs, and those are what the pool works on.

    Returns the per-pixel values too, so the result can be compared against CDSE
    rather than merely timed.
    """
    import zarr

    t0 = time.time()
    root = _eopf_root(item)
    g = zarr.open_consolidated(root, mode="r")
    open_s = time.time() - t0

    xs, ys = px["utm_x"].to_numpy("float64"), px["utm_y"].to_numpy("float64")
    t_c = time.time()
    coords = {
        res: (
            g[f"measurements/reflectance/{res}/y"][:],
            g[f"measurements/reflectance/{res}/x"][:],
        )
        for res in ("r10m", "r20m")
    }
    coord_s = time.time() - t_c

    jobs = [(b, p) for b, p in EOPF_ARRAY.items()] + [("SCL", EOPF_SCL)]

    def _one(job):
        band, path = job
        grp = "r20m" if ("r20m" in path) else "r10m"
        yc, xc = coords[grp]
        arr = g[path]
        rows, cols = _eopf_rowcol(yc, xc, xs, ys, EOPF_GRP_RES[grp])
        ch_y, ch_x = arr.chunks
        out = np.zeros(len(xs), dtype="int32")
        blocks = {(int(r), int(c)) for r, c in zip(rows // ch_y, cols // ch_x)}
        for br, bc in blocks:
            sel = (rows // ch_y == br) & (cols // ch_x == bc)
            r0, c0 = br * ch_y, bc * ch_x
            blk = arr[
                r0 : min(r0 + ch_y, arr.shape[0]), c0 : min(c0 + ch_x, arr.shape[1])
            ]
            out[sel] = blk[rows[sel] - r0, cols[sel] - c0]
        return band, out, len(blocks)

    t_r = time.time()
    vals, n_blocks = {}, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for band, out, nb in pool.map(_one, jobs):
            vals[band] = out
            n_blocks += nb
    read_s = time.time() - t_r

    df = pd.DataFrame({"pix_id": px["pix_id"].to_numpy()})
    for band in config.BANDS:
        # EOPF carries the baseline offset as array metadata (add_offset=-0.1,
        # scale_factor=1e-4), so the harmonized DN is DN + add_offset*10000 —
        # i.e. the BOA_ADD_OFFSET the CDSE reader has to fetch from MTD XML is
        # simply *there*. One fewer network round trip, and no baseline guessing.
        off = int(
            round(
                float(g[EOPF_ARRAY[band]].attrs.get("add_offset", 0.0))
                * config.DN_SCALE
            )
        )
        v = vals[band]
        df[band] = np.where(v == config.NODATA_DN, 0, v + off).astype("int32")
    df["scl"] = vals["SCL"].astype("uint8")

    return (
        len(df),
        {
            "open_s": round(open_s, 2),
            "coord_s": round(coord_s, 2),
            "read_s": round(read_s, 2),
            "total_s": round(time.time() - t0, 2),
            "n_chunk_reads": n_blocks,
            "n_opens": 1,
        },
        df,
    )


def compare_eopf_cdse(
    aoi_name: str, n_scenes: int = 4, progress=None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Time AND value-compare CDSE JP2 vs serial EOPF vs optimised EOPF.

    Same scenes, same pixels, three code paths. Timing alone would not be enough:
    a faster reader that returns different numbers is not a faster reader, it is
    a different dataset, and the parity gate in `parity.py` exists precisely
    because that distinction has bitten this project before.
    """
    grid = aoi_mod.load_grid(aoi_name)
    cat = catalog.dedupe_versions(catalog.load(aoi_name)).sort_values("datetime")
    rows, diffs = [], []
    used = 0
    for _, sc in cat.iloc[::-1].iterrows():
        px = reader.pixels_for_scene(grid, sc)
        if len(px) < 500:
            continue
        item = eopf_match(sc)
        if item is None:
            continue

        rec = {"scene": sc["id"], "tile": sc["tile"], "n_pixels": int(len(px))}
        t0 = time.time()
        try:
            cdse_df = reader.read_scene(sc, px, scl_first=False)
            rec["cdse_s"] = round(time.time() - t0, 2)
        except Exception as e:  # noqa: BLE001
            rec["cdse_s"], rec["cdse_error"] = None, str(e)[:120]
            cdse_df = None
        try:
            _, m = read_eopf_serial(item, px)
            rec.update({f"eopf_serial_{k}": v for k, v in m.items()})
        except Exception as e:  # noqa: BLE001
            rec["eopf_serial_error"] = f"{type(e).__name__}: {e}"[:120]
        try:
            _, m, eopf_df = read_eopf_optimised(item, px)
            rec.update({f"eopf_opt_{k}": v for k, v in m.items()})
        except Exception as e:  # noqa: BLE001
            rec["eopf_opt_error"] = f"{type(e).__name__}: {e}"[:120]
            eopf_df = None

        if cdse_df is not None and eopf_df is not None and len(cdse_df):
            j = cdse_df.merge(eopf_df, on="pix_id", suffixes=("_cdse", "_eopf"))
            # Compare only pixels both sides consider real data.
            keep = j[[f"{b}_cdse" for b in config.BANDS]].gt(0).all(axis=1)
            j = j[keep]
            for b in config.BANDS:
                d = j[f"{b}_cdse"].astype("int64") - j[f"{b}_eopf"].astype("int64")
                diffs.append(
                    {
                        "scene": sc["id"],
                        "band": b,
                        "n": int(len(d)),
                        "exact_match": round(float((d == 0).mean()), 6),
                        "max_abs_diff": int(d.abs().max()) if len(d) else 0,
                    }
                )
            diffs.append(
                {
                    "scene": sc["id"],
                    "band": "SCL",
                    "n": int(len(j)),
                    "exact_match": round(
                        float((j["scl_cdse"] == j["scl_eopf"]).mean()), 6
                    ),
                    "max_abs_diff": 0,
                }
            )
        rows.append(rec)
        if progress:
            progress(rec)
        used += 1
        if used >= n_scenes:
            break
    return pd.DataFrame(rows), pd.DataFrame(diffs)


# ═══════════════════════════════════════════════════════════════════════════
# Where you talk to CDSE from
# ═══════════════════════════════════════════════════════════════════════════
# CDSE documents S3 as its high-performance parallel-access interface and offers
# a region-specific Amsterdam endpoint alongside the default. They are not the
# same service: the default resolves to a single address, the Amsterdam one to a
# ~20-address pool, which is exactly the difference that shows up under the
# concurrency a backfill runs at. Worth measuring rather than assuming, since
# picking the wrong one costs hours across 15k scenes.
CDSE_ENDPOINTS = {
    "default": "https://eodata.dataspace.copernicus.eu",
    "amsterdam": "https://eodata.ams.dataspace.copernicus.eu",
}


def endpoint_sweep(
    aoi_name: str,
    n_scenes: int = 6,
    workers: int = 2,
    endpoints: dict | None = None,
    progress=None,
) -> pd.DataFrame:
    """Same scenes, same concurrency, different S3 endpoint.

    Measures both latency and throughput, because they can disagree: a
    load-balanced pool can be slower for one scene and faster for twenty.
    """
    import os as _os

    endpoints = endpoints or CDSE_ENDPOINTS
    grid = aoi_mod.load_grid(aoi_name)
    cat = catalog.dedupe_versions(catalog.load(aoi_name)).sort_values("datetime")
    scenes = []
    for _, sc in cat.iloc[::-1].iterrows():
        if len(reader.pixels_for_scene(grid, sc)) >= 500:
            scenes.append(sc)
        if len(scenes) >= n_scenes:
            break

    original = config.S3_ENDPOINT
    out = []
    try:
        for name, url in endpoints.items():
            config.S3_ENDPOINT = url
            _os.environ["AWS_S3_ENDPOINT"] = url.replace("https://", "")
            # One warm-up scene: the first request to a new endpoint pays DNS and
            # TLS setup that no later scene does, and charging that to the
            # endpoint would be measuring our own cold start.
            try:
                reader.read_scene(scenes[0], reader.pixels_for_scene(grid, scenes[0]))
            except Exception:  # noqa: BLE001
                pass

            per_scene = []
            for sc in scenes:
                t0 = time.time()
                try:
                    reader.read_scene(sc, reader.pixels_for_scene(grid, sc))
                    per_scene.append(time.time() - t0)
                except Exception as e:  # noqa: BLE001
                    per_scene.append(np.nan)
                    if progress:
                        progress({"endpoint": name, "error": str(e)[:120]})

            t0 = time.time()
            ok = 0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [
                    pool.submit(read_cdse, sc, reader.pixels_for_scene(grid, sc))
                    for sc in scenes
                ]
                for f in as_completed(futs):
                    try:
                        f.result()
                        ok += 1
                    except Exception:  # noqa: BLE001
                        pass
            el = time.time() - t0
            rec = {
                "endpoint": name,
                "url": url,
                "n_scenes": len(scenes),
                "median_scene_s": round(float(np.nanmedian(per_scene)), 2),
                "mean_scene_s": round(float(np.nanmean(per_scene)), 2),
                "n_failed": int(np.isnan(per_scene).sum()),
                "workers": workers,
                "concurrent_ok": ok,
                "concurrent_s": round(el, 1),
                "scenes_per_min": round(len(scenes) / el * 60, 1),
            }
            out.append(rec)
            if progress:
                progress(rec)
    finally:
        config.S3_ENDPOINT = original
        _os.environ["AWS_S3_ENDPOINT"] = original.replace("https://", "")
    return pd.DataFrame(out)


# ═══════════════════════════════════════════════════════════════════════════
# Transport tuning: can the JP2 path be made faster without changing what it reads?
# ═══════════════════════════════════════════════════════════════════════════
# The cost of a scene is three things — 7 opens, ~63 block range-GETs, ~90 MB of
# JPEG 2000 decode (see config.GDAL_OPTS for the measurement). Each preset below
# attacks one of them. The production default was selected with this harness on
# 2026-08-19; results and the unique-scene confirmation are in the design doc.
GDAL_PRESETS: dict[str, dict] = {
    # What ships today (currently GDAL_NUM_THREADS=2 via config.GDAL_OPTS).
    "baseline": {},
    # (b) request count: enable HTTP/2. MULTIPLEX already defaults to YES, but is
    # inert on the usual HTTP/1.1 connection and outside ReadMultiRange calls.
    "http2": {"GDAL_HTTP_VERSION": "2", "GDAL_HTTP_MULTIPLEX": "YES"},
    # Negative control: merging already defaults to YES. Turning it off tells us
    # whether this reader/JP2 path emits a ReadMultiRange call on which the option
    # can act; explicitly setting YES would be identical to the baseline.
    "no_merge": {"GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "NO"},
    # (c) decode: bounded cases matter because seven bands already run in
    # parallel. ALL_CPUS in every worker can oversubscribe a many-core machine.
    "decode_threads_1": {"GDAL_NUM_THREADS": "1"},
    "decode_threads_2": {"GDAL_NUM_THREADS": "2"},
    "decode_threads_4": {"GDAL_NUM_THREADS": "4"},
    "decode_threads": {"GDAL_NUM_THREADS": "ALL_CPUS"},
    # (b) re-reads: VSI_CACHE_SIZE is compressed input bytes and is per file.
    # This case can prove whether the JP2 driver revisits ranges, but may reserve
    # up to 7 x 256 MB while all bands are open.
    "big_cache": {"VSI_CACHE_SIZE": "268435456"},
    "tuned": {"GDAL_NUM_THREADS": "2", "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES"},
}


def _same_scene_values(
    baseline: pd.DataFrame, candidate: pd.DataFrame
) -> tuple[bool, int]:
    """Exact transport parity, including row identity and missing rows."""
    cols = ["pix_id", *config.BANDS, "scl", "nodata", "clear"]
    b = baseline[cols].sort_values("pix_id").reset_index(drop=True)
    t = candidate[cols].sort_values("pix_id").reset_index(drop=True)
    if len(b) != len(t):
        return False, abs(len(b) - len(t))
    unequal = (b != t).to_numpy()
    mismatch = int(unequal.sum())
    return mismatch == 0, mismatch


def gdal_option_sweep(
    aoi_name: str,
    n_scenes: int = 4,
    repeats: int = 2,
    presets: dict | None = None,
    progress=None,
) -> pd.DataFrame:
    """Time `reader.read_scene` under different GDAL transport settings.

    Two things this does that a naive sweep would not, both learned the hard way
    in this project:

    * **Balances preset position across scenes and repeats.** The EOPF comparison
      in §4.1 of the design doc showed a 2.5x "win" that was entirely warm cache
      from the preceding method. Preset order is rotated for each scene and
      reversed on alternate repeats so each candidate occupies every position,
      rather than merely swapping which candidate is first and last.
    * **Checks the values, not just the clock.** A transport change that returns
      different numbers is not a faster reader, it is a different dataset. Every
      preset is compared cell-for-cell against the baseline, and a preset that
      disagrees is reported as failed however fast it was.

    Run it on a machine that is not simultaneously backfilling: the two contend
    for the same connection budget and neither measurement survives it.
    """
    presets = presets or GDAL_PRESETS
    grid = aoi_mod.load_grid(aoi_name)
    cat = catalog.dedupe_versions(catalog.load(aoi_name)).sort_values("datetime")
    scenes = []
    for _, sc in cat.iloc[::-1].iterrows():
        if len(reader.pixels_for_scene(grid, sc)) >= 500:
            scenes.append(sc)
        if len(scenes) >= n_scenes:
            break

    baseline_vals: dict[str, pd.DataFrame] = {}
    outputs: dict[tuple[int, str, str], pd.DataFrame] = {}
    rows = []
    names = list(presets)
    for rep in range(repeats):
        base_order = names if rep % 2 == 0 else list(reversed(names))
        for scene_pos, sc in enumerate(scenes):
            # Across enough scene/repeat combinations, every preset occupies
            # every ordinal position equally often. This balances both cold
            # cache and periodic endpoint slowdowns observed in the first run.
            shift = (rep * len(scenes) + scene_pos) % len(names)
            order = base_order[shift:] + base_order[:shift]
            for order_pos, name in enumerate(order):
                px = reader.pixels_for_scene(grid, sc)
                t0 = time.time()
                try:
                    # Pass overrides into each band worker: Rasterio/GDAL config
                    # is thread-local, so an Env around read_scene in this main
                    # thread would not configure the actual reads.
                    df = reader.read_scene(
                        sc,
                        px,
                        apply_offset=False,
                        scl_first=False,
                        gdal_overrides=presets[name],
                    )
                    el = time.time() - t0
                    ok, mismatch = True, 0
                    if name == "baseline":
                        if sc["id"] in baseline_vals:
                            ok, mismatch = _same_scene_values(
                                baseline_vals[sc["id"]], df
                            )
                        else:
                            baseline_vals[sc["id"]] = df
                    outputs[(rep, name, sc["id"])] = df
                    if name != "baseline" and sc["id"] in baseline_vals:
                        ok, mismatch = _same_scene_values(baseline_vals[sc["id"]], df)
                    rows.append(
                        {
                            "preset": name,
                            "rep": rep,
                            "scene": sc["id"],
                            "order_pos": order_pos,
                            "n_pixels": int(len(px)),
                            "seconds": round(el, 2),
                            "values_match": ok,
                            "n_mismatched": mismatch,
                        }
                    )
                except Exception as e:  # noqa: BLE001
                    rows.append(
                        {
                            "preset": name,
                            "rep": rep,
                            "scene": sc["id"],
                            "order_pos": order_pos,
                            "n_pixels": int(len(px)),
                            "seconds": None,
                            "values_match": False,
                            "error": f"{type(e).__name__}: {e}"[:120],
                        }
                    )
                if progress:
                    progress(rows[-1])

    # Some balanced orders encounter a candidate before that scene's baseline.
    # Resolve parity after all timed reads so correctness never constrains order.
    for row in rows:
        key = (row["rep"], row["preset"], row["scene"])
        if row["seconds"] is None or row["preset"] == "baseline":
            continue
        if row["scene"] not in baseline_vals:
            row["values_match"] = False
            row["error"] = "no successful baseline read for scene"
            continue
        row["values_match"], row["n_mismatched"] = _same_scene_values(
            baseline_vals[row["scene"]], outputs[key]
        )
    return pd.DataFrame(rows)


def summarise_gdal_sweep(df: pd.DataFrame) -> pd.DataFrame:
    """Median seconds per preset, with the value check folded in.

    Reported against the baseline as a speedup, because the absolute numbers
    move with the network and only the ratio is portable between machines.
    """
    ok = df[df["seconds"].notna()]
    g = (
        ok.groupby("preset")
        .agg(
            n=("seconds", "size"),
            median_s=("seconds", "median"),
            mean_s=("seconds", "mean"),
            values_match=("values_match", "all"),
            mismatched=("n_mismatched", "sum"),
        )
        .round(2)
    )
    if "baseline" in g.index:
        g["speedup_vs_baseline"] = (
            g.loc["baseline", "median_s"] / g["median_s"]
        ).round(2)
    return g.sort_values("median_s")
