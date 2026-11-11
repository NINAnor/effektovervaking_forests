// @ts-check
// The dashboard. Classic script, loaded last: reads DASHBOARD_CONFIG, fetches
// the JSON that `forestmon publish dashboard` bakes, draws map and charts.

const CFG = window.DASHBOARD_CONFIG || {};
const BASE = (CFG.dataBase || 'data/').replace(/\/?$/, '/');
const SCHEMA = 1; // must match dashboard.SCHEMA in Python
const FOREST_COLORS = { olivinskog: '#6aa84f', kalklindeskog: '#d6b656' };
const KIND_COLORS = { loss: '#e06666', gain: '#4dd0a7' };
const KINDS = ['loss', 'gain'];

const state = {
  forestType: 'all',
  kind: 'loss',
  minConf: 0,
  from: '',
  to: '',
  resolution: 'annual'
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
      sources: {
        base: {
          type: 'raster',
          tiles: [CFG.basemap || 'https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
          tileSize: 256,
          attribution: CFG.basemapAttribution || '&copy; OpenStreetMap contributors'
        }
      },
      layers: [{ id: 'base', type: 'raster', source: 'base' }]
    },
    center: [9.5, 60.5],
    zoom: 5.5
  });
  map.addControl(new maplibregl.NavigationControl(), 'top-right');
  return new Promise((resolve) => map.on('load', resolve));
}

function addPolygons(geojson) {
  const color = ['match', ['get', 'forest_type'],
    'olivinskog', FOREST_COLORS.olivinskog, 'kalklindeskog', FOREST_COLORS.kalklindeskog, '#999'];
  map.addSource('forest', { type: 'geojson', data: geojson });
  map.addLayer({ id: 'forest-fill', type: 'fill', source: 'forest', paint: { 'fill-color': color, 'fill-opacity': 0.3 } });
  map.addLayer({ id: 'forest-line', type: 'line', source: 'forest', paint: { 'line-color': color, 'line-width': 1 } });
  hoverPopup('forest-fill', (p) =>
    `<strong>${esc(p.forest_type)}</strong><br>${esc(p.naturtype || p.layer || '')}<br>${fmt(p.area_ha, 2)} ha`);
  fitTo(geojson);
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
      hoverPopup(key, (p) =>
        `<strong>${esc(p.kind)}</strong> &mdash; ${esc(p.aoi)}<br>latest break ${esc(p.month)}<br>` +
        `confidence ${fmt(p.conf, 2)}, ${p.n} event${p.n === 1 ? '' : 's'}`);
      return key;
    }).catch((e) => {
      pointLayers.delete(key); // retry on the next refresh
      throw e;
    }));
  }
  return pointLayers.get(key);
}

function hoverPopup(layer, html) {
  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
  map.on('mousemove', layer, (e) => {
    map.getCanvas().style.cursor = 'pointer';
    popup.setLngLat(e.lngLat).setHTML(html(e.features[0].properties)).addTo(map);
  });
  map.on('mouseleave', layer, () => {
    map.getCanvas().style.cursor = '';
    popup.remove();
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
  map.setFilter('forest-fill', forest);
  map.setFilter('forest-line', forest);
  const wanted = new Set(wantedLayers().map(({ aoi, kind }) => `${aoi}|${kind}`));
  const filter = ['all',
    ['>=', ['get', 'month'], state.from],
    ['<=', ['get', 'month'], state.to],
    ['>=', ['get', 'conf'], state.minConf]];
  for (const key of pointLayers.keys()) {
    if (!map.getLayer(key)) continue;
    map.setLayoutProperty(key, 'visibility', wanted.has(key) ? 'visible' : 'none');
    map.setFilter(key, filter);
  }
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

function fmt(v, digits) {
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
  const [polygons, rows] = await Promise.all([
    fetchJson(index.files.polygons),
    fetchJson(index.files.monthly),
    createMap()
  ]);
  monthly = rows;
  setupStatus();
  setupControls();
  addPolygons(polygons);
  refresh();
  let pending = 0;
  new ResizeObserver(() => {
    cancelAnimationFrame(pending);
    pending = requestAnimationFrame(renderCharts);
  }).observe(document.querySelector('.panel--charts'));
}

init().catch((e) => showError(`Error loading data: ${e.message}`));
