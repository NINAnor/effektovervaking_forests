// @ts-check
// The dashboard. Classic script, loaded last: reads DASHBOARD_CONFIG, fetches
// the JSON that `forestmon publish dashboard` bakes, draws map and charts.

const CFG = window.DASHBOARD_CONFIG || {};
const BASE = (CFG.dataBase || 'data/').replace(/\/?$/, '/');
const SCHEMA = 6; // must match dashboard.SCHEMA in Python
const FOREST_COLORS = { olivinskog: '#6aa84f', kalklindeskog: '#d6b656' };
// Bright, with a white ring on the map, so the alerts stand out on imagery.
const KIND_COLORS = { loss: '#f03c3c', gain: '#2fd39a' };
const KIND_LABELS = { loss: 'Possible tree loss', gain: 'Possible regrowth' };
const NEW_COLOR = '#ffd60a'; // ring of alerts from the last NEW_DAYS
const NEW_DAYS = 30;
const KINDS = ['loss', 'gain'];
const BASEMAPS = CFG.basemaps || {
  osm: {
    label: 'Map (OpenStreetMap)',
    tiles: CFG.basemap || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
    attribution: CFG.basemapAttribution || '&copy; OpenStreetMap contributors'
  }
};
// Condition colours, as in the old inspector (ColorBrewer BrBG).
const CLASS_COLORS = {
  good_condition: '#35978f', indistinguishable: '#9aa39c', poor_condition: '#bf812d', no_data: '#d7dad3'
};
const CLASS_LABELS = {
  good_condition: 'good', indistinguishable: 'not decided', poor_condition: 'poor', no_data: 'no score'
};
// The field surveys behind the stands (the prefix of `layer`) and their condition words.
const SURVEYS = { MI: 'nature-type mapping (MI)', HB13: 'DN-håndbok 13', NiN5k: 'NiN 1:5000' };
const survey = (layer) => SURVEYS[String(layer).split('_')[0]] || layer;
const TILSTAND = { god: 'good', moderat: 'moderate', dårlig: 'poor', sværtRedusert: 'very reduced' };
const fieldCondition = (t) => (t ? TILSTAND[t] || t : 'not recorded');
const TRAJ_COLORS = { improving: '#35978f', stable: '#9aa39c', fluctuating: '#c8ccc2', declining: '#bf812d' };
// Cells: (score - threshold) x 1000, brown below the threshold, blue-green above.
// C2C: the year a change ended, light to dark purple (apart from the red/green alerts).
const C2C_YEARS = [2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025];
const C2C_COLORS = ['#efedf5', '#dadaeb', '#bcbddc', '#9e9ac8', '#807dba', '#6a51a3', '#54278f', '#3f007d'];
const C2C_GATE = 0.175; // |NBR drop| tuned against the labelled pixels (old inspector)
// Dot size by zoom: large when zoomed out, so scattered pixels are findable;
// smallest at mid zoom; from z16 the true 10 m footprint (at ~60° N: 5 m radius
// = 78271 / 2^z m per screen pixel), so dots grow into their ground pixel.
const DOT_STOPS = [[4, 7], [8, 6], [11, 4.5], [14, 3.5], [16, 4.2], [17, 8.4], [18, 16.8], [19, 33.5]];
// k scales the dot: a number, or a data expression (see bySize).
const dotRadius = (k = 1, add = 0) => ['interpolate', ['exponential', 2], ['zoom'],
  ...DOT_STOPS.flatMap(([z, r]) => [z, typeof k === 'number' ? r * k + add : ['+', ['*', r, k], add]])];
/** Dot scale from a change's size: half at zero, 0.75 at the cut-off, 1.4 at `full` and above. */
const bySize = (prop, gate, full) =>
  ['interpolate', ['linear'], ['abs', ['coalesce', ['get', prop], 0]], 0, 0.5, gate, 0.75, full, 1.4];
const SCCD_GATE = 0.075; // |dB08 - dB12|, the candidate cut-off; certainty saturates at 3x
// Up close the dots cover their pixel: let the imagery show through.
const DOT_OPACITY = ['interpolate', ['linear'], ['zoom'], 15, 0.95, 18, 0.6];
const STAND_FILL = ['interpolate', ['linear'], ['zoom'], 8, 0.6, 14, 0.2];
const CELL_FILL = ['interpolate', ['linear'], ['zoom'], 12, 0.5, 14, 0.85];
/** The base opacity of every layer behind each slider; the slider scales it. */
const FADE = {
  alerts: () => [...pointLayers.keys()].map((id) => [id, { 'circle-opacity': DOT_OPACITY, 'circle-stroke-opacity': 1 }]),
  c2c: () => [['c2c', { 'circle-opacity': DOT_OPACITY, 'circle-stroke-opacity': 1 }]],
  radd: () => [['radd', { 'raster-opacity': 0.9 }]],
  stands: () => [['forest-fill', { 'fill-opacity': STAND_FILL }], ['forest-line', { 'line-opacity': 1 }],
    ['forest-pts', { 'circle-opacity': 1, 'circle-stroke-opacity': 1 }]],
  cells: () => [['cells', { 'fill-opacity': CELL_FILL }]]
};
/** A number, or a zoom interpolation's outputs, times k. */
const fade = (v, k) => (typeof v === 'number' ? v * k : v.map((x, i) => (i > 3 && i % 2 === 0 ? x * k : x)));

function applyOpacity() {
  for (const [key, layers] of Object.entries(FADE)) {
    for (const [id, paint] of layers()) {
      if (!map.getLayer(id)) continue;
      for (const [prop, v] of Object.entries(paint)) map.setPaintProperty(id, prop, fade(v, state.opacity[key]));
    }
  }
}
const CELL_RAMP = [['#8c510a', -90], ['#bf812d', -60], ['#dfc27d', -30], ['#f6e8c3', 0],
  ['#c7eae5', 30], ['#80cdc1', 60], ['#35978f', 90], ['#01665e', null]];

const state = {
  forestType: 'all',
  kind: 'loss',
  minConf: 0.34, // low certainty is mostly winter and cloud-gap noise
  from: '', // ISO dates, inclusive
  to: '',
  resolution: 'annual',
  basemap: Object.keys(BASEMAPS)[0], // the first configured is the default
  alerts: true,
  radd: false,
  raddMode: 'alert',
  stands: true,
  standColor: 'forest_type',
  cells: false,
  c2c: false,
  c2cWeak: false,
  opacity: { alerts: 1, c2c: 1, radd: 1, stands: 1, cells: 1 }
};

/** @type {any} */ let index;
/** @type {any[]} */ let monthly = [];
/** @type {any} */ let map;
/** Point layers, keyed `${aoi}|${kind}`, as promises so a key loads once. */
const pointLayers = new Map();
/** The loaded columns per point layer, for counting what the filters keep. */
const pointCols = new Map();

// ── data ─────────────────────────────────────────────────────────────────────
async function fetchJson(name) {
  const url = BASE + name;
  // index.json is always fresh; the files it lists are cached until a new bake changes generated_at.
  const r = name === 'index.json' ? await fetch(url, { cache: 'no-store' })
    : await fetch(`${url}?v=${encodeURIComponent(index.generated_at)}`);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  try {
    return await r.json();
  } catch (e) {
    throw new Error(`${url}: not JSON (${e.message})`);
  }
}

async function loadIndex() {
  const idx = await fetchJson('index.json');
  if (idx.schema !== SCHEMA) {
    throw new Error(`index.json schema ${idx.schema}, app expects ${SCHEMA}; re-run forestmon publish dashboard`);
  }
  return idx;
}

/** Columnar points -> GeoJSON, once per file. */
function pointsToGeojson(cols, aoi, kind) {
  const n = cols.lon.length;
  for (const k of ['lat', 'date', 'conf', 'n', 'mag']) {
    if (!cols[k] || cols[k].length !== n) throw new Error(`points ${aoi}/${kind}: column ${k} has the wrong length`);
  }
  const features = new Array(n);
  for (let i = 0; i < n; i++) {
    features[i] = {
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [cols.lon[i], cols.lat[i]] },
      // lon/lat kept exact: rendered geometry is snapped to the tile grid.
      properties: { aoi, kind, lon: cols.lon[i], lat: cols.lat[i], date: cols.date[i], conf: cols.conf[i], n: cols.n[i], mag: cols.mag[i] }
    };
  }
  return { type: 'FeatureCollection', features };
}

// ── map ──────────────────────────────────────────────────────────────────────
function createMap() {
  map = new maplibregl.Map({
    container: 'map',
    style: {
      version: 8,
      sources: Object.fromEntries(Object.entries(BASEMAPS).map(([id, b]) => [`base-${id}`, {
        type: 'raster', tiles: [b.tiles], tileSize: 256, maxzoom: b.maxzoom || 19, attribution: b.attribution || ''
      }])),
      layers: Object.keys(BASEMAPS).map((id) => ({
        id: `base-${id}`, type: 'raster', source: `base-${id}`,
        layout: { visibility: id === state.basemap ? 'visible' : 'none' }
      }))
    },
    center: [9.5, 60.5],
    zoom: 5.5
  });
  map.addControl(new maplibregl.NavigationControl(), 'top-right');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-left');
  return new Promise((resolve) => map.on('load', resolve));
}

function standColor(mode) {
  const match = (prop, colors, fallback) =>
    ['match', ['coalesce', ['get', prop], ''], ...Object.entries(colors).flat(), fallback];
  if (mode === 'condition_local' || mode === 'condition_global') return match(mode, CLASS_COLORS, CLASS_COLORS.no_data);
  if (mode === 'trajectory') return match('trajectory', TRAJ_COLORS, CLASS_COLORS.no_data);
  return match('forest_type', FOREST_COLORS, '#999');
}

function standLegend(mode) {
  if (mode === 'condition_local' || mode === 'condition_global') {
    return Object.entries(CLASS_COLORS).map(([k, c]) => [CLASS_LABELS[k], c]);
  }
  if (mode === 'trajectory') return Object.entries(TRAJ_COLORS);
  return Object.entries(FOREST_COLORS);
}

function addPolygons(geojson) {
  map.addSource('forest', { type: 'geojson', data: geojson });
  // Each stand also as a point, so small stands are findable when zoomed out.
  map.addSource('forest-pts', {
    type: 'geojson',
    data: {
      type: 'FeatureCollection',
      features: geojson.features.map((f) => ({
        type: 'Feature',
        properties: f.properties,
        geometry: { type: 'Point', coordinates: [f.properties.lon, f.properties.lat] }
      }))
    }
  });
  const color = standColor(state.standColor);
  map.addLayer({
    id: 'forest-fill', type: 'fill', source: 'forest',
    paint: { 'fill-color': color, 'fill-opacity': STAND_FILL }
  });
  map.addLayer({
    id: 'forest-line', type: 'line', source: 'forest',
    paint: {
      // Declining stands get a heavier brown outline, as in the old inspector.
      'line-color': ['case', ['==', ['get', 'trajectory'], 'declining'], '#8c510a', color],
      'line-width': ['interpolate', ['linear'], ['zoom'],
        8, 1, 14, ['case', ['==', ['get', 'trajectory'], 'declining'], 3, 1.5]]
    }
  });
  map.addLayer({
    id: 'forest-pts', type: 'circle', source: 'forest-pts', maxzoom: 10,
    paint: {
      'circle-color': color,
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 4, 5, 10, 6],
      'circle-stroke-color': '#0f1620',
      'circle-stroke-width': 1
    }
  });
  fitTo(geojson);
}

/** config.js wins; otherwise the pmtiles under the S3 prefix the data were baked from. */
function cellsUrl() {
  if (CFG.conditionCells) return CFG.conditionCells;
  const src = index.source;
  return src && src !== 'local' ? src.replace(/\/?$/, '/') + 'condition/pmtiles/condition_pixels.pmtiles' : '';
}

let pmtilesReady = false;
/** Register the pmtiles:// protocol once, for whichever layer needs it first. */
function usePmtiles() {
  if (pmtilesReady) return;
  maplibregl.addProtocol('pmtiles', new pmtiles.Protocol().tile);
  pmtilesReady = true;
}

function addConditionCells() {
  const url = cellsUrl();
  if (!url || typeof pmtiles === 'undefined') {
    if (url) console.error('pmtiles.js failed to load; condition cells are off');
    document.getElementById('cells-section').hidden = true;
    return;
  }
  usePmtiles();
  map.addSource('cells', { type: 'vector', url: 'pmtiles://' + url });
  const diff = ['*', 1000, ['-', ['to-number', ['get', 'dor_global_2025']], ['to-number', ['get', 'threshold_global']]]];
  const ramp = ['step', diff, CELL_RAMP[0][0]];
  for (let k = 0; k < CELL_RAMP.length - 1; k++) ramp.push(CELL_RAMP[k][1], CELL_RAMP[k + 1][0]);
  map.addLayer({
    id: 'cells', type: 'fill', source: 'cells', 'source-layer': 'condition_pixels',
    minzoom: 12, // below this a 10 m cell is smaller than a screen pixel
    layout: { visibility: state.cells ? 'visible' : 'none' },
    paint: {
      'fill-color': ['case', ['has', 'dor_global_2025'], ramp, CLASS_COLORS.no_data],
      'fill-opacity': CELL_FILL
    }
  }, 'forest-line');
}

/** config.js wins; otherwise C2C's pmtiles under the S3 prefix the data were baked from. */
function c2cUrl() {
  if (CFG.c2cPixels) return CFG.c2cPixels;
  const src = index.source;
  return src && src !== 'local' ? src.replace(/\/?$/, '/') + 'c2c/pmtiles/c2c_pixels.pmtiles' : '';
}

function addC2c() {
  const url = c2cUrl();
  if (!url || typeof pmtiles === 'undefined') {
    document.getElementById('c2c-section').hidden = true;
    return;
  }
  usePmtiles();
  map.addSource('c2c', { type: 'vector', url: 'pmtiles://' + url });
  const year = ['round', ['coalesce', ['get', 'change_year'], C2C_YEARS[0]]];
  const color = ['step', year, C2C_COLORS[0]];
  C2C_YEARS.slice(1).forEach((y, i) => color.push(y, C2C_COLORS[i + 1]));
  map.addLayer({
    id: 'c2c', type: 'circle', source: 'c2c', 'source-layer': 'c2c_pixels',
    layout: { visibility: 'none' },
    paint: {
      'circle-color': color,
      'circle-radius': dotRadius(bySize('magnitude', C2C_GATE, 0.5)),
      'circle-stroke-color': '#1d1030', 'circle-stroke-width': 0.8, 'circle-opacity': DOT_OPACITY
    }
  });
}

/** C2C dates a loss to the first yearly image after it (change_year X.996 = loss in X+1). */
const c2cYear = (p) => Math.round(Number(p.change_year));

function c2cSection(p) {
  const year = c2cYear(p), drop = Math.abs(Number(p.magnitude));
  const late = year >= 2024 ? ' (last years are undercounted)' : '';
  return `<section><h4>Forest-loss map (C2C)</h4><div class="chips">` +
    chip(`loss in ${year}`, C2C_COLORS[Math.max(0, C2C_YEARS.indexOf(year))]) +
    (drop < C2C_GATE ? chip('weak: below the 0.175 cut-off', CLASS_COLORS.no_data) : '') + '</div>' +
    `<dl><dt>Loss year</dt><dd>${year}${late}</dd>` +
    `<dt>Size of drop (NBR)</dt><dd>${fmt(drop, 2)} (cut-off 0.175)</dd>` +
    `<dt>Years stable before</dt><dd>${fmt(p.duration, 0)}</dd>` +
    `<dt>Drops found here</dt><dd>${fmt(p.n_breaks, 0)}</dd>` +
    `<dt>Hansen (Global Forest Watch)</dt><dd>${p.hansen_lossyear ? `loss in ${Math.round(p.hansen_lossyear)}` : 'no loss mapped'}</dd></dl>` +
    `${timeseriesLink([Number(p.lon), Number(p.lat)])}</section>`;
}

function score(v) {
  return v === null || v === undefined || v === '' ? 'no score' : fmt(v, 3);
}

// One popup for the whole map: the topmost kind of feature under the cursor wins.
const POPUPS = [
  [(id) => pointLayers.has(id), (p) =>
    `<strong>${esc(KIND_LABELS[p.kind])}</strong> &mdash; ${esc(p.aoi)}<br>detected ${fmtDate(p.date)}<br>` +
    `certainty ${certainty(p.conf)}, size ${fmt(p.mag, 2)} &middot; click for details`],
  [(id) => id === 'c2c', (p) =>
    `<strong>Forest loss in ${c2cYear(p)}</strong> (C2C)<br>size of drop ${fmt(Math.abs(p.magnitude), 2)} &middot; click for details`],
  [(id) => id === 'cells', (p) =>
    `<strong>10 m cell</strong><br>score 2025: ${score(p.dor_global_2025)} ` +
    `(good/poor threshold ${fmt(p.threshold_global, 3)})<br>` +
    `stand: ${esc(CLASS_LABELS[p.stand_condition_class] || '—')}, change 2017–2025 ${esc(p.trajectory_class || '—')}<br>` +
    `${fmt(p.edge_m, 0)} m from the stand edge`],
  [(id) => id === 'forest-fill' || id === 'forest-pts', (p) =>
    `<strong>${esc(p.forest_type)} stand</strong> &mdash; ${esc(survey(p.layer))}<br>` +
    `${fmt(p.area_ha, 2)} ha, surveyed ${esc(p.survey_year ?? '—')} (field condition: ${esc(fieldCondition(p.tilstand))})<br>` +
    `condition 2025: ${score(p.dor_global_2025)}, ${esc(CLASS_LABELS[p.condition_local] || 'no score')} (local) / ` +
    `${esc(CLASS_LABELS[p.condition_global] || 'no score')} (nature type)<br>change 2017–2025 ${esc(p.trajectory || '—')}`]
];

/** @type {any} */ let popup;

function setupPopup() {
  popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, maxWidth: '320px' });
  map.getCanvas().addEventListener('mouseleave', () => popup.remove());
  map.on('mousemove', (e) => {
    if (!document.getElementById('detail').hidden) {
      map.getCanvas().style.cursor = '';
      popup.remove();
      return;
    }
    const layers = ['forest-fill', 'forest-pts', 'cells', 'c2c', ...pointLayers.keys()].filter((id) => map.getLayer(id));
    const feats = map.queryRenderedFeatures(e.point, { layers });
    let html = null;
    for (const [match, render] of POPUPS) {
      const f = feats.find((x) => match(x.layer.id));
      if (f) { html = render(f.properties); break; }
    }
    map.getCanvas().style.cursor = html ? 'pointer' : '';
    if (html) popup.setLngLat(e.lngLat).setHTML(html).addTo(map);
    else popup.remove();
  });
}

function ensurePoints(aoi, kind) {
  const key = `${aoi}|${kind}`;
  if (!pointLayers.has(key)) {
    const file = index.files.points[aoi][kind];
    pointLayers.set(key, fetchJson(file).then((cols) => {
      pointCols.set(key, cols);
      map.addSource(key, { type: 'geojson', data: pointsToGeojson(cols, aoi, kind) });
      map.addLayer({
        id: key,
        type: 'circle',
        source: key,
        paint: {
          'circle-color': KIND_COLORS[kind],
          'circle-radius': dotRadius(bySize('mag', SCCD_GATE, 3 * SCCD_GATE)),
          'circle-stroke-color': ['case', isNew(), NEW_COLOR, '#ffffff'],
          'circle-stroke-width': ['case', isNew(), 2.5, 1],
          'circle-opacity': DOT_OPACITY,
          'circle-stroke-opacity': 1
        }
      });
      applyMapState(); // filter and show it per the state now, not on the next refresh
      return key;
    }).catch((e) => {
      pointLayers.delete(key); // retry on the next refresh
      throw e;
    }));
  }
  return pointLayers.get(key);
}

// ── RADD: Earth Engine XYZ tiles, masked to the stands (forestmon publish radd) ──
/** radd_tiles.json, a map link pasted in this browser, or null. */
let raddTiles = null;
/** The file's tiles, to fall back to when a pasted link is cleared. */
let raddFile = null;
const RADD_LINK_KEY = 'forestmon:radd-link';
// An Earth Engine map: what getMap()'s urlFormat and eetiles.geocarpentry.org give.
const EE_MAP = /^https:\/\/[a-z0-9.-]+\.googleapis\.com\/v1\/projects\/[^/\s]+\/maps\/[^/\s?#]+/i;

/** A pasted link as radd_tiles.json would hold it; one visualisation serves both modes. */
function linkTiles({ url, at }) {
  const tiles = `${url.match(EE_MAP)[0]}/tiles/{z}/{x}/{y}`;
  return { pasted: true, minted_at: at, layers: { alert: { tiles }, alert_date: { tiles } } };
}

function setRaddLink(link) {
  try {
    if (link) localStorage.setItem(RADD_LINK_KEY, JSON.stringify(link));
    else localStorage.removeItem(RADD_LINK_KEY);
  } catch { /* private mode: this page view only */ }
  raddTiles = link ? linkTiles(link) : raddFile;
  describeRadd();
  if (map.getSource('radd')) checkRaddTiles();
  applyRadd();
}

function storedRaddLink() {
  try {
    const link = JSON.parse(localStorage.getItem(RADD_LINK_KEY) || 'null');
    return link && EE_MAP.test(link.url) ? link : null;
  } catch {
    return null;
  }
}

async function loadRaddTiles() {
  try {
    return await fetchJson('radd_tiles.json');
  } catch {
    return null; // optional layer
  }
}

/** The template the radd source was last given (MapLibre updates source.tiles asynchronously). */
let raddApplied = null;

function applyRadd() {
  if (!raddTiles) {
    if (map.getLayer('radd')) map.setLayoutProperty('radd', 'visibility', 'none');
    return;
  }
  const tiles = [raddTiles.layers[state.raddMode].tiles];
  if (!map.getSource('radd')) {
    if (!state.radd) return; // nothing is fetched until it is switched on
    map.addSource('radd', {
      type: 'raster', tiles, tileSize: 256, maxzoom: 15,
      attribution: '<a href="https://www.wur.nl/en/research-results/research-institutes/environmental-research/radd-forest-disturbance-alert.htm">RADD</a> / Wageningen University'
    });
    map.addLayer({ id: 'radd', type: 'raster', source: 'radd', paint: { 'raster-opacity': 0.9 } }, 'forest-line');
    raddApplied = tiles[0];
    checkRaddTiles();
  } else if (state.radd && raddApplied !== tiles[0]) {
    map.getSource('radd').setTiles(tiles);
    raddApplied = tiles[0];
  }
  map.setLayoutProperty('radd', 'visibility', state.radd ? 'visible' : 'none');
}

/** MapLibre stays silent on tile 404s, so probe one tile per layer once. */
function checkRaddTiles() {
  for (const { tiles } of Object.values(raddTiles.layers)) {
    fetch(tiles.replace('{z}', 0).replace('{x}', 0).replace('{y}', 0))
      .then((r) => { if (!r.ok) throw new Error(r.status); })
      .catch(() => {
        document.getElementById('radd-note').textContent = raddTiles?.pasted
          ? 'The map link failed to load: Earth Engine links expire. Paste a new one.'
          : 'RADD tiles failed to load. Earth Engine map IDs expire: re-run forestmon publish radd.';
      });
  }
}

function describeRadd() {
  const note = document.getElementById('radd-note');
  document.getElementById('radd-modes').hidden = !raddTiles || raddTiles.pasted;
  for (const el of document.querySelectorAll('#radd-section .key, #radd-section label.opacity')) el.hidden = !raddTiles;
  // No layer: nothing to switch on, only the advanced link form.
  document.querySelector('#radd-section .switch input').disabled = !raddTiles;
  if (!raddTiles) { note.textContent = 'Not available yet.'; return; }
  const days = (Date.now() - raddTiles.minted_at) / 864e5;
  note.textContent = raddTiles.pasted
    ? `Your pasted map link, added ${fmt(days * 24, 0)} h ago. Links expire; paste a new one if the layer goes blank.`
    : `Image ${raddTiles.image_id} (v${raddTiles.version_date}), tiles made ${fmt(days, 0)} d ago` +
      (days > 3 ? ' — the layer may be out of date.' : '.');
}

function setupRaddLink() {
  const input = /** @type {HTMLInputElement} */ (document.getElementById('radd-link'));
  const msg = document.getElementById('radd-link-msg');
  document.getElementById('radd-link-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const url = input.value.trim();
    if (!EE_MAP.test(url)) {
      msg.textContent = 'Not an Earth Engine map link: it should contain …googleapis.com/v1/projects/…/maps/…';
      return;
    }
    msg.textContent = '';
    setRaddLink({ url, at: Date.now() });
  });
  document.getElementById('radd-link-clear').addEventListener('click', () => {
    input.value = '';
    msg.textContent = '';
    setRaddLink(null);
  });
}

// ── the click card: everything under the clicked point ───────────────────────
/** Stand properties by poly_id; the map's copies have their arrays stringified. */
const standById = new Map();

/** Condition by year against the threshold, as a small SVG line. */
function trajChart(series, threshold, years, markYear) {
  const pts = series.map((v, i) => [years[i], v]).filter(([, v]) => v !== null && v !== undefined);
  if (!pts.length) return '<p class="hint">No yearly scores.</p>';
  const W = 300, H = 120, m = { l: 34, r: 16, t: 8, b: 20 };
  const vals = pts.map(([, v]) => Number(v)).concat(threshold ?? []);
  const lo = Math.min(...vals) - 0.02, hi = Math.max(...vals) + 0.02;
  const x = (yr) => m.l + ((yr - years[0]) / Math.max(1, years[years.length - 1] - years[0])) * (W - m.l - m.r);
  const y = (v) => H - m.b - ((v - lo) / (hi - lo)) * (H - m.t - m.b);
  let g = '';
  for (const v of [lo + 0.02, (lo + hi) / 2, hi - 0.02]) {
    g += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/>` +
      `<text x="${m.l - 4}" y="${y(v) + 3}" text-anchor="end">${fmt(v, 2)}</text>`;
  }
  for (const yr of [years[0], years[years.length >> 1], years[years.length - 1]]) {
    g += `<text x="${x(yr)}" y="${H - 5}" text-anchor="middle">${yr}</text>`;
  }
  if (markYear >= years[0] && markYear <= years[years.length - 1]) g += `<line class="mark" x1="${x(markYear)}" x2="${x(markYear)}" y1="${m.t}" y2="${H - m.b}"><title>surveyed ${markYear}</title></line>`;
  if (threshold !== null && threshold !== undefined) {
    g += `<line class="thr" x1="${m.l}" x2="${W - m.r}" y1="${y(threshold)}" y2="${y(threshold)}"><title>threshold ${fmt(threshold, 3)}</title></line>`;
  }
  g += `<polyline class="traj" points="${pts.map(([yr, v]) => `${x(yr)},${y(v)}`).join(' ')}"/>`;
  for (const [yr, v] of pts) {
    const good = threshold === null || threshold === undefined || v >= threshold;
    g += `<circle cx="${x(yr)}" cy="${y(v)}" r="3.5" fill="${good ? CLASS_COLORS.good_condition : CLASS_COLORS.poor_condition}">` +
      `<title>${yr}: ${fmt(v, 3)} (${good ? 'above' : 'below'} threshold)</title></circle>`;
  }
  return `<svg class="traj-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Condition score by year against the threshold">${g}</svg>`;
}

/** Theil-Sen slope per year, in score units (the tiles' sen_slope is standardised). */
function senSlope(series, years) {
  const pts = series.map((v, i) => [years[i], v]).filter(([, v]) => v !== null && v !== undefined);
  const slopes = [];
  for (let i = 0; i < pts.length; i++) {
    for (let j = i + 1; j < pts.length; j++) slopes.push((pts[j][1] - pts[i][1]) / (pts[j][0] - pts[i][0]));
  }
  if (!slopes.length) return null;
  slopes.sort((a, b) => a - b);
  const k = slopes.length >> 1;
  return slopes.length % 2 ? slopes[k] : (slopes[k - 1] + slopes[k]) / 2;
}

function trend(series) {
  const s = senSlope(series, index.years);
  return s === null ? '—' : `${s >= 0 ? '+' : ''}${fmt(s, 4)} / yr`;
}

/** The pixel in an external Sentinel-2 time-series viewer (config.js). */
function timeseriesLink([lon, lat]) {
  if (!CFG.timeseriesUrl) return '';
  const url = CFG.timeseriesUrl.replace('{lon}', lon.toFixed(5)).replace('{lat}', lat.toFixed(5));
  return `<a class="ext-link" href="${esc(url)}" target="_blank" rel="noopener">See this spot's satellite image history &#8599;</a>` +
    '<p class="hint">Opens a Sentinel-2 viewer in a new tab; the chart takes a moment to load. ' +
    'A drop in the line is when the forest changed.</p>';
}

function chip(text, color) {
  return `<span class="chip"><i style="background:${color}"></i>${esc(text)}</span>`;
}

function cellSection(p) {
  const series = index.years.map((yr) => p[`dor_global_${yr}`] ?? null);
  const traj = p.trajectory_class || '—';
  const above = Number(p.dor_global_2025) >= Number(p.threshold_global);
  return '<section><h4>10 m cell condition</h4>' +
    trajChart(series, p.threshold_global, index.years) +
    '<div class="chips">' +
    (p.dor_global_2025 == null ? chip('2025: no score', CLASS_COLORS.no_data)
      : chip(`2025: ${above ? 'above' : 'below'} threshold`, above ? CLASS_COLORS.good_condition : CLASS_COLORS.poor_condition)) +
    chip(`change 2017–2025: ${traj}`, TRAJ_COLORS[traj] || CLASS_COLORS.no_data) + '</div>' +
    `<dl><dt>Score 2025 (nature type)</dt><dd>${score(p.dor_global_2025)}</dd><dt>Good/poor threshold</dt><dd>${fmt(p.threshold_global, 3)}</dd>` +
    `<dt>Trend 2017–2025</dt><dd>${trend(series)}</dd><dt>Distance to stand edge</dt><dd>${fmt(p.edge_m, 0)} m</dd></dl></section>`;
}

function standSection(p) {
  const local = p.condition_local || 'no_data', global = p.condition_global || 'no_data';
  return `<section><h4>Stand: ${esc(p.forest_type)}</h4>` +
    trajChart(p.series || [], p.threshold_global, index.years, p.survey_year) +
    '<div class="chips">' +
    chip(`local: ${CLASS_LABELS[local] || 'no score'}`, CLASS_COLORS[local]) +
    chip(`nature type: ${CLASS_LABELS[global] || 'no score'}`, CLASS_COLORS[global]) +
    chip(`change 2017–2025: ${p.trajectory || '—'}`, TRAJ_COLORS[p.trajectory] || CLASS_COLORS.no_data) + '</div>' +
    `<dl><dt>Area</dt><dd>${fmt(p.area_ha, 2)} ha</dd><dt>Survey</dt><dd>${esc(survey(p.layer))}, ${esc(p.survey_year ?? 'year unknown')}</dd>` +
    `<dt>Survey ID</dt><dd>${esc(String(p.poly_id).split(':').pop())}</dd>` +
    `<dt>Field condition (survey)</dt><dd>${esc(fieldCondition(p.tilstand))}</dd><dt>Score 2025 (nature type)</dt><dd>${score(p.dor_global_2025)}</dd>` +
    `<dt>Trend 2017–2025</dt><dd>${trend(p.series || [])}</dd></dl></section>`;
}

function showDetail(e) {
  const at = (ids) => map.queryRenderedFeatures(e.point, { layers: ids.filter((id) => map.getLayer(id)) });
  const sccd = at([...pointLayers.keys()]);
  const cell = at(['cells'])[0];
  const c2c = at(['c2c'])[0];
  const stand = at(['forest-fill', 'forest-pts'])[0];
  let html = '';
  for (const f of sccd.slice(0, 3)) {
    const p = f.properties;
    html += `<section><h4>Tree-cover alert (Sentinel-2)</h4><div class="chips">${chip(KIND_LABELS[p.kind], KIND_COLORS[p.kind])}` +
      `${p.date >= newCutoff() ? chip('new', NEW_COLOR) : ''}</div>` +
      `<dl><dt>Detected</dt><dd>${fmtDate(p.date)}</dd><dt>Certainty</dt><dd>${certainty(p.conf)}</dd>` +
      `<dt>Size of change</dt><dd>${fmt(p.mag, 3)} (cut-off ${SCCD_GATE})</dd>` +
      `<dt>Changes detected here</dt><dd>${esc(p.n)}</dd></dl>${timeseriesLink([p.lon, p.lat])}</section>`;
  }
  if (c2c) html += c2cSection(c2c.properties);
  if (cell) html += cellSection(cell.properties);
  if (stand) html += standSection(standById.get(stand.properties.poly_id) || stand.properties);
  const card = document.getElementById('detail');
  if (!html) { card.hidden = true; setMarker(null); return; }
  // On the dot that was picked (rendered coordinates are snapped to the tile grid).
  const dot = sccd[0] || c2c;
  const standId = !dot && !cell && stand ? stand.properties.poly_id : null;
  document.getElementById('detail-title').textContent =
    `${fmt(e.lngLat.lat, 5)}° N, ${fmt(e.lngLat.lng, 5)}° E`;
  document.getElementById('detail-body').innerHTML = html;
  card.hidden = false;
  if (!card.contains(document.activeElement)) document.getElementById('detail-close').focus();
  if (popup) popup.remove();
  setMarker(dot ? [Number(dot.properties.lon), Number(dot.properties.lat)] : [e.lngLat.lng, e.lngLat.lat], standId);
}

let pulse = 0;

/** The selection: a dark-and-white ring around the picked dot (pulsing once), or the stand's outline. */
function setMarker(coords, standId = null) {
  const data = { type: 'FeatureCollection', features: coords && standId === null
    ? [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: coords } }] : [] };
  if (!map.getSource('sel')) {
    map.addSource('sel', { type: 'geojson', data });
    for (const [id, color, width] of [['sel-halo', '#111', 6], ['sel', '#fff', 3]]) {
      map.addLayer({
        id, type: 'circle', source: 'sel',
        paint: { 'circle-radius': dotRadius(1, 6), 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': color, 'circle-stroke-width': width }
      });
    }
    for (const [id, color, width] of [['sel-stand-halo', '#111', 6], ['sel-stand', '#fff', 3]]) {
      map.addLayer({ id, type: 'line', source: 'forest', filter: ['==', ['get', 'poly_id'], -1], paint: { 'line-color': color, 'line-width': width } });
    }
  } else map.getSource('sel').setData(data);
  for (const id of ['sel-stand-halo', 'sel-stand']) map.setFilter(id, ['==', ['get', 'poly_id'], standId ?? -1]);
  // Alert layers load later: keep the selection on top.
  for (const id of ['sel-stand-halo', 'sel-stand', 'sel-halo', 'sel']) map.moveLayer(id);
  cancelAnimationFrame(pulse);
  if (!coords || standId !== null || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const t0 = performance.now();
  const step = (t) => {
    const k = Math.min(1, (t - t0) / 700);
    for (const id of ['sel-halo', 'sel']) map.setPaintProperty(id, 'circle-radius', dotRadius(1, 6 + 24 * (1 - k) ** 2));
    if (k < 1) pulse = requestAnimationFrame(step);
  };
  pulse = requestAnimationFrame(step);
}

function setupDetail(polygons) {
  for (const f of polygons.features) standById.set(f.properties.poly_id, f.properties);
  map.on('click', showDetail);
  map.on('zoomend', setCellsHint);
  const close = () => {
    document.getElementById('detail').hidden = true;
    setMarker(null);
  };
  document.getElementById('detail-close').addEventListener('click', close);
  document.addEventListener('keydown', (e) => {
    const dialog = /** @type {HTMLDialogElement} */ (document.getElementById('info'));
    if (e.key === 'Escape' && !dialog.open && !document.getElementById('detail').hidden) close();
  });
}

function fitTo(geojson) {
  let w = 180, s = 90, e = -180, n = -90;
  const visit = (c) => {
    if (typeof c[0] === 'number') {
      w = Math.min(w, c[0]); e = Math.max(e, c[0]); s = Math.min(s, c[1]); n = Math.max(n, c[1]);
    } else c.forEach(visit);
  };
  for (const f of geojson.features) if (f.geometry) visit(f.geometry.coordinates);
  if (w <= e) map.fitBounds([[w, s], [e, n]], { padding: 30, duration: 0 });
}

function wantedLayers() {
  const aois = state.forestType === 'all' ? index.forest_types : [state.forestType];
  const kinds = state.kind === 'all' ? KINDS : [state.kind];
  return aois.flatMap((aoi) => kinds.map((kind) => ({ aoi, kind })));
}

/** Show and filter the loaded layers from the state as it is NOW. */
function applyMapState() {
  const forest = state.forestType === 'all' ? null : ['==', ['get', 'forest_type'], state.forestType];
  const color = standColor(state.standColor);
  for (const id of ['forest-fill', 'forest-line', 'forest-pts']) {
    map.setFilter(id, forest);
    map.setLayoutProperty(id, 'visibility', state.stands ? 'visible' : 'none');
  }
  map.setPaintProperty('forest-fill', 'fill-color', color);
  map.setPaintProperty('forest-pts', 'circle-color', color);
  map.setPaintProperty('forest-line', 'line-color', ['case', ['==', ['get', 'trajectory'], 'declining'], '#8c510a', color]);
  if (map.getLayer('c2c')) {
    const gate = ['>=', ['abs', ['coalesce', ['get', 'magnitude'], 0]], state.c2cWeak ? 0 : C2C_GATE];
    map.setFilter('c2c', forest ? ['all', forest, gate] : gate);
    map.setLayoutProperty('c2c', 'visibility', state.c2c ? 'visible' : 'none');
  }
  if (map.getLayer('cells')) {
    map.setFilter('cells', forest);
    map.setLayoutProperty('cells', 'visibility', state.cells ? 'visible' : 'none');
  }
  for (const id of Object.keys(BASEMAPS)) {
    map.setLayoutProperty(`base-${id}`, 'visibility', id === state.basemap ? 'visible' : 'none');
  }
  if (popup) popup.remove();
  document.getElementById('stand-legend').innerHTML =
    standLegend(state.standColor).map(([name, c]) =>
      `<li><i class="sw" style="background:${c}"></i> ${esc(name)}</li>`).join('') +
    '<li><i class="sw sw--outline"></i> declining (outline)</li>';
  const n = state.forestType === 'all' ? index.polygon_count
    : index.polygon_counts?.[state.forestType] ?? index.polygon_count;
  document.getElementById('stand-count').textContent = `(${n})`;
  const wanted = new Set(wantedLayers().map(({ aoi, kind }) => `${aoi}|${kind}`));
  const [from, to] = [ymd(state.from), ymd(state.to)];
  const filter = ['all',
    ['>=', ['get', 'date'], from],
    ['<=', ['get', 'date'], to],
    ['>=', ['get', 'conf'], state.minConf]];
  let shown = 0;
  for (const key of pointLayers.keys()) {
    if (!map.getLayer(key)) continue;
    const on = state.alerts && wanted.has(key);
    map.setLayoutProperty(key, 'visibility', on ? 'visible' : 'none');
    map.setFilter(key, filter);
    const c = pointCols.get(key);
    if (wanted.has(key) && c) for (let i = 0; i < c.date.length; i++) shown += +(c.date[i] >= from && c.date[i] <= to && c.conf[i] >= state.minConf);
  }
  setCellsHint();
  document.getElementById('alert-count').textContent = pointsLoading ? '(loading…)' : `(${shown.toLocaleString()})`;
  applyRadd();
  applyOpacity();
}

/** The squares only draw from zoom 12; say so when they are on but not yet visible. */
function setCellsHint() {
  const hint = document.getElementById('cells-hint');
  if (hint) hint.textContent = state.cells && map.getZoom() < 12
    ? 'Zoom in further to see them (they show from zoom 12).' : 'Zoom in close to see them.';
}

let mapGeneration = 0;
/** True while a refresh is still fetching point files, so the count is not shown as 0. */
let pointsLoading = false;

async function refreshMap() {
  const gen = ++mapGeneration;
  pointsLoading = true;
  applyMapState();
  const results = await Promise.allSettled(wantedLayers().map(({ aoi, kind }) => ensurePoints(aoi, kind)));
  // The state may have changed while files loaded; apply the current one.
  if (gen === mapGeneration) pointsLoading = false;
  applyMapState();
  if (gen !== mapGeneration) return; // a newer refresh owns the status line
  const failed = results.find((r) => r.status === 'rejected');
  if (failed) showError(`Some alert points failed to load: ${failed.reason.message}`);
  else showStatus();
}

// ── charts (plain SVG) ───────────────────────────────────────────────────────
function filteredMonthly() {
  return monthly.filter((d) =>
    (state.forestType === 'all' || d.forest_type === state.forestType) &&
    d.period >= state.from.slice(0, 7) && d.period <= state.to.slice(0, 7));
}

/** Months -> years when grouped by year: areas are summed. */
function byResolution(rows) {
  if (state.resolution === 'monthly') return rows.map((d) => ({ ...d, x: d.period }));
  const out = new Map();
  for (const d of rows) {
    const key = `${d.forest_type}|${d.period.slice(0, 4)}`;
    const o = out.get(key) || { forest_type: d.forest_type, x: d.period.slice(0, 4), loss_ha: 0, gain_ha: 0 };
    o.loss_ha += d.loss_ha;
    o.gain_ha += d.gain_ha;
    out.set(key, o);
  }
  return [...out.values()];
}

function chartFrame(el, xs, yMax, yLabel) {
  const W = Math.max(el.clientWidth || 480, 280), H = 220;
  const m = { l: 60, r: 10, t: 10, b: 26 };
  const band = (W - m.l - m.r) / xs.length;
  const x = (i) => m.l + (i + 0.5) * band;
  // Round tick steps (1, 2 or 5 x 10^n), never finer than one pixel (0.01 ha).
  const raw = Math.max((yMax || 1) / 4, 0.01);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const tick = [1, 2, 5, 10].map((k) => k * mag).find((t) => t >= raw * 0.999);
  const top = Math.ceil((yMax || 1) / tick) * tick;
  const y = (v) => H - m.b - (v / top) * (H - m.t - m.b);
  const digits = Math.max(0, -Math.floor(Math.log10(tick) + 1e-9));
  let g = '';
  for (let v = 0; v <= top + tick / 2; v += tick) {
    g += `<line class="grid" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/>` +
      `<text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${fmt(v, digits)}</text>`;
  }
  const step = Math.ceil(xs.length / 8);
  xs.forEach((t, i) => {
    if ((xs.length - 1 - i) % step === 0) g += `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${esc(t)}</text>`;
  });
  g += `<text x="12" y="${m.t + 4}" transform="rotate(-90 12 ${m.t + 4})" text-anchor="end">${esc(yLabel)}</text>`;
  return { W, H, x, y, g, band, m };
}

function legend(items) {
  return '<div class="chart-legend">' +
    items.map(([name, c]) => `<span><i style="background:${c}"></i>${esc(name)}</span>`).join('') + '</div>';
}

/** The alert chart shows the whole record; the selected period sits between two markers. */
function renderAlerts() {
  const el = document.getElementById('chart-alerts');
  const kinds = state.kind === 'all' ? KINDS : [state.kind];
  const sums = new Map();
  const rows = monthly.filter((d) => state.forestType === 'all' || d.forest_type === state.forestType);
  for (const d of byResolution(rows)) {
    const o = sums.get(d.x) || { loss_ha: 0, gain_ha: 0 };
    o.loss_ha += d.loss_ha;
    o.gain_ha += d.gain_ha;
    sums.set(d.x, o);
  }
  const xs = [...sums.keys()].sort();
  if (!xs.length) { el.innerHTML = '<p class="muted">No data.</p>'; return; }
  const yMax = Math.max(...xs.map((t) => kinds.reduce((s, k) => s + sums.get(t)[`${k}_ha`], 0))) || 1;
  const f = chartFrame(el, xs, yMax, 'new alert area (ha)');
  const bw = Math.max(1, f.band * 0.8);
  let g = f.g;
  xs.forEach((t, i) => {
    let base = 0;
    for (const k of kinds) {
      const v = sums.get(t)[`${k}_ha`];
      if (v > 0) {
        g += `<rect x="${f.x(i) - bw / 2}" y="${f.y(base + v)}" width="${bw}" height="${f.y(base) - f.y(base + v)}" ` +
          `fill="${KIND_COLORS[k]}"><title>${esc(t)} ${esc(KIND_LABELS[k])}: ${fmt(v, 2)} ha</title></rect>`;
      }
      base += v;
    }
  });
  const top = f.m.t, bottom = f.H - f.m.b;
  g += `<rect class="brush-out" data-side="left" y="${top}" height="${bottom - top}"/>` +
    `<rect class="brush-out" data-side="right" y="${top}" height="${bottom - top}"/>`;
  for (const [end, label] of [['from', 'Start of the period'], ['to', 'End of the period']]) {
    g += `<g class="brush-handle" data-end="${end}" tabindex="0" role="slider" aria-label="${label}" aria-valuemin="0" aria-valuemax="${xs.length}">` +
      `<rect class="hit" x="-10" y="${top}" width="20" height="${bottom - top}"/>` +
      `<line y1="${top}" y2="${bottom}"/><rect class="grip" x="-5" y="${(top + bottom) / 2 - 12}" width="10" height="24" rx="5"/></g>`;
  }
  el.innerHTML = legend(kinds.map((k) => [KIND_LABELS[k], KIND_COLORS[k]])) +
    `<svg viewBox="0 0 ${f.W} ${f.H}" role="group" aria-label="Alert area over time, with the period markers">${g}</svg>`;
  setupBrush(el.querySelector('svg'), xs, f);
}

/** Which bar a date falls in: the first at or after it (start) or the last at or before it (end). */
function periodIndex(xs, iso, end) {
  const key = iso.slice(0, state.resolution === 'monthly' ? 7 : 4);
  const i = end === 'from' ? xs.findIndex((x) => x >= key) : xs.findLastIndex((x) => x <= key);
  return i < 0 ? (end === 'from' ? xs.length - 1 : 0) : i;
}

let brushFocus = null; // the marker to refocus after a keyboard step re-renders the chart

function setupBrush(svg, xs, f) {
  const n = xs.length;
  const edge = (k) => f.m.l + k * f.band; // the boundary before bar k
  const span = () => [periodIndex(xs, state.from, 'from'), periodIndex(xs, state.to, 'to') + 1];
  const draw = () => {
    const [a, b] = span();
    const left = svg.querySelector('[data-side="left"]'), right = svg.querySelector('[data-side="right"]');
    left.setAttribute('x', String(edge(0)));
    left.setAttribute('width', String(Math.max(0, edge(a) - edge(0))));
    right.setAttribute('x', String(edge(b)));
    right.setAttribute('width', String(Math.max(0, edge(n) - edge(b))));
    for (const h of svg.querySelectorAll('.brush-handle')) {
      const from = h.dataset.end === 'from';
      h.setAttribute('transform', `translate(${edge(from ? a : b)},0)`);
      h.setAttribute('aria-valuenow', String(from ? a : b));
      h.setAttribute('aria-valuetext', fmtDate(ymd(from ? state.from : state.to)));
    }
  };
  const [lo, hi] = index.date_range;
  const clampDate = (iso) => (iso < lo ? lo : iso > hi ? hi : iso);
  const start = (x) => (x.length === 4 ? `${x}-01-01` : `${x}-01`);
  // Day 0 of the next month is the last of this one (month is 1-based here, 0-based in Date.UTC).
  const finish = (x) => (x.length === 4 ? `${x}-12-31`
    : new Date(Date.UTC(+x.slice(0, 4), +x.slice(5), 0)).toISOString().slice(0, 10));
  for (const h of svg.querySelectorAll('.brush-handle')) {
    const end = h.dataset.end;
    const set = (k) => {
      const [a, b] = span();
      if (end === 'from') state.from = clampDate(start(xs[Math.min(Math.max(k, 0), b - 1)]));
      else state.to = clampDate(finish(xs[Math.max(Math.min(k, n), a + 1) - 1]));
      setPeriodControls(null);
      draw();
    };
    h.addEventListener('pointerdown', (e) => { h.setPointerCapture(e.pointerId); h.classList.add('dragging'); });
    h.addEventListener('pointermove', (e) => {
      if (!h.hasPointerCapture(e.pointerId)) return;
      const r = svg.getBoundingClientRect();
      set(Math.round(((e.clientX - r.left) * (f.W / r.width) - f.m.l) / f.band));
    });
    h.addEventListener('lostpointercapture', () => { brushFocus = end; refresh(); });
    h.addEventListener('keydown', (e) => {
      const step = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
      if (!step) return;
      e.preventDefault();
      const [a, b] = span();
      set((end === 'from' ? a : b) + step);
      brushFocus = end;
      refresh();
    });
  }
  draw();
  if (brushFocus) {
    /** @type {SVGGElement} */ (svg.querySelector(`.brush-handle[data-end="${brushFocus}"]`)).focus();
    brushFocus = null;
  }
}

function renderSummary() {
  const rows = filteredMonthly();
  const total = (k) => rows.reduce((s, d) => s + d[`${k}_ha`], 0);
  // Candidates, not confirmed change: the monthly totals count every certainty.
  const cards = (state.kind === 'all' ? KINDS : [state.kind]).map((k) => ({
    label: KIND_LABELS[k], value: `${fmt(total(k), 2)} ha`,
    sub: 'unreviewed candidates first seen in the months selected, all certainties'
  }));
  document.getElementById('summary-cards').innerHTML = cards.map((c) =>
    `<div class="summary-card"><div class="summary-card__label">${esc(c.label)}</div>` +
    `<div class="summary-card__value">${esc(c.value)}</div><div class="summary-card__sub">${esc(c.sub)}</div></div>`).join('');
}

// ── chrome ───────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

// Theme: light by default, as in the RECOVER app; js/boot.js applies the
// stored choice before first paint.
function setupTheme() {
  const btn = document.getElementById('theme-toggle');
  const sync = () => {
    const dark = document.documentElement.dataset.theme === 'dark';
    btn.textContent = dark ? 'Light' : 'Dark';
    btn.setAttribute('aria-pressed', String(dark));
  };
  btn.addEventListener('click', () => {
    const dark = document.documentElement.dataset.theme !== 'dark';
    if (dark) document.documentElement.dataset.theme = 'dark';
    else delete document.documentElement.dataset.theme;
    try { localStorage.setItem('forestmon:theme', dark ? 'dark' : 'light'); } catch { /* private mode */ }
    sync(); // chart colours are CSS variables: nothing to redraw
  });
  sync();
}

function fmt(v, digits) {
  if (v === null || v === undefined || v === '' || Number.isNaN(Number(v))) return '—';
  return Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

// Dates: ISO strings in the controls, YYYYMMDD integers on the points.
const ymd = (iso) => Number(iso.replaceAll('-', ''));
function addDays(iso, n) {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}
function fmtDate(n) {
  const s = String(n);
  return new Date(`${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6)}T00:00:00Z`)
    .toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
}
const newCutoff = () => ymd(addDays(index.date_range[1], 1 - NEW_DAYS)); // as the 'Last 30 days' range
const isNew = () => ['>=', ['get', 'date'], newCutoff()];
function certainty(c) {
  // Tiers as confidence.LOW_MAX / MEDIUM_MAX.
  return `${c >= 0.67 ? 'high' : c >= 0.34 ? 'medium' : 'low'} (${fmt(c, 2)})`;
}

function showError(msg) {
  const el = document.getElementById('data-status');
  el.textContent = msg;
  el.classList.add('error');
  console.error(msg);
}

function showStatus() {
  const el = document.getElementById('data-status');
  el.textContent = `Data generated ${new Date(index.generated_at).toLocaleString()}`;
  el.classList.remove('error');
}

function setupStatus() {
  showStatus();
  const px = index.pixels;
  const n = (k) => index.forest_types.reduce((s, a) => s + px[a][k], 0);
  document.getElementById('footer-note').textContent =
    `${index.polygon_count} mapped stands · ${n('loss').toLocaleString()} tree-loss and ` +
    `${n('gain').toLocaleString()} regrowth alerts (10 m squares) · ${fmtDate(ymd(index.date_range[0]))} to ` +
    `${fmtDate(ymd(index.date_range[1]))}. Alerts are candidates for review, not confirmed change.`;
}

/** Write the period into the date fields; `preset` is the quick range to mark, null for a custom one. */
function setPeriodControls(preset) {
  /** @type {HTMLInputElement} */ (document.getElementById('filter-date-from')).value = state.from;
  /** @type {HTMLInputElement} */ (document.getElementById('filter-date-to')).value = state.to;
  for (const el of /** @type {NodeListOf<HTMLInputElement>} */ (document.querySelectorAll('input[name="period"]'))) {
    el.checked = el.value === preset;
  }
}

function setupPeriod() {
  const [lo, hi] = index.date_range;
  for (const id of ['filter-date-from', 'filter-date-to']) {
    const el = /** @type {HTMLInputElement} */ (document.getElementById(id));
    el.min = lo;
    el.max = hi;
  }
  const preset = (v) => {
    state.from = v === 'all' ? lo : [addDays(hi, 1 - Number(v)), lo].sort()[1];
    state.to = hi;
    setPeriodControls(v);
  };
  const checked = /** @type {HTMLInputElement} */ (document.querySelector('input[name="period"]:checked'));
  const [f0, t0] = ['filter-date-from', 'filter-date-to'].map((id) => /** @type {HTMLInputElement} */ (document.getElementById(id)).value);
  if (!checked && f0 && t0 && lo <= f0 && f0 <= t0 && t0 <= hi) { state.from = f0; state.to = t0; } // restored on reload
  else preset(checked?.value || 'all');
  for (const el of document.querySelectorAll('input[name="period"]')) {
    el.addEventListener('change', (e) => {
      const v = /** @type {HTMLInputElement} */ (e.target).value;
      preset(v);
      // A year or less is one or two bars by year: show months; the whole record by year.
      /** @type {HTMLSelectElement} */ (document.getElementById('chart-group')).value = state.resolution = v === 'all' ? 'annual' : 'monthly';
      refresh();
    });
  }
  for (const [id, key] of [['filter-date-from', 'from'], ['filter-date-to', 'to']]) {
    document.getElementById(id).addEventListener('change', (e) => {
      const v = /** @type {HTMLInputElement} */ (e.target).value;
      // An emptied field means the record's end; a crossed range moves the other end.
      state[key] = v === '' ? (key === 'from' ? lo : hi) : v < lo ? lo : v > hi ? hi : v;
      if (state.from > state.to) state[key === 'from' ? 'to' : 'from'] = state[key];
      setPeriodControls(null);
      refresh();
    });
  }
  document.getElementById('period-note').textContent =
    `Newest alert: ${fmtDate(ymd(hi))}. Quick ranges count back from it.`;
}

/** The "i" buttons: the layer's plain-words description, with its legend, in a dialog. */
function setupInfo() {
  const dlg = /** @type {HTMLDialogElement} */ (document.getElementById('info'));
  for (const btn of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll('[data-info]'))) {
    btn.addEventListener('click', () => {
      const t = /** @type {HTMLTemplateElement} */ (document.getElementById(`info-${btn.dataset.info}`));
      document.getElementById('info-title').textContent = t.dataset.title;
      const body = document.getElementById('info-body');
      body.replaceChildren();
      const keys = btn.closest('.layer-group')?.querySelectorAll('.key, .ramp, .ramp-labels') || [];
      if (keys.length) {
        const box = document.createElement('div');
        box.className = 'info-legend';
        for (const k of keys) {
          const c = /** @type {HTMLElement} */ (k.cloneNode(true));
          c.removeAttribute('id');
          c.hidden = false; // e.g. the radar key, hidden while no layer is configured
          box.append(c);
        }
        body.append(box);
      }
      body.append(t.content.cloneNode(true));
      dlg.showModal();
    });
  }
  document.getElementById('info-close').addEventListener('click', () => dlg.close());
  dlg.addEventListener('click', (e) => { if (e.target === dlg) dlg.close(); });
}

function setupControls() {
  setupPeriod();
  const bind = (id, key, parse = (v) => v) => {
    document.getElementById(id).addEventListener('change', (e) => {
      state[key] = parse(/** @type {HTMLInputElement} */ (e.target).value);
      refresh();
    });
  };
  bind('filter-forest-type', 'forestType');
  bind('filter-alert-type', 'kind');
  bind('filter-confidence', 'minConf', Number);
  bind('chart-group', 'resolution');
  bind('stand-color', 'standColor');
  document.getElementById('basemap-options').innerHTML = Object.entries(BASEMAPS).map(([id, b]) =>
    `<label class="seg__opt"><input type="radio" name="basemap" value="${esc(id)}"${id === state.basemap ? ' checked' : ''}>` +
    `<span>${esc(b.short || b.label || id)}</span></label>`).join('');
  const link = storedRaddLink();
  if (link) raddTiles = linkTiles(link);
  describeRadd();
  setupRaddLink();
  for (const el of document.querySelectorAll('input[name="radd-mode"]')) {
    el.addEventListener('change', (e) => {
      state.raddMode = /** @type {HTMLInputElement} */ (e.target).value;
      applyRadd();
    });
  }
  // Browsers may restore form state on reload; the controls are the truth.
  const val = (id) => /** @type {HTMLInputElement} */ (document.getElementById(id));
  state.forestType = val('filter-forest-type').value;
  state.kind = val('filter-alert-type').value;
  state.minConf = Number(val('filter-confidence').value);
  state.resolution = val('chart-group').value;
  state.standColor = val('stand-color').value;
  state.alerts = val('toggle-alerts').checked;
  state.stands = val('toggle-stands').checked;
  state.cells = val('toggle-cells').checked;
  state.c2c = val('toggle-c2c').checked;
  state.c2cWeak = val('c2c-weak').checked;
  state.radd = val('toggle-radd').checked;
  state.raddMode = /** @type {HTMLInputElement} */ (document.querySelector('input[name="radd-mode"]:checked')).value;
  if (window.innerWidth < 700) /** @type {HTMLDetailsElement} */ (document.getElementById('layer-panel')).open = false;
  for (const k of KINDS) {
    for (const el of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll(`.sw--${k}`))) el.style.background = KIND_COLORS[k];
  }
  for (const el of /** @type {NodeListOf<HTMLElement>} */ (document.querySelectorAll('.sw--new'))) {
    el.style.background = '#ffffff';
    el.style.border = `2.5px solid ${NEW_COLOR}`;
  }
  document.getElementById('c2c-key').innerHTML = C2C_YEARS.map((y, i) =>
    `<span><i class="sw" style="background:${C2C_COLORS[i]}"></i>${y}</span>`).join('');
  document.getElementById('cell-ramp').style.background =
    `linear-gradient(90deg, ${CELL_RAMP.map(([c]) => c).join(', ')})`;
  for (const el of document.querySelectorAll('input[name="basemap"]')) {
    el.addEventListener('change', (e) => {
      state.basemap = /** @type {HTMLInputElement} */ (e.target).value;
      applyMapState();
    });
  }
  for (const el of /** @type {NodeListOf<HTMLInputElement>} */ (document.querySelectorAll('input[data-opacity]'))) {
    state.opacity[el.dataset.opacity] = Number(el.value) / 100; // restored on reload
    el.addEventListener('input', () => {
      state.opacity[el.dataset.opacity] = Number(el.value) / 100;
      applyOpacity();
    });
  }
  for (const [id, key] of [['toggle-alerts', 'alerts'], ['toggle-radd', 'radd'], ['toggle-stands', 'stands'], ['toggle-cells', 'cells'],
    ['toggle-c2c', 'c2c'], ['c2c-weak', 'c2cWeak']]) {
    document.getElementById(id).addEventListener('change', (e) => {
      state[key] = /** @type {HTMLInputElement} */ (e.target).checked;
      refreshMap().catch((err) => showError(`Map update failed: ${err.message}`));
    });
  }
}

function renderCharts() {
  renderAlerts();
  renderSummary();
}

function refresh() {
  renderCharts();
  refreshMap().catch((e) => showError(`Map update failed: ${e.message}`));
}

/** A failed start: a plain sentence on the map, the technical cause in the status pill and console. */
function showFatal(e) {
  showError(`Error loading data: ${e.message}`);
  const p = document.createElement('p');
  p.className = 'map-error';
  p.textContent = 'The map data could not be loaded. Try reloading the page; if it keeps happening, ' +
    'tell whoever runs this dashboard.';
  document.querySelector('.map-wrap').append(p);
}

/** Tile layers read from S3 fail quietly in MapLibre: say so in their section. */
function setupLayerErrors() {
  map.on('error', (e) => {
    const section = { c2c: 'c2c-section', cells: 'cells-section' }[e.sourceId];
    const el = section && document.getElementById(section);
    if (!el || el.querySelector('.layer-error')) return;
    const p = document.createElement('p');
    p.className = 'hint layer-error';
    p.textContent = 'This layer could not be loaded; try again later.';
    el.append(p);
    console.error(e.error);
  });
}

async function init() {
  // The buttons work even if the data never arrive.
  setupTheme();
  setupInfo();
  if (typeof maplibregl === 'undefined') throw new Error('MapLibre failed to load (vendor/ and unpkg both unavailable)');
  index = await loadIndex();
  const [polygons, rows, radd] = await Promise.all([
    fetchJson(index.files.polygons),
    fetchJson(index.files.monthly),
    loadRaddTiles(),
    createMap()
  ]);
  monthly = rows;
  raddTiles = raddFile = radd;
  setupStatus();
  setupControls();
  setupLayerErrors();
  addPolygons(polygons);
  addConditionCells();
  addC2c();
  setupPopup();
  setupDetail(polygons);
  refresh();
  let pending = 0;
  new ResizeObserver(() => {
    cancelAnimationFrame(pending);
    pending = requestAnimationFrame(renderCharts);
  }).observe(document.querySelector('.panel--charts'));
}

init().catch(showFatal);
