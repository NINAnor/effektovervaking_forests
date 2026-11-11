# Forest Monitor dashboard

A static page showing S-CCD loss and gain alerts and forest cover for
**olivinskog** and **kalklindeskog**: a MapLibre map, two SVG charts and a
summary.

```
app/
  index.html      markup, CSP, and the tags that load the rest
  app.css         styles
  js/boot.js      MapLibre CDN fallback, then config.js (cache-busted)
  js/app.js       everything else; must load last
  js/globals.d.ts declarations so `// @ts-check` works in an editor
  config.js       per-deployment settings: where the data is, the basemap
  vendor/         MapLibre 4.7.1, self-hosted
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
| `monthly.json` | per forest type and month: new loss/gain area, cumulative loss, cover |
| `points_<aoi>_<kind>.json` | one point per 10 m pixel, columnar: lon, lat, latest break month, confidence, event count |

Each pixel counts once: area by its first break, map date by its latest.
Cover is the polygons' area minus cumulative loss; gain is not added back.
The app refuses an `index.json` whose `schema` differs from `SCHEMA` in
`js/app.js`; bump both (and `dashboard.SCHEMA`) when a file's shape changes.

Condition per 10 m cell is not baked: the page reads
`condition/pmtiles/condition_pixels.pmtiles` straight from S3 by range
requests (`conditionCells` in `config.js`; empty takes it from the S3 prefix the data were
baked from, and hides the layer when they were baked from a folder). It draws from
zoom 12 and is coloured by the 2025 score minus the stand threshold.

**RADD** (Wageningen RADD Europe, Sentinel-1, 10 m) is optional. The old
inspector drew it as Earth Engine map tiles, but those IDs expire within hours,
so a static page samples it instead: `forestmon publish radd` (needs the `gee`
extra and `earthengine authenticate`) writes the alert pixels inside the stands
to `data/radd/parquet/radd_alerts.parquet`; the bake picks it up from
`<source>/radd/parquet/` or `--radd <file>`. Without it the RADD layer is hidden.

**Clicking the map** opens a card with everything under the point: RADD and
S-CCD alerts, the 10 m cell's condition 2017–2025 against its threshold, and
the stand's yearly condition (survey year marked) with its classes and
trajectory.

The map's **Layers** panel switches the basemap (`basemaps` in `config.js`:
OpenStreetMap and Esri World Imagery) and each layer, with its key beside it.

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
range requests), keeping the map off the critical path of a CDN. `js/boot.js` falls
back to unpkg with SRI if the files are missing. To refresh (and update both
digests in `js/boot.js`: `openssl dgst -sha384 -binary <file> | openssl base64 -A`):

```bash
curl -fsSL https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js  -o app/vendor/maplibre-gl.js
curl -fsSL https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css -o app/vendor/maplibre-gl.css
curl -fsSL https://unpkg.com/pmtiles@3.0.6/dist/pmtiles.js          -o app/vendor/pmtiles.js
```
