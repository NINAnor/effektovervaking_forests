# Forest Monitor dashboard

A static page showing S-CCD candidates of tree loss and regrowth for
**olivinskog** and **kalklindeskog**: a MapLibre map, an SVG chart of alert area over time and a
summary.

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

The browser reads only JSON, baked by Python from the published parquet under
the project's S3 prefix (`alerts/parquet/<aoi>_alerts_all.parquet` and
`condition/parquet/polygons_geo.parquet`):

```bash
forestmon publish dashboard --source "$FORESTMON_DASHBOARD_SOURCE" --out app/data
```

`FORESTMON_DASHBOARD_SOURCE` is the public HTTPS URL of the prefix
(`<endpoint>/<bucket>/<prefix>`; see `.env.example`) or a local folder with
the same layout. `index.json` is written last and lists the other files:

| file | contents |
| --- | --- |
| `index.json` | schema version, generation time, counts, period, file names |
| `polygons.geojson` | the 737 mapped stands (EPSG:4326, lightly simplified), with their 2025 condition class (local and global), score, trajectory, field condition and survey year from `condition/parquet/condition_stands.parquet` |
| `monthly.json` | per forest type and month: new loss/gain area, one value per certainty choice (`conf_tiers` in `index.json`: all, medium and high, high) |
| `points_<aoi>_<kind>.json` | one point per 10 m pixel, columnar: lon, lat, latest break date (YYYYMMDD), certainty, event count, |magnitude| of the latest break (dot size) |

Each pixel counts once: area by its first break, map date by its latest.
The app refuses an `index.json` whose `schema` differs from `SCHEMA` in
`js/app.js`; bump both (and `dashboard.SCHEMA`) when a file's shape changes.

Condition per 10 m cell is not baked: the page reads
`condition/pmtiles/condition_pixels.pmtiles` straight from S3 by range
requests (`conditionCells` in `config.js`; empty takes it from the S3 prefix the data were
baked from, and hides the layer when they were baked from a folder). It draws from
zoom 12 and is coloured by the 2025 score minus the stand threshold.

**C2C** (Composite2Change, yearly Sentinel-2 composites 2017–2025) is read
from `c2c/pmtiles/c2c_pixels.pmtiles` under the same S3 prefix (or `c2cPixels`
in `config.js`), source-layer `c2c_pixels`. Shown only where |ΔNBR| ≥ 0.175,
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
in `config.js`), the 10 m cell's condition 2017–2025 against its threshold, and
the stand's yearly condition (survey year marked) with its classes and
trajectory.

The map's **Layers** panel switches the basemap (`basemaps` in `config.js`:
Kartverket topo and Esri World Imagery) and each layer, with its key beside it.

`config.js`'s `dataBase` says where the JSON is: `data/` beside the page, or
any URL with CORS (e.g. the baked files copied to an S3 prefix).

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
