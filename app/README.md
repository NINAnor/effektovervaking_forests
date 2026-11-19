# Forest Monitor dashboard

A static page showing S-CCD candidates of tree loss and regrowth for
**olivinskog** and **kalklindeskog**: a MapLibre map, an SVG chart of alert area over time, the forest area remaining
each year (C2C) and a summary.

```
app/
  index.html      markup, CSP, and the tags that load the rest
  app.css         styles
  js/boot.js      MapLibre CDN fallback, then config.js (cache-busted)
  js/app.js       everything else; must load last
  js/globals.d.ts declarations so `// @ts-check` works in an editor
  config.js       per-deployment settings: where the data is, the basemap
  vendor/         MapLibre 4.7.1 and pmtiles.js 3.0.6, self-hosted
  data/           baked JSON (git-ignored; see below)
```

There is no build step, no `package.json` and no `node_modules`: plain
`<script>` tags that share one global scope. The stack is the one used by the
RECOVER labelling app.

## Data

The browser reads only JSON, baked by Python from three inputs (set in `.env`,
see `.env.example`):

* the S-CCD alert tables, `<aoi>_alerts.parquet`: the cycle's own
  `operational/alerts/` (`--alerts-dir`), else `sccd/operational/alerts/` under
  `FORESTMON_DASHBOARD_SOURCE`, the project's S3 prefix;
* C2C, `c2c/parquet/c2c_pixels.parquet` under the same prefix;
* condition, `FORESTMON_CONDITION_SOURCE`: the public dms dataset folder. The
  bake reads the stands from it and refuses to run while a file the app needs
  is missing there (`polygons_geo.parquet`, `condition_stands.parquet`,
  `condition_pixels.parquet`, `condition_pixels.pmtiles`, `index.csv` and every
  block it lists), naming the missing ones.

```bash
forestmon publish dashboard --out app/data
```

`index.json` is written last and lists the other files:

| file | contents |
| --- | --- |
| `index.json` | schema version, generation time (kept when nothing else changed), counts, period, file names; `condition`: the condition folder, when a URL; `forest_area`: per forest type the mapped area (overlapping polygons once) and the C2C loss each year from 2018 (NBR drop ≥ 0.175, each cell once, from `c2c/parquet/c2c_pixels.parquet`; null when the source has none, and the chart is hidden) |
| `polygons.geojson` | the 737 mapped stands (EPSG:4326, lightly simplified), with their latest condition class (local and global), score, trajectory, field condition and survey year from `condition/parquet/condition_stands.parquet` |
| `monthly.json` | per forest type and month: new loss/gain area, one value per certainty choice (`conf_tiers` in `index.json`: all, medium and high, high) |
| `points_<aoi>_<kind>.json` | one point per 10 m pixel, columnar: lon, lat, latest break date (YYYYMMDD), certainty, event count, |magnitude| of the latest break (dot size) |
| `downloads/` | behind the layers' ↓ menus, as the map shows them: `stands.gpkg.zip` (the polygons above), `alerts.gpkg.zip` (the points above, all forest types and kinds, with first and latest break), `c2c.parquet` (GeoParquet, every C2C pixel; absent when the source has none). Listed in `index.json` `downloads`, with condition's GeoParquet and block `index.csv` as links into its dms folder; fixed timestamps, so a bake of unchanged data leaves them unchanged |

Each pixel counts once: area by its first break, map date by its latest.
The app refuses an `index.json` whose `schema` differs from `SCHEMA` in
`js/app.js`; bump both (and `dashboard.SCHEMA`) when a file's shape changes.

Condition per 10 m cell is not baked: the page reads `condition_pixels.pmtiles`
in the condition folder (`index.json` `condition`) by range requests
(`conditionCells` in `config.js` overrides it; no layer when baked from a local
folder). It draws from
zoom 12 and is coloured by the latest score minus the stand threshold.

**C2C** (Composite2Change, yearly Sentinel-2 composites from 2017) is read
from `c2cPixels` in `config.js` (`c2c/pmtiles/c2c_pixels.pmtiles` under the S3
prefix), source-layer `c2c_pixels`. Shown only where |ΔNBR| ≥ 0.175,
the loss gate tuned against the labels in the old inspector, unless *Include
weak changes* is ticked (94 % of the pixels are below it). Coloured by the year
the change ended; `duration` is the years the line was stable before the drop.

**RADD** (Wageningen RADD Europe, Sentinel-1, 10 m) is drawn from Earth
Engine XYZ tiles, as in the old inspector, masked to the stands:
`forestmon publish radd` (needs the `gee` extra and `earthengine authenticate`)
mints them into `app/data/radd_tiles.json`. Map IDs expire within days, so run
it nightly; the panel shows the tiles' age.

Without a local Earth Engine login, a tile link can be pasted instead (Layers →
Radar alerts → *Use a map link*; kept in that browser's localStorage, and it
overrides the file). Run this in the [Code Editor](https://code.earthengine.google.com/)
and paste the printed link, or paste a map ID into
[eetiles.geocarpentry.org](https://eetiles.geocarpentry.org/) and copy its
*Standard XYZ URL*. To clip to the stands, upload them as a table asset first
(`forestmon publish stands-asset` writes the shapefile zip) and set `STANDS`.

```js
var STANDS = null; // e.g. 'projects/<your-project>/assets/forest_stands'
var img = ee.Image(ee.ImageCollection('projects/wurnrt-raddeurope/assets/01_NRT/00_Operational/V1_IC')
  .filterMetadata('layer', 'contains', 'alert')
  .filterMetadata('geography', 'equals', 'europe')
  .sort('system:time_end', false).first());
if (STANDS) img = img.updateMask(ee.Image(0).paint(ee.FeatureCollection(STANDS), 1).selfMask());
var a = img.select('Alert').unmask(0, false);
var vis = ee.ImageCollection([   // confirmed over unconfirmed, as the RADD app
  a.eq(3).selfMask().visualize({palette: ['EA7E7D']}),
  a.eq(2).selfMask().visualize({palette: ['00ffff'], opacity: 0.75})
]).mosaic();
Map.addLayer(vis, {}, 'RADD');
print(vis.getMap().urlFormat);  // paste this into the dashboard
```

**Clicking the map** opens a card with everything under the point: S-CCD
alerts (with a link to the pixel's Sentinel-2 NBR time series, `timeseriesUrl`
in `config.js`), the 10 m cell's yearly condition against its threshold, and
the stand's yearly condition (survey year marked) with its classes and
trajectory.

The map's **Layers** panel switches the basemap (`basemaps` in `config.js`:
Kartverket topo and Esri World Imagery) and each layer, with its key beside it.
**Past** is Esri World Imagery Wayback (`waybackConfig`), as in the habloss
labelling app: a release picker with ‹ ›, by default only the releases with a
new photo at the map centre (each release's tilemap names the release its tile
comes from), and the photo's own capture date there from the release's
metadata service.

`config.js`'s `dataBase` says where the JSON is: `data/` beside the page, or
any URL with CORS (e.g. the baked files copied to an S3 prefix).

## Updates

**Alerts (nightly, nothing to do).** With `FORESTMON_DASHBOARD_SOURCE` and
`FORESTMON_CONDITION_SOURCE` in `.env`, `scripts/forestmon_cycle.sh` re-bakes
`app/data` after each detect and publish, then commits and pushes it, and Pages
redeploys. A night without new alerts commits nothing. A failed bake keeps the
last one and makes the cycle exit non-zero.

**Condition (annual).** Run the condition pipeline, then upload its outputs to the
dms dataset by hand: `polygons_geo.parquet`, `condition_stands.parquet`,
`condition_pixels.parquet`, `condition_pixels.pmtiles`, `index.csv` and the
block COGs. If dms gives the update a new folder, point
`FORESTMON_CONDITION_SOURCE` at it. The next cycle picks it up, or run
`forestmon publish dashboard` to see it now. Until every file is there, the bake
says which are missing and the dashboard keeps showing the last year.

**C2C (annual).** Upload `c2c/parquet/c2c_pixels.parquet` and
`c2c/pmtiles/c2c_pixels.pmtiles` under the S3 prefix (same names); the next
bake refreshes the chart and the download.

A new year needs no code: the bake takes the years from the condition stands'
`dor_global_<year>` columns and C2C's change years, and the page's labels, keys
and C2C colours follow `index.json`.

The cycle runs on whichever machine has its cron line; moving it is in
`docs/deployment.md`, section 5.

## Running locally

```bash
forestmon publish dashboard --out app/data
python -m http.server 8000 --directory app   # open http://localhost:8000/
```

`file://` does not work: the app fetches its JSON.

## Tests and lint

- `tests/test_dashboard.py`: the baker, on a synthetic source.
- `tests/test_dashboard_app.py`: the page in headless Chromium (Playwright);
  skips where Playwright is not installed.
- eslint runs from prek (`eslint.config.mjs`), with no local node install.

## Vendored libraries

`vendor/` holds MapLibre and pmtiles.js 3.0.6 (reads PMTiles from S3 with
range requests), keeping the map off the critical path of a CDN. There is no CDN
fallback: the CSP allows `script-src 'self'` only. To refresh:

```bash
curl -fsSL https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js  -o app/vendor/maplibre-gl.js
curl -fsSL https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css -o app/vendor/maplibre-gl.css
curl -fsSL https://unpkg.com/pmtiles@3.0.6/dist/pmtiles.js          -o app/vendor/pmtiles.js
```
