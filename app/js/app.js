// @ts-check
// The dashboard. Classic script, loaded last: reads DASHBOARD_CONFIG, fetches
// the JSON that `forestmon publish dashboard` bakes, draws map and charts.

const CFG = window.DASHBOARD_CONFIG || {};
const BASE = (CFG.dataBase || 'data/').replace(/\/?$/, '/');
const SCHEMA = 3; // must match dashboard.SCHEMA in Python
const FOREST_COLORS = { olivinskog: '#6aa84f', kalklindeskog: '#d6b656' };
const KIND_COLORS = { loss: '#e06666', gain: '#4dd0a7' };
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
const TRAJ_COLORS = { improving: '#35978f', stable: '#9aa39c', fluctuating: '#c8ccc2', declining: '#bf812d' };
// Cells: (score - threshold) x 1000, brown below the threshold, blue-green above.
const CELL_RAMP = [['#8c510a', -90], ['#bf812d', -60], ['#dfc27d', -30], ['#f6e8c3', 0],
  ['#c7eae5', 30], ['#80cdc1', 60], ['#35978f', 90], ['#01665e', null]];

const state = {
  forestType: 'all',
  kind: 'loss',
  minConf: 0,
  from: '',
  to: '',
  resolution: 'annual',
  basemap: Object.keys(BASEMAPS)[0], // the first configured is the default
  alerts: true,
  radd: false,
  raddMode: 'alert',
  stands: true,
  standColor: 'forest_type',
  cells: false
};

/** @type {any} */ let index;
/** @type {any[]} */ let monthly = [];
/** @type {any} */ let map;
/** Point layers, keyed `${aoi}|${kind}`, as promises so a key loads once. */
const pointLayers = new Map();

// ── data ─────────────────────────────────────────────────────────────────────
async function fetchJson(name) {
  const url = BASE + name;
  const r = await fetch(url, { cache: 'no-store' });
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
  for (const k of ['lat', 'month', 'conf', 'n']) {
    if (!cols[k] || cols[k].length !== n) throw new Error(`points ${aoi}/${kind}: column ${k} has the wrong length`);
  }
  const features = new Array(n);
  for (let i = 0; i < n; i++) {
    features[i] = {
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [cols.lon[i], cols.lat[i]] },
      properties: { aoi, kind, month: cols.month[i], conf: cols.conf[i], n: cols.n[i] }
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
    paint: { 'fill-color': color, 'fill-opacity': ['interpolate', ['linear'], ['zoom'], 8, 0.6, 14, 0.2] }
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
      'circle-radius': ['interpolate', ['linear'], ['zoom'], 4, 3, 10, 5],
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

function addConditionCells() {
  const url = cellsUrl();
  if (!url || typeof pmtiles === 'undefined') {
    if (url) console.error('pmtiles.js failed to load; condition cells are off');
    document.getElementById('cells-section').hidden = true;
    return;
  }
  const protocol = new pmtiles.Protocol();
  maplibregl.addProtocol('pmtiles', protocol.tile);
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
      'fill-opacity': ['interpolate', ['linear'], ['zoom'], 12, 0.5, 14, 0.85]
    }
  }, 'forest-line');
}

function score(v) {
  return v === null || v === undefined || v === '' ? 'no score' : fmt(v, 3);
}

// One popup for the whole map: the topmost kind of feature under the cursor wins.
const POPUPS = [
  [(id) => pointLayers.has(id), (p) =>
    `<strong>${esc(p.kind)}</strong> &mdash; ${esc(p.aoi)}<br>latest break ${esc(p.month)}<br>` +
    `confidence ${fmt(p.conf, 2)}, ${p.n} event${p.n === 1 ? '' : 's'}`],
  [(id) => id === 'cells', (p) =>
    `<strong>10 m cell</strong><br>condition 2025: ${score(p.dor_global_2025)} ` +
    `(threshold ${fmt(p.threshold_global, 3)})<br>` +
    `stand: ${esc(CLASS_LABELS[p.stand_condition_class] || '—')}, trajectory ${esc(p.trajectory_class || '—')}<br>` +
    `${fmt(p.edge_m, 0)} m inside the stand`],
  [(id) => id === 'forest-fill' || id === 'forest-pts', (p) =>
    `<strong>${esc(p.forest_type)}</strong> &mdash; ${esc(p.layer)}<br>` +
    `${fmt(p.area_ha, 2)} ha, surveyed ${esc(p.survey_year ?? '—')} (${esc(p.tilstand || 'no field condition')})<br>` +
    `condition 2025: ${score(p.dor_global_2025)}, ${esc(CLASS_LABELS[p.condition_local] || 'no score')} (local) / ` +
    `${esc(CLASS_LABELS[p.condition_global] || 'no score')} (global)<br>trajectory ${esc(p.trajectory || '—')}`]
];

/** @type {any} */ let popup;

function setupPopup() {
  popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, maxWidth: '320px' });
  map.getCanvas().addEventListener('mouseleave', () => popup.remove());
  map.on('mousemove', (e) => {
    if (!document.getElementById('detail').hidden) { popup.remove(); return; }
    const layers = ['forest-fill', 'forest-pts', 'cells', ...pointLayers.keys()].filter((id) => map.getLayer(id));
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
      map.addSource(key, { type: 'geojson', data: pointsToGeojson(cols, aoi, kind) });
      map.addLayer({
        id: key,
        type: 'circle',
        source: key,
        paint: {
          'circle-color': KIND_COLORS[kind],
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 6, 1.5, 12, 3, 16, 6],
          'circle-opacity': 0.8
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
/** radd_tiles.json, or null when it has not been minted. */
let raddTiles = null;

async function loadRaddTiles() {
  try {
    return await fetchJson('radd_tiles.json');
  } catch {
    return null; // optional layer
  }
}

function applyRadd() {
  if (!raddTiles) return;
  const tiles = [raddTiles.layers[state.raddMode].tiles];
  if (!map.getSource('radd')) {
    if (!state.radd) return; // nothing is fetched until it is switched on
    map.addSource('radd', {
      type: 'raster', tiles, tileSize: 256, maxzoom: 15,
      attribution: '<a href="https://www.wur.nl/en/research-results/research-institutes/environmental-research/radd-forest-disturbance-alert.htm">RADD</a> / Wageningen University'
    });
    map.addLayer({ id: 'radd', type: 'raster', source: 'radd', paint: { 'raster-opacity': 0.9 } }, 'forest-pts');
    map.on('error', (e) => {
      if (e.sourceId === 'radd') {
        document.getElementById('radd-note').textContent =
          'RADD tiles failed to load. Earth Engine map IDs expire: re-run forestmon publish radd.';
      }
    });
  } else if (map.getSource('radd').tiles?.[0] !== tiles[0]) {
    map.getSource('radd').setTiles(tiles);
  }
  map.setLayoutProperty('radd', 'visibility', state.radd ? 'visible' : 'none');
}

function describeRadd() {
  const days = (Date.now() - raddTiles.minted_at) / 864e5;
  document.getElementById('radd-note').textContent =
    `Image ${raddTiles.image_id} (v${raddTiles.version_date}), tiles made ${fmt(days, 0)} d ago` +
    (days > 3 ? ' — they may have expired; re-run forestmon publish radd.' : '.');
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
  for (const yr of [years[0], years[4], years[years.length - 1]]) {
    g += `<text x="${x(yr)}" y="${H - 5}" text-anchor="middle">${yr}</text>`;
  }
  if (markYear) g += `<line class="mark" x1="${x(markYear)}" x2="${x(markYear)}" y1="${m.t}" y2="${H - m.b}"><title>surveyed ${markYear}</title></line>`;
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
  return `<a class="ext-link" href="${esc(url)}" target="_blank" rel="noopener">Sentinel-2 NBR time series &#8599;</a>`;
}

function chip(text, color) {
  return `<span class="chip"><i style="background:${color}"></i>${esc(text)}</span>`;
}

function cellSection(p) {
  const series = index.years.map((yr) => p[`dor_global_${yr}`] ?? null);
  const traj = p.trajectory_class || '—';
  return '<section><h4>10 m cell condition</h4>' +
    trajChart(series, p.threshold_global, index.years) +
    '<div class="chips">' +
    chip(`2025: ${Number(p.dor_global_2025) >= Number(p.threshold_global) ? 'above' : 'below'} threshold`,
      Number(p.dor_global_2025) >= Number(p.threshold_global) ? CLASS_COLORS.good_condition : CLASS_COLORS.poor_condition) +
    chip(`trajectory: ${traj}`, TRAJ_COLORS[traj] || CLASS_COLORS.no_data) + '</div>' +
    `<dl><dt>Score 2025</dt><dd>${score(p.dor_global_2025)}</dd><dt>Threshold</dt><dd>${fmt(p.threshold_global, 3)}</dd>` +
    `<dt>Trend 2017–2025</dt><dd>${trend(series)}</dd><dt>Inside stand</dt><dd>${fmt(p.edge_m, 0)} m</dd></dl></section>`;
}

function standSection(p) {
  const local = p.condition_local || 'no_data', global = p.condition_global || 'no_data';
  return `<section><h4>Stand: ${esc(p.forest_type)} &middot; ${esc(p.layer)}</h4>` +
    trajChart(p.series || [], p.threshold_global, index.years, p.survey_year) +
    '<div class="chips">' +
    chip(`local: ${CLASS_LABELS[local]}`, CLASS_COLORS[local]) +
    chip(`global: ${CLASS_LABELS[global]}`, CLASS_COLORS[global]) +
    chip(`trajectory: ${p.trajectory || '—'}`, TRAJ_COLORS[p.trajectory] || CLASS_COLORS.no_data) + '</div>' +
    `<dl><dt>Area</dt><dd>${fmt(p.area_ha, 2)} ha</dd><dt>Surveyed</dt><dd>${esc(p.survey_year ?? '—')}</dd>` +
    `<dt>Field condition</dt><dd>${esc(p.tilstand || '—')}</dd><dt>Score 2025</dt><dd>${score(p.dor_global_2025)}</dd>` +
    `<dt>Trend 2017–2025</dt><dd>${trend(p.series || [])}</dd></dl></section>`;
}

function showDetail(e) {
  const at = (ids) => map.queryRenderedFeatures(e.point, { layers: ids.filter((id) => map.getLayer(id)) });
  const sccd = at([...pointLayers.keys()]);
  const cell = at(['cells'])[0];
  const stand = at(['forest-fill', 'forest-pts'])[0];
  let html = '';
  for (const f of sccd.slice(0, 3)) {
    const p = f.properties;
    html += `<section><h4>S-CCD ${esc(p.kind)} pixel</h4><div class="chips">${chip(p.kind, KIND_COLORS[p.kind])}</div>` +
      `<dl><dt>Latest break</dt><dd>${esc(p.month)}</dd><dt>Confidence</dt><dd>${fmt(p.conf, 2)}</dd>` +
      `<dt>Events</dt><dd>${esc(p.n)}</dd></dl>${timeseriesLink(f.geometry.coordinates)}</section>`;
  }
  if (cell) html += cellSection(cell.properties);
  if (stand) html += standSection(standById.get(stand.properties.poly_id) || stand.properties);
  const card = document.getElementById('detail');
  if (!html) { card.hidden = true; setMarker(null); return; }
  document.getElementById('detail-title').textContent =
    `${fmt(e.lngLat.lat, 5)}° N, ${fmt(e.lngLat.lng, 5)}° E`;
  document.getElementById('detail-body').innerHTML = html;
  card.hidden = false;
  if (popup) popup.remove();
  setMarker(e.lngLat);
}

function setMarker(lngLat) {
  const data = { type: 'FeatureCollection', features: lngLat
    ? [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [lngLat.lng, lngLat.lat] } }] : [] };
  if (map.getSource('sel')) { map.getSource('sel').setData(data); return; }
  map.addSource('sel', { type: 'geojson', data });
  map.addLayer({
    id: 'sel', type: 'circle', source: 'sel',
    paint: { 'circle-radius': 9, 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': '#fff', 'circle-stroke-width': 2.5 }
  });
}

function setupDetail(polygons) {
  for (const f of polygons.features) standById.set(f.properties.poly_id, f.properties);
  map.on('click', showDetail);
  document.getElementById('detail-close').addEventListener('click', () => {
    document.getElementById('detail').hidden = true;
    setMarker(null);
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
  document.getElementById('stand-count').textContent = String(n);
  const wanted = new Set(wantedLayers().map(({ aoi, kind }) => `${aoi}|${kind}`));
  const filter = ['all',
    ['>=', ['get', 'month'], state.from],
    ['<=', ['get', 'month'], state.to],
    ['>=', ['get', 'conf'], state.minConf]];
  for (const key of pointLayers.keys()) {
    if (!map.getLayer(key)) continue;
    map.setLayoutProperty(key, 'visibility', state.alerts && wanted.has(key) ? 'visible' : 'none');
    map.setFilter(key, filter);
  }
  applyRadd();
}

let mapGeneration = 0;

async function refreshMap() {
  const gen = ++mapGeneration;
  applyMapState();
  const results = await Promise.allSettled(wantedLayers().map(({ aoi, kind }) => ensurePoints(aoi, kind)));
  // The state may have changed while files loaded; apply the current one.
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
    d.period >= state.from && d.period <= state.to);
}

/** Months -> years when annual: cover is the December (or last) value, areas are summed. */
function byResolution(rows) {
  if (state.resolution === 'monthly') return rows.map((d) => ({ ...d, x: d.period }));
  const out = new Map();
  for (const d of rows) {
    const key = `${d.forest_type}|${d.period.slice(0, 4)}`;
    const o = out.get(key) || { forest_type: d.forest_type, x: d.period.slice(0, 4), loss_ha: 0, gain_ha: 0 };
    o.loss_ha += d.loss_ha;
    o.gain_ha += d.gain_ha;
    o.area_ha = d.area_ha; // rows are in month order, so this ends on the last month
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
  return { W, H, x, y, g, band };
}

function legend(items) {
  return '<div class="chart-legend">' +
    items.map(([name, c]) => `<span><i style="background:${c}"></i>${esc(name)}</span>`).join('') + '</div>';
}

function renderCover() {
  const el = document.getElementById('chart-cover');
  const rows = byResolution(filteredMonthly());
  const xs = [...new Set(rows.map((d) => d.x))].sort();
  if (!xs.length) { el.innerHTML = '<p class="muted">No data in range.</p>'; return; }
  const types = [...new Set(rows.map((d) => d.forest_type))];
  const f = chartFrame(el, xs, Math.max(...rows.map((d) => d.area_ha)), 'area (ha)');
  let g = f.g;
  for (const t of types) {
    const pts = rows.filter((d) => d.forest_type === t).map((d) => [f.x(xs.indexOf(d.x)), f.y(d.area_ha)]);
    g += `<polyline fill="none" stroke="${FOREST_COLORS[t]}" stroke-width="2" points="${pts.join(' ')}"/>`;
    // Dots too, so a one-point range still shows something.
    if (pts.length <= 24) g += pts.map(([px, py]) => `<circle cx="${px}" cy="${py}" r="2.5" fill="${FOREST_COLORS[t]}"/>`).join('');
  }
  el.innerHTML = legend(types.map((t) => [t, FOREST_COLORS[t]])) +
    `<svg viewBox="0 0 ${f.W} ${f.H}" role="img" aria-label="Forest cover over time">${g}</svg>`;
}

function renderAlerts() {
  const el = document.getElementById('chart-alerts');
  const kinds = state.kind === 'all' ? KINDS : [state.kind];
  const sums = new Map();
  for (const d of byResolution(filteredMonthly())) {
    const o = sums.get(d.x) || { loss_ha: 0, gain_ha: 0 };
    o.loss_ha += d.loss_ha;
    o.gain_ha += d.gain_ha;
    sums.set(d.x, o);
  }
  const xs = [...sums.keys()].sort();
  if (!xs.length) { el.innerHTML = '<p class="muted">No data in range.</p>'; return; }
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
          `fill="${KIND_COLORS[k]}"><title>${esc(t)} ${k}: ${fmt(v, 2)} ha</title></rect>`;
      }
      base += v;
    }
  });
  el.innerHTML = legend(kinds.map((k) => [k, KIND_COLORS[k]])) +
    `<svg viewBox="0 0 ${f.W} ${f.H}" role="img" aria-label="Alert area over time">${g}</svg>`;
}

function renderSummary() {
  const rows = filteredMonthly();
  const total = (k) => rows.reduce((s, d) => s + d[k], 0);
  const latest = {};
  for (const d of rows) latest[d.forest_type] = d;
  const cover = (t) => latest[t]
    ? { value: `${fmt(latest[t].area_ha, 1)} ha`, sub: `as of ${latest[t].period}` }
    : { value: '—', sub: '' };
  const cards = [
    { label: 'New loss area', value: `${fmt(total('loss_ha'), 2)} ha`, sub: 'selected period, by first break' },
    { label: 'New gain area', value: `${fmt(total('gain_ha'), 2)} ha`, sub: 'selected period, by first break' },
    { label: 'Olivinskog cover', ...cover('olivinskog') },
    { label: 'Kalklindeskog cover', ...cover('kalklindeskog') }
  ];
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
    sync();
    renderCharts();
  });
  sync();
}

function fmt(v, digits) {
  if (v === null || v === undefined || v === '' || Number.isNaN(Number(v))) return '—';
  return Number(v).toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
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
    `${index.polygon_count} mapped polygons · ${n('loss').toLocaleString()} loss and ` +
    `${n('gain').toLocaleString()} gain pixels (10 m) · ${index.period_range[0]} to ${index.period_range[1]}. ` +
    'Cover is the mapped area minus cumulative loss; gain is not added back.';
}

function setupControls() {
  const [lo, hi] = index.period_range;
  for (const id of ['filter-date-from', 'filter-date-to']) {
    const el = /** @type {HTMLInputElement} */ (document.getElementById(id));
    el.min = lo;
    el.max = hi;
  }
  state.from = lo;
  state.to = hi;
  /** @type {HTMLInputElement} */ (document.getElementById('filter-date-from')).value = lo;
  /** @type {HTMLInputElement} */ (document.getElementById('filter-date-to')).value = hi;

  const bind = (id, key, parse = (v) => v) => {
    document.getElementById(id).addEventListener('change', (e) => {
      const v = /** @type {HTMLInputElement} */ (e.target).value;
      // An emptied month input would filter everything out; fall back to the range ends.
      state[key] = v === '' ? (key === 'from' ? lo : key === 'to' ? hi : state[key]) : parse(v);
      refresh();
    });
  };
  bind('filter-forest-type', 'forestType');
  bind('filter-alert-type', 'kind');
  bind('filter-confidence', 'minConf', Number);
  bind('filter-date-from', 'from');
  bind('filter-date-to', 'to');
  bind('cover-resolution', 'resolution');
  bind('stand-color', 'standColor');
  document.getElementById('basemap-options').innerHTML = Object.entries(BASEMAPS).map(([id, b]) =>
    `<label class="seg__opt"><input type="radio" name="basemap" value="${esc(id)}"${id === state.basemap ? ' checked' : ''}>` +
    `<span>${esc(b.short || b.label || id)}</span></label>`).join('');
  if (!raddTiles) document.getElementById('radd-section').hidden = true;
  else describeRadd();
  for (const el of document.querySelectorAll('input[name="radd-mode"]')) {
    el.addEventListener('change', (e) => {
      state.raddMode = /** @type {HTMLInputElement} */ (e.target).value;
      applyRadd();
    });
  }
  setupTheme();
  // Browsers may restore form state on reload; the controls are the truth.
  const val = (id) => /** @type {HTMLInputElement} */ (document.getElementById(id));
  state.forestType = val('filter-forest-type').value;
  state.kind = val('filter-alert-type').value;
  state.minConf = Number(val('filter-confidence').value);
  state.resolution = val('cover-resolution').value;
  state.standColor = val('stand-color').value;
  state.alerts = val('toggle-alerts').checked;
  state.stands = val('toggle-stands').checked;
  state.cells = val('toggle-cells').checked;
  state.radd = val('toggle-radd').checked;
  state.raddMode = /** @type {HTMLInputElement} */ (document.querySelector('input[name="radd-mode"]:checked')).value;
  if (window.innerWidth < 700) /** @type {HTMLDetailsElement} */ (document.getElementById('layer-panel')).open = false;
  document.getElementById('cell-ramp').style.background =
    `linear-gradient(90deg, ${CELL_RAMP.map(([c]) => c).join(', ')})`;
  for (const el of document.querySelectorAll('input[name="basemap"]')) {
    el.addEventListener('change', (e) => {
      state.basemap = /** @type {HTMLInputElement} */ (e.target).value;
      applyMapState();
    });
  }
  for (const [id, key] of [['toggle-alerts', 'alerts'], ['toggle-radd', 'radd'], ['toggle-stands', 'stands'], ['toggle-cells', 'cells']]) {
    document.getElementById(id).addEventListener('change', (e) => {
      state[key] = /** @type {HTMLInputElement} */ (e.target).checked;
      refreshMap().catch((err) => showError(`Map update failed: ${err.message}`));
    });
  }
}

function renderCharts() {
  renderCover();
  renderAlerts();
  renderSummary();
}

function refresh() {
  renderCharts();
  refreshMap().catch((e) => showError(`Map update failed: ${e.message}`));
}

async function init() {
  if (typeof maplibregl === 'undefined') throw new Error('MapLibre failed to load (vendor/ and unpkg both unavailable)');
  index = await loadIndex();
  const [polygons, rows, radd] = await Promise.all([
    fetchJson(index.files.polygons),
    fetchJson(index.files.monthly),
    loadRaddTiles(),
    createMap()
  ]);
  monthly = rows;
  raddTiles = radd;
  setupStatus();
  setupControls();
  addPolygons(polygons);
  addConditionCells();
  setupPopup();
  setupDetail(polygons);
  refresh();
  let pending = 0;
  new ResizeObserver(() => {
    cancelAnimationFrame(pending);
    pending = requestAnimationFrame(renderCharts);
  }).observe(document.querySelector('.panel--charts'));
}

init().catch((e) => showError(`Error loading data: ${e.message}`));
