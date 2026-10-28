# olivinskog — AOI profile (multi-UTM-zone)

Profile for `olivinskog`, the multi-UTM-zone area of interest of the `forestmon` S-CCD monitor.

`olivinskog` is processed through the same CLI as `kalklindeskog`. The AOI identifier corresponds to the GeoParquet file stem under `FORESTMON_AOI_DIR` (`olivinskog.parquet`; the legacy name `olivinskog_union.parquet` is also accepted).

## Profile

| Attribute | Value |
| --- | --- |
| Polygons | 104 |
| Fixed 10 m pixels | 130,539 |
| Pixels in EPSG:32632 (UTM zone 32N) | 126,610 |
| Pixels in EPSG:32634 (UTM zone 34N) | 2,628 |
| Pixels in EPSG:32633 (UTM zone 33N) | 1,225 |
| Pixels in EPSG:32635 (UTM zone 35N) | 76 |
| Home MGRS tiles | 20 |
| Catalogue scenes (2017-01-03 to 2026-10-27) | 61,276 |
| Observation store rows (2017–2026) | 117,382,672 (1.2 GB parquet, yearly files) |
| Observation store size comparison | 1.2 GB vs kalklindeskog 360 MB |
| S-CCD state pickle size | 173 MB |
| Parity vs Earth Engine (2026-06-25..2026-10-23 sample) | 504 matched observations, 100% exact match on every band |
| Alerts by status: `below_gate` | 210,535 |
| Alerts by status: `historical` | 21,765 |
| Alerts by status: `new` | 2,687 |

## Why it differs from a single-zone AOI

Single-zone AOIs like `kalklindeskog` reside wholly within UTM zone 32N (EPSG:32632). `olivinskog` spans four UTM zones across Norway: EPSG:32632, EPSG:32633, EPSG:32634, and EPSG:32635.

* **One polygon, one CRS; no warping.** Reprojecting or warping satellite imagery resamples pixel values and risks step changes in radiometric time series. To prevent this, each polygon is gridded on the native Sentinel-2 10 m UTM lattice Sentinel-2 uses to tile it (`aoi.polygon_crs`). Polygons are gridded as whole units so an individual stand is never split across two coordinate systems.
* **MGRS zone conventions.** UTM zones are nominally 6° wide (`floor((lon + 180) / 6) + 1`), but MGRS widens zone 32V to 3–12°E between 56°N and 64°N, and Sentinel-2 tiling follows this convention. Western Norway stands around 5°E are tiled in zone 32 (such as 32VKL and 32VKN) rather than zone 31.
* **North of 64°N tile reach (re-run `grid build` after `catalog sync`).** MGRS layout north of 64°N has no zone-33 Sentinel-2 tiles at 12.6–12.9°E. Stands located there are covered only by the eastern overhang of zone-32 tiles (32WPS/32WPT). If assigned strictly by theoretical longitude, those stands would be assigned to zone 33 where no Sentinel-2 products exist to observe them. When tile bounds are available, `polygon_crs` checks whether any tile in the polygon's theoretical zone actually contains it; if not, it assigns the zone of the tile in which the polygon sits most interior. For this reason, `grid build` must be re-run after `catalog sync`.
* **`pix_id` embeds EPSG code.** Every AOI's `pix_id` embeds the EPSG code, which is what keeps IDs unique across zones in a multi-zone AOI like this one: `f"{crs.split(':')[1]}_{gx}_{gy}"` (e.g. `32632_...`, `32633_...`). This ensures pixel IDs remain globally unique across zones while staying pinned to integer 10 m grid lattice boundaries.
* **Per-zone COG output.** A single raster cannot combine multiple UTM lattices without warping. In `publish.py`, single-zone AOIs emit `alerts_<aoi>.tif`, whereas multi-zone AOIs emit one sparse COG per zone: `alerts_olivinskog_<epsg>.tif` (e.g. `alerts_olivinskog_32632.tif`, `alerts_olivinskog_32633.tif`, `alerts_olivinskog_32634.tif`, `alerts_olivinskog_32635.tif`). Vector outputs (`alerts_olivinskog_loss.pmtiles` and `.geojson`) reproject each zone's native UTM polygons to WGS84 for unified presentation.

## Scale and memory

* **Observation store volume.** `olivinskog` comprises 130,539 pixels across 20 home MGRS tiles and 61,276 catalogue scenes, yielding 117,382,672 observation rows. Partitioned into yearly Parquet files (2017–2026), this totals 1.2 GB on disk, compared to 360 MB for `kalklindeskog`.
* **Batched backfill and compaction.** Ingestion does not append directly to yearly Parquet files. Repeatedly reading, concatenating, and rewriting multi-gigabyte files across tens of thousands of scenes would incur $O(n^2)$ I/O overhead and create vulnerability to file corruption if interrupted. Instead, `obs.py` writes immutable part files per batch (`forestmon obs backfill`, batch size `INGEST_BATCH`, default 100 scenes, or `--batch`). The backfill is restartable and loses at most one batch of work on failure. `forestmon obs compact` consolidates part files into one sorted, deduplicated Parquet partition per year as a separate, resumable step.
* **Sparse raster allocation.** Olivinskog stands extend from 58°N to 69°N across Norway. In zone 32 alone, allocating a dense raster array over the full bounding box would require ~17 GB of RAM for an area that is >99.9% empty. `publish.write_cog` circumvents this by writing only occupied 512×512 pixel blocks into a sparse tiled GeoTIFF (`SPARSE_OK="TRUE"`), which GDAL copies to COG without allocating unpopulated blocks.
* **State memory.** The per-pixel recursive Kalman filter state is held in memory and persisted via `state.py`. For `olivinskog`, the state pickle is 173 MB.

## Running it

`olivinskog` uses the same CLI commands as `kalklindeskog`:

```bash
forestmon doctor                            # dependencies + credentials
forestmon grid build olivinskog             # initial grid build from MGRS geometry
forestmon catalog sync olivinskog           # STAC scene index and tile bounds
forestmon grid build olivinskog             # rebuild: assign CRS based on covering tiles (north of 64N)
forestmon grid assign-tiles olivinskog      # pin each pixel to its most interior tile
forestmon catalog schedule olivinskog       # when is the next scene readable?
forestmon parity check olivinskog           # CDSE == Earth Engine? (gate before bootstrap)
forestmon obs backfill olivinskog           # history -> store; batched, resumable
forestmon obs compact olivinskog            # parts -> one sorted parquet per year
forestmon sccd bootstrap olivinskog         # fit initial per-pixel state
forestmon run olivinskog                    # the operational cycle
forestmon catalog reconcile olivinskog      # weekly: late / reprocessed products
forestmon alerts list olivinskog            # list alerts
forestmon publish site olivinskog           # build PMTiles, GPKG, and per-zone COGs
forestmon health olivinskog                 # check backlog, latency, and store-to-model gap
forestmon logs olivinskog --failures        # inspect failure logs
```

## Known caveats

As documented in `docs/operational.md`:

* **Cloud masking is looser than Earth Engine's.** Earth Engine recipes require `s2cloudless` probability < 40, a Sentinel Hub product hosted by Google that is not part of the CDSE SAFE archive. The operational mask is SCL-only. Parity checks evaluate unmasked reflectance and track mask disagreement separately (`config.CLOUD_MASK_NOTE`).
* **Winter data gap.** At 58–69°N, the optical record halts from November to February due to low solar elevation and snow cover (SCL 11 rejected). S-CCD must not interpret the seasonal hiatus as disturbance. `confidence.py` caps the confidence of breaks in November to February, or after a gap of more than 60 days, at `SEASON_CAP` (0.30) and records the cause in `season_flag`.
* **The gate is under-calibrated.** `alerts.LOSS_GATE = 0.075` was tuned on 15 loss positives from annual labels. Near-real-time accuracy requires re-tuning against a dated near-real-time label set.
* **No confirmation logic yet.** S-CCD emits a single break above the gate. `confidence.py` scores it afterwards from the number of confirming observations (`n_confirm`) and agreement with OPERA DIST-ALERT (`agree_opera`); Sentinel-1 agreement is not implemented, and the gate itself remains the weak point.
