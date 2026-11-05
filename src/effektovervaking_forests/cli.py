"""`forestmon` — the operational CLI.

Command groups follow the pipeline stages, in run order:

    forestmon grid build kalklindeskog        # the pixels we monitor
    forestmon catalog sync kalklindeskog      # what imagery exists
    forestmon catalog schedule kalklindeskog  # when the next scene lands
    forestmon obs backfill kalklindeskog      # history -> observation store
    forestmon sccd bootstrap kalklindeskog    # fit initial per-pixel state
    forestmon run kalklindeskog               # the operational cycle

Everything is per-AOI and idempotent; `run` is the one a timer calls.
`forestmon condition ...` is the separate annual condition pipeline
(docs/condition/README.md); it shares only the configuration.
"""

from __future__ import annotations

import json
import logging
import shutil

import numpy as np
import pandas as pd
import typer

from . import alerts as alerts_mod
from . import aoi as aoi_mod
from . import (
    catalog,
    cdse_auth,
    config,
    obs,
    obslog,
    pipeline,
    reader,
    runs,
    store,
)
from . import confidence as conf_mod
from . import publish as publish_mod
from . import sccd as sccd_mod
from . import state as state_mod

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Near-real-time forest disturbance monitoring from CDSE.",
)
grid_app = typer.Typer(no_args_is_help=True, help="Pixel grid the state is carried on.")
cat_app = typer.Typer(no_args_is_help=True, help="CDSE STAC scene index and schedule.")
obs_app = typer.Typer(no_args_is_help=True, help="Clear-sky observation store.")
sccd_app = typer.Typer(no_args_is_help=True, help="S-CCD fitting and online update.")
alerts_app = typer.Typer(no_args_is_help=True, help="Alerts and their review status.")
app.add_typer(grid_app, name="grid")
app.add_typer(cat_app, name="catalog")
app.add_typer(obs_app, name="obs")
app.add_typer(sccd_app, name="sccd")
app.add_typer(alerts_app, name="alerts")
publish_app = typer.Typer(
    no_args_is_help=True, help="Build the public site's data artefacts."
)
app.add_typer(publish_app, name="publish")
condition_app = typer.Typer(
    no_args_is_help=True,
    help="Stand condition and trajectory from AlphaEarth embeddings (annual).",
)
app.add_typer(condition_app, name="condition")


@app.callback()
def main(
    debug: bool = typer.Option(False, envvar="DEBUG", help="Enable debug logging."),
):
    """Configure logging and make sure the data tree exists."""
    logging.basicConfig(level=logging.DEBUG if debug else logging.WARNING)
    config.ensure_dirs()


AOI_ARG = typer.Argument(..., help="AOI name, e.g. kalklindeskog")


def _echo(msg: str, err: bool = False):
    typer.echo(msg, err=err)


def _fail(msg: str):
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _crs_summary(df: pd.DataFrame) -> str:
    """'EPSG:32632' for one zone; 'EPSG:32632 (102,021 px), ...' for several."""
    n = df.groupby("utm_crs").size()
    if len(n) == 1:
        return str(n.index[0])
    return ", ".join(f"{c} ({k:,} px)" for c, k in n.items())


# ── grid ─────────────────────────────────────────────────────────────────────
def _tile_bounds(aoi: str) -> dict:
    """{tile: (utm bounds, crs)} per AOI tile; empty when no catalogue yet."""
    import rasterio

    from . import reader as _reader

    cat = catalog.dedupe_versions(catalog.load(aoi))
    if cat.empty:
        return {}
    bounds = {}
    with _reader._gdal_env():
        for tile in catalog.resolve_tiles(aoi):
            sc = cat[cat["tile"] == tile]
            if sc.empty:
                continue
            href = sc.iloc[-1]["assets"][config.ASSET_FOR_BAND["B02"]]
            with rasterio.open(_reader._vsis3(href)) as ds:
                bounds[tile] = (tuple(ds.bounds), ds.crs.to_string())
            _echo(f"  {tile}: {bounds[tile][1]}")
    return bounds


@grid_app.command("build")
def grid_build(aoi: str = AOI_ARG):
    """Rasterise the AOI polygons onto the Sentinel-2 10 m lattice.

    Re-run after `catalog sync` so tile extents can refine each polygon's zone.
    """
    tb = _tile_bounds(aoi)
    if not tb:
        _echo("no catalogue yet: zones from MGRS alone; rebuild after `catalog sync`")
    df = aoi_mod.build_grid(aoi, tb or None)
    aoi_mod.save_grid(aoi, df)
    _echo(
        f"{len(df):,} pixels  ({len(df) * 100 / 1e4:,.1f} ha)  "
        f"crs={_crs_summary(df)}  polygons={df.poly_idx.nunique()}"
    )
    _echo(f"fingerprint {aoi_mod.grid_fingerprint(df)}  ->  {aoi_mod.grid_path(aoi)}")


@grid_app.command("assign-tiles")
def grid_assign_tiles(aoi: str = AOI_ARG):
    """Pin every pixel to one MGRS tile (its most interior covering tile).

    Run after `grid build` and `catalog sync`.
    """
    bounds = _tile_bounds(aoi)
    if not bounds:
        _fail(f"no catalogue for {aoi}; run `catalog sync {aoi}` first")
    df = aoi_mod.assign_home_tiles(aoi, bounds)
    n_unassigned = int((df["home_tile"] == "").sum())
    _echo(
        f"\n{len(df):,} pixels pinned across {df.home_tile.nunique()} tiles"
        + (
            f"; {n_unassigned:,} UNASSIGNED — rebuild the grid (`grid build`) "
            "now the catalogue exists, then re-run this"
            if n_unassigned
            else ""
        )
    )
    _echo(df.groupby("home_tile").size().rename("pixels").to_string())


@grid_app.command("show")
def grid_show(aoi: str = AOI_ARG):
    """Summarise an existing grid."""
    df = aoi_mod.load_grid(aoi)
    _echo(
        f"{len(df):,} pixels across {df.poly_idx.nunique()} polygons, "
        f"crs={_crs_summary(df)}, fingerprint {aoi_mod.grid_fingerprint(df)}"
    )


# ── catalog ──────────────────────────────────────────────────────────────────
@cat_app.command("sync")
def catalog_sync(
    aoi: str = AOI_ARG,
    start: str = typer.Option(None, help="ISO date; default incremental"),
    end: str = typer.Option(None, help="ISO date; default now"),
):
    """Refresh the CDSE STAC scene index for this AOI."""
    df = catalog.sync(
        aoi,
        start=start,
        end=end,
        progress=lambda tile, n: _echo(f"  {tile}: {n} items"),
    )
    _echo(
        f"{len(df):,} scenes indexed  "
        f"({pd.to_datetime(df.datetime).min():%Y-%m-%d} -> "
        f"{pd.to_datetime(df.datetime).max():%Y-%m-%d})"
    )


@cat_app.command("tiles")
def catalog_tiles(
    aoi: str = AOI_ARG, refresh: bool = typer.Option(False, help="Re-resolve from STAC")
):
    """Which MGRS tiles actually overlap the AOI polygons."""
    _echo(", ".join(catalog.resolve_tiles(aoi, refresh=refresh)))


@cat_app.command("reconcile")
def cat_reconcile(
    aoi: str = AOI_ARG, days: int = typer.Option(30, help="Publication-time lookback")
):
    """Sweep for scenes published late or reprocessed under a new baseline.

    Run weekly; `sync` cannot see these.
    """
    res = catalog.reconcile(
        aoi, days=days, progress=lambda t, n: _echo(f"  {t}: {n} items")
    )
    _echo(json.dumps(res, indent=1, default=str))


@cat_app.command("schedule")
def catalog_schedule(
    aoi: str = AOI_ARG, days: int = typer.Option(10, help="Horizon in days")
):
    """When the next acquisitions happen, and when they should be readable."""
    s = catalog.schedule(aoi, horizon_days=days)
    if s.empty:
        _fail("no active acquisition tracks; is the catalogue synced?")
    df = catalog.load(aoi)
    lag = catalog.publication_lag(df)
    _echo(
        f"publication lag: median {lag.median():.1f} h, "
        f"p90 {lag.quantile(0.9):.1f} h  (n={len(lag):,})"
    )
    for _, r in s.head(20).iterrows():
        _echo(
            f"  {r.expected_acquisition:%Y-%m-%d %H:%M} UTC  {r.tile}  "
            f"orbit {r.rel_orbit:>3}  {r.platform}  "
            f"-> readable ~{r.expected_available:%Y-%m-%d %H:%M}"
        )
    nxt = catalog.next_availability(aoi)
    _echo(f"next data expected readable: {nxt:%Y-%m-%d %H:%M} UTC")


# ── obs ──────────────────────────────────────────────────────────────────────
@obs_app.command("backfill")
def obs_backfill(
    aoi: str = AOI_ARG,
    start: str = typer.Option(config.HISTORY_START),
    end: str = typer.Option(None),
    workers: int = typer.Option(
        config.SCENE_WORKERS,
        help="Concurrent scene reads (x7 band threads = connections)",
    ),
    batch: int = typer.Option(config.INGEST_BATCH, help="Scenes per commit"),
    limit: int = typer.Option(None, help="Stop after N scenes"),
    force_lock: bool = typer.Option(
        False, "--force-lock", help="Take over a stale AOI lease"
    ),
):
    """Read historical scenes into the observation store.

    Resumable: each batch commits its own part file and ledger rows.
    """
    if not cdse_auth.have_credentials():
        _fail("CDSE S3 credentials missing — run `forestmon doctor`")
    grid = aoi_mod.load_grid(aoi)
    todo = pipeline.pending_scenes(aoi, limit=limit, since=start)
    if end:
        todo = todo[
            pd.to_datetime(todo["datetime"], utc=True) <= pd.Timestamp(end, tz="UTC")
        ]
    if todo.empty:
        _echo("nothing pending")
        raise typer.Exit()
    _echo(
        f"{len(todo):,} scenes pending, {len(grid):,} pixels, "
        f"{workers} workers x {reader.BAND_WORKERS} bands, batches of {batch}"
    )

    def prog(i, n, row):
        if i % 25 == 0 or row["status"] != "ok":
            _echo(
                f"  [{i}/{n}] {row['scene_id'][:44]} {row['status']} "
                f"clear={row['n_clear']} {row['seconds']}s "
                f"{row['message'][:60]}"
            )

    try:
        with obslog.run_logger(
            aoi, "backfill", workers=workers, n_scenes=len(todo), batch=batch
        ) as log:
            with store.lease(aoi, what="backfill", force=force_lock):
                got = pipeline.ingest(
                    aoi,
                    todo,
                    grid,
                    workers=workers,
                    batch_size=batch,
                    progress=prog,
                    log=log,
                )
    except store.LockBusy as e:
        _fail(str(e))
    _echo(
        f"stored {got['n_rows']:,} observations ({got['n_clear']:,} clear) "
        f"from {got['n_ok']:,} scenes in {got['n_batches']} batches "
        f"({got['n_error']:,} failed)"
    )
    _echo(f"run `forestmon obs compact {aoi}` to fold part files into yearly parquet")


@obs_app.command("compact")
def obs_compact(
    aoi: str = AOI_ARG, year: int = typer.Option(None, help="Only this year")
):
    """Fold immutable part files into one sorted parquet per year."""
    out = obs.compact(aoi, years=[year] if year else None, progress=_echo)
    if not out:
        _echo("no part files to compact")
    else:
        _echo(f"compacted {len(out)} year(s), {sum(out.values()):,} rows total")


@obs_app.command("status")
def obs_status(aoi: str = AOI_ARG):
    """Observation store and run-ledger summary."""
    yrs = obs.years_present(aoi)
    if not yrs:
        _echo("observation store is empty")
    for y in yrs:
        df = obs.load(aoi, years=[y], columns=["pix_id", "scene_id"], clear_only=False)
        parts = len(obs.parts_for(aoi, y))
        _echo(
            f"  {y}: {len(df):>9,} obs  {df.pix_id.nunique():>7,} pixels  "
            f"{df.scene_id.nunique():>5,} scenes"
            + (f"  ({parts} uncompacted parts)" if parts else "")
        )
    led = runs.load(aoi)
    if len(led):
        by = led["status"].value_counts().to_dict()
        _echo(f"ledger: {len(led):,} scenes {by}")
        unapplied = runs.unapplied_scene_ids(aoi)
        if unapplied:
            _echo(
                f"  {len(unapplied):,} scenes stored but NOT yet applied to state "
                "— the next `run` folds them in from disk"
            )
        cat_n = len(catalog.load(aoi))
        _echo(f"catalogue: {cat_n:,} scenes -> {cat_n - len(led):,} not yet processed")


# ── sccd ─────────────────────────────────────────────────────────────────────
@sccd_app.command("bootstrap")
def sccd_bootstrap(
    aoi: str = AOI_ARG,
    chunk_pixels: int = typer.Option(
        12_000, help="Pixels per fitting chunk (bounds memory)"
    ),
):
    """Fit initial per-pixel S-CCD state from the observation store."""

    grid = aoi_mod.load_grid(aoi)
    # Fit in pixel chunks to bound memory; pixels are independent.
    ids = np.sort(grid["pix_id"].to_numpy())
    chunks = np.array_split(ids, max(1, int(np.ceil(len(ids) / chunk_pixels))))
    states, breaks, n_obs, last_obs = {}, [], 0, None
    stats = {"n_fitted": 0, "n_skipped": 0, "n_breaks": 0, "min_obs": None}
    for k, ch in enumerate(chunks, 1):
        observations = obs.load(aoi, pix_range=(ch[0], ch[-1]))
        if observations.empty:
            stats["n_skipped"] += len(ch)
            continue
        n_obs += len(observations)
        last_obs = (
            max(last_obs, observations["datetime"].max())
            if last_obs is not None
            else observations["datetime"].max()
        )
        _echo(
            f"chunk {k}/{len(chunks)}: fitting "
            f"{observations.pix_id.nunique():,} pixels "
            f"from {len(observations):,} observations..."
        )
        s_, b_, st = sccd_mod.bootstrap(observations)
        states.update(s_)
        breaks.extend(b_)
        for key in ("n_fitted", "n_skipped", "n_breaks"):
            stats[key] += st[key]
        stats["min_obs"] = st["min_obs"]
        del observations, s_, b_
    if not states:
        _fail(f"observation store is empty; run `obs backfill {aoi}` first")
    stats["n_skipped"] += len(grid) - stats["n_fitted"] - stats["n_skipped"]
    meta = {
        "grid_fingerprint": aoi_mod.grid_fingerprint(grid),
        "last_observation": str(last_obs),
        "bootstrap": stats,
        "monitoring_start": config.MONITORING_START,
        "alerts_dirty": True,
    }
    state_mod.save(aoi, states, meta)
    # Mark every stored scene as applied so the first `run` does not replay
    # them through sccd_update.
    n_marked = runs.mark_applied(aoi, runs.processed_ids(aoi))
    alerts_mod.record(aoi, breaks, grid)
    meta["alerts_dirty"] = False
    state_mod.save_meta(aoi, meta)

    tbl = alerts_mod.load(aoi)
    n_hist = int((tbl["status"] == "historical").sum()) if len(tbl) else 0
    _echo(
        f"fitted {stats['n_fitted']:,} pixels "
        f"(skipped {stats['n_skipped']:,} with <{stats['min_obs']} clear obs — "
        "these are refit automatically once they cross the threshold), "
        f"{stats['n_breaks']:,} breaks"
    )
    _echo(
        f"{n_hist:,} breaks are dated before {config.MONITORING_START} and are "
        "recorded as 'historical', not as alerts"
    )
    _echo(f"{n_marked:,} ledger scenes marked state_applied")
    _echo(f"state -> {state_mod.state_path(aoi)}")


@sccd_app.command("update")
def sccd_update_cmd(
    aoi: str = AOI_ARG,
    workers: int = typer.Option(config.SCENE_WORKERS),
    limit: int = typer.Option(None, help="Max scenes this run"),
    no_sync: bool = typer.Option(False, help="Skip catalogue sync"),
):
    """Ingest pending scenes and advance S-CCD state with them."""
    res = pipeline.run_update(aoi, workers=workers, limit=limit, sync=not no_sync)
    _echo(json.dumps(res, indent=1, default=str))


@sccd_app.command("status")
def sccd_status(aoi: str = AOI_ARG):
    """What state exists, and how current it is."""
    if not state_mod.exists(aoi):
        _echo("no state fitted")
        raise typer.Exit()
    _, meta = state_mod.load(aoi)
    _echo(json.dumps(meta, indent=1, default=str))
    last = state_mod.last_observation_date(aoi)
    if last is not None:
        age = (pd.Timestamp.now(tz="UTC") - last).days
        _echo(f"newest observation in state is {age} days old")


# ── alerts ───────────────────────────────────────────────────────────────────
@alerts_app.command("list")
def alerts_list(
    aoi: str = AOI_ARG,
    status: str = typer.Option("new", help="Filter, or 'all'"),
    limit: int = typer.Option(30),
):
    """Show alerts, newest break first."""
    df = alerts_mod.load(aoi)
    if df.empty:
        _echo("no alerts")
        raise typer.Exit()
    if status != "all":
        df = df[df["status"] == status]
    df = df.sort_values("break_date", ascending=False).head(limit)
    for _, r in df.iterrows():
        _echo(
            f"  {r.break_date}  poly {r.poly_id}  {r.lon:.5f},{r.lat:.5f}  "
            f"{r.kind} {r.magnitude:+.3f}  [{r.status}]"
        )


@alerts_app.command("summary")
def alerts_summary(aoi: str = AOI_ARG):
    """Counts by status and kind."""
    s = alerts_mod.summary(aoi)
    _echo("no alerts" if s.empty else s.to_string(index=False))


@alerts_app.command("rescore")
def alerts_rescore(
    aoi: str = AOI_ARG,
    no_agreement: bool = typer.Option(
        False, "--no-agreement", help="Skip the OPERA DIST-ALERT cross-check."
    ),
):
    """Recompute every alert's confidence tier.

    Confidence grows as observations accumulate after the break, so this runs
    in the operational loop, not only at write time.
    """
    df = alerts_mod.rescore(aoi, use_agreement=not no_agreement)
    if df.empty:
        _echo("no alerts")
        return
    _echo(conf_mod.summary(df[df["kind"] == "loss"]).to_string())


# ── publish ──────────────────────────────────────────────────────────────────
@publish_app.command("site")
def publish_site(
    aoi: list[str] = typer.Argument(None, help="AOIs; default all with alerts."),
    out: str = typer.Option(str(publish_mod.WEB_OUT), help="Web payload directory."),
    gis: str = typer.Option(str(publish_mod.GIS_OUT), help="GIS artefact directory."),
    no_raster: bool = typer.Option(False, "--no-raster"),
    no_rescore: bool = typer.Option(
        False,
        "--no-rescore",
        help="Publish tiers as they stand instead of recomputing first.",
    ),
):
    """Build PMTiles, the queue index, the GIS artefacts and the manifest.

    Rescores first by default, since reviewers work the queue in tier order.
    """
    names = (
        list(aoi)
        if aoi
        else [a for a in config.AOI_PARQUET if alerts_mod.alerts_path(a).exists()]
    )
    if not names:
        _fail("no AOI has an alert table yet")
    # Check up front, not as a FileNotFoundError halfway through the first AOI.
    if not shutil.which(publish_mod._gdal("ogr2ogr")):
        _fail(
            "ogr2ogr (GDAL >= 3.8) not found; put it on PATH or set FORESTMON_GDAL_BIN"
        )

    results = []
    for name in names:
        if not no_rescore:
            alerts_mod.rescore(name)
        r = publish_mod.publish(
            name, out_dir=out, gis_dir=gis, write_raster=not no_raster
        )
        results.append(r)
        _echo(
            f"{name}: {r.get('n_loss_pixels', 0)} loss pixels, "
            f"{r.get('n_gain_pixels', 0)} gain pixels "
            f"-> {r['outputs'].get('loss', {}).get('pmtiles', '(none)')}"
        )
    m = publish_mod.write_manifest(results, out_dir=out)
    _echo(f"manifest: {m}")


# ── observability ────────────────────────────────────────────────────────────
@app.command("health")
def health_cmd(
    aoi: str = AOI_ARG,
    as_json: bool = typer.Option(False, "--json", help="Machine-readable"),
    strict: bool = typer.Option(
        False, help="Exit non-zero when degraded, not only unhealthy"
    ),
):
    """How far behind is the system, and is anything stuck?

    Computed from artefacts on disk, not the log. Exit code: 0 healthy,
    1 degraded (with --strict), 2 unhealthy.
    """
    h = obslog.health(aoi)
    if as_json:
        _echo(json.dumps(h, indent=1, default=str))
    else:
        colour = {
            "healthy": typer.colors.GREEN,
            "degraded": typer.colors.YELLOW,
            "unhealthy": typer.colors.RED,
        }[h["status"]]
        typer.secho(f"{aoi}: {h['status'].upper()}", fg=colour, bold=True)
        for k in (
            "backlog_scenes",
            "backlog_age_days",
            "catalogue_age_days",
            "state_age_days",
            "n_stored_not_applied",
            "n_uncompacted_parts",
            "recent_error_rate",
            "scene_seconds_median",
            "scene_seconds_p95",
            "n_alerts_awaiting_review",
            "hours_since_last_run",
        ):
            if k in h:
                _echo(f"  {k:<28} {h[k]}")
        if h.get("alerts_by_status"):
            _echo(f"  {'alerts_by_status':<28} {h['alerts_by_status']}")
        if h.get("top_errors"):
            _echo("  recent read errors:")
            for msg, n in h["top_errors"].items():
                _echo(f"    {n:>4}x {msg}")
        if h.get("lease"):
            _echo(
                f"  lease: {h['lease']['what']} pid {h['lease']['pid']} on "
                f"{h['lease']['host']}, held {h['lease']['held_hours']} h"
            )
        if h.get("last_run"):
            lr = h["last_run"]
            _echo(
                f"  last run: {lr['command']} {'ok' if lr['ok'] else 'FAILED'} "
                f"at {lr['ts']} ({lr.get('seconds')}s)"
            )
            # A missing error is NaN (truthy), hence the notna check.
            if lr.get("error") and pd.notna(lr["error"]):
                typer.secho(f"    {lr['error']}", fg=typer.colors.RED)
        for w in h["warnings"]:
            typer.secho(f"  warn: {w}", fg=typer.colors.YELLOW)
        for pb in h["problems"]:
            typer.secho(f"  PROBLEM: {pb}", fg=typer.colors.RED)

    code = obslog.health_exit_code(h)
    raise typer.Exit(code=code if (strict or code == 2) else 0)


@app.command("logs")
def logs_cmd(
    aoi: str = AOI_ARG,
    limit: int = typer.Option(40, help="Most recent N events"),
    level: str = typer.Option(None, help="Only info / warn / error"),
    run_id: str = typer.Option(None, help="Only this run"),
    failures: bool = typer.Option(False, help="Only warnings and errors"),
):
    """Recent structured events (JSON Lines under FORESTMON_LOG_DIR)."""
    df = obslog.read_events(aoi, level=level, run_id=run_id)
    if df.empty:
        _echo(f"no events logged for {aoi} yet ({obslog.log_path(aoi)})")
        raise typer.Exit()
    if failures:
        df = df[df["level"].isin(("warn", "error"))]
    df = df.tail(limit)
    for _, r in df.iterrows():
        skip = {
            "ts",
            "run_id",
            "aoi",
            "command",
            "level",
            "event",
            "stage",
            "elapsed_s",
        }
        extra = " ".join(
            f"{k}={v}" for k, v in r.items() if k not in skip and pd.notna(r[k])
        )
        mark = {"error": "!!", "warn": " !"}.get(r["level"], "  ")
        line = f"{mark} {r['ts']:%m-%d %H:%M:%S} {r['run_id']} {r['event']:<26} {extra}"
        typer.secho(
            line[:240],
            fg={"error": typer.colors.RED, "warn": typer.colors.YELLOW}.get(r["level"]),
        )
    _echo(f"\n{obslog.log_path(aoi)}")


# ── the operational entrypoint ───────────────────────────────────────────────
@app.command("run")
def run(
    aoi: str = AOI_ARG,
    workers: int = typer.Option(config.SCENE_WORKERS),
    limit: int = typer.Option(None, help="Max scenes this run"),
    batch: int = typer.Option(config.INGEST_BATCH, help="Scenes per commit"),
    force_lock: bool = typer.Option(
        False, "--force-lock", help="Take over a stale AOI lease"
    ),
    wait_for_schedule: bool = typer.Option(
        False, help="Exit quietly if no new scene is expected yet"
    ),
):
    """One operational cycle: sync, ingest new scenes, advance S-CCD, alert.

    Safe on a frequent timer; --wait-for-schedule skips the catalogue round
    trip when nothing can have landed yet.
    """
    if wait_for_schedule:
        try:
            nxt = catalog.next_availability(aoi)
        except ValueError:
            nxt = None
        pend = len(pipeline.pending_scenes(aoi))
        if pend == 0 and nxt is not None and nxt > pd.Timestamp.now(tz="UTC"):
            _echo(f"nothing pending; next scene readable ~{nxt:%Y-%m-%d %H:%M} UTC")
            raise typer.Exit()

    try:
        res = pipeline.run_update(
            aoi, workers=workers, limit=limit, batch_size=batch, force_lock=force_lock
        )
    except store.LockBusy as e:
        # Not an error: the previous run still holds the lease.
        _echo(str(e))
        raise typer.Exit(code=0) from e
    _echo(json.dumps(res, indent=1, default=str))
    if res.get("n_new_breaks"):
        typer.secho(
            f"{res['n_new_breaks']} new break(s) — `forestmon alerts list {aoi}`",
            fg=typer.colors.YELLOW,
        )


@app.command("doctor")
def doctor():
    """Check that everything this package depends on is actually available."""
    import rasterio

    ok = True
    with rasterio.Env() as e:
        jp2 = "JP2OpenJPEG" in e.drivers()
    _echo(
        f"  {'OK ' if jp2 else 'MISSING'}  GDAL JP2OpenJPEG driver "
        f"{'' if jp2 else '(the PyPI rasterio wheel bundles it)'}"
    )
    ok &= jp2
    _echo(f"  {'OK ' if sccd_mod.HAVE_PYXCCD else 'MISSING'}  pyxccd")
    ok &= sccd_mod.HAVE_PYXCCD
    creds = cdse_auth.have_credentials()
    _echo(
        f"  {'OK ' if creds else 'MISSING'}  CDSE S3 credentials "
        f"({config.S3_KEY_ENV}/{config.S3_SECRET_ENV})"
    )
    ok &= creds
    ogr = publish_mod._gdal("ogr2ogr")
    have_ogr = bool(shutil.which(ogr))
    _echo(
        f"  {'OK ' if have_ogr else 'WARN'}  ogr2ogr "
        f"{'' if have_ogr else '(only `publish site` needs it; set FORESTMON_GDAL_BIN)'}"  # noqa: E501
    )
    _echo(f"  data dir: {config.DATA}")
    _echo(
        f"  AOI dir:  {config.AOI_DIR} "
        f"({', '.join(config.AOI_PARQUET) or 'no AOIs found'})"
    )
    for name in config.AOI_PARQUET:
        p = aoi_mod.grid_path(name)
        _echo(f"  {'OK ' if p.exists() else '-  '}  grid: {name}")
    raise typer.Exit(code=0 if ok else 1)


# ── condition (AlphaEarth, annual) ───────────────────────────────────────────
@condition_app.command("steps")
def condition_steps():
    """List the pipeline steps in run order."""
    from .condition.steps import STEPS

    for s in STEPS:
        note = " (needs Earth Engine)" if s.needs_ee else ""
        _echo(f"  {s.id:<4} {s.module:<24} {s.summary}{note}")


@condition_app.command(
    "step",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def condition_step(
    ctx: typer.Context,
    step_id: str = typer.Argument(..., help="Step id, e.g. 04 (see `steps`)."),
):
    """Run one step in-process; any further arguments go to the step
    (`forestmon condition step 04 -- --help` lists them)."""
    from .condition.steps import BY_ID, run_step

    if step_id not in BY_ID:
        _fail(f"unknown step {step_id!r}; one of: {', '.join(BY_ID)}")
    run_step(step_id, list(ctx.args))


@condition_app.command("run")
def condition_run(
    start: str = typer.Option(None, "--from", help="First step id (inclusive)."),
    stop: str = typer.Option(None, "--to", help="Last step id (inclusive)."),
    ee_project: str = typer.Option(
        config.EE_PROJECT,
        help="Earth Engine project for 02e (Hansen). Without it 02e is skipped, "
        "which step 04 only allows if hansen_pixels.parquet already exists.",
    ),
    dry_run: bool = typer.Option(False, help="Print the plan and exit."),
):
    """Run the pipeline, or a --from/--to slice of it, stopping at the first
    failing step. Each step is logged under <FORESTMON_LOG_DIR>/condition/."""
    from .condition import paths
    from .condition.steps import plan, run_pipeline

    try:
        steps, skipped = plan(start, stop, ee_project)
    except ValueError as e:
        _fail(str(e))
    log_dir = config.LOG_DIR / "condition"
    _echo(f"data/outputs : {paths.ROOT}")
    _echo(f"logs         : {log_dir}")
    for s, args in steps:
        _echo(f"  {s.id:<4} {s.module} {' '.join(args)}".rstrip())
    for s in skipped:
        _echo(
            f"  ({s.id} skipped: needs --ee-project/FORESTMON_EE_PROJECT and the "
            "gee extra; trajectory classes will have no stated false-positive rate)"
        )
    if dry_run:
        return
    raise typer.Exit(code=run_pipeline(steps, log_dir, echo=_echo))


@condition_app.command("doctor")
def condition_doctor():
    """Check inputs, optional speed-ups and where the pipeline will write."""
    import importlib.util

    import rasterio

    from .condition import paths

    ok = True
    gpkg = paths.KARTLEGGING_GPKG.exists()
    _echo(
        f"  {'OK ' if gpkg else 'MISSING'}  stand polygons {paths.KARTLEGGING_GPKG}"
        f"{'' if gpkg else ' (set FORESTS_KARTLEGGING_GPKG)'}"
    )
    ok &= gpkg
    with rasterio.Env() as e:
        drivers = e.drivers()
    gtiff = "GTiff" in drivers
    _echo(f"  {'OK ' if gtiff else 'MISSING'}  GDAL GTiff driver (mirror COGs)")
    ok &= gtiff
    have_ee = importlib.util.find_spec("ee") is not None
    note = "" if have_ee else " (step 02e only; `--extra gee`)"
    _echo(f"  {'OK ' if have_ee else 'WARN'}  ee{note}")
    _echo(f"  root:        {paths.ROOT}")
    for name, p in (("polygons", paths.POLYGONS), ("condition", paths.CONDITION)):
        _echo(f"  {'OK ' if p.exists() else '-  '}  {name}: {p}")
    raise typer.Exit(code=0 if ok else 1)


if __name__ == "__main__":
    app()
