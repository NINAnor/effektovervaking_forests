"""Write docs/report-assets/map/stand_map.md: the interactive stand map for the HTML report.

The 737 stands (condition.parquet, 2025; trajectory.parquet; polygons_geo.parquet)
as simplified GeoJSON in a Leaflet map. Included by the .qmd for HTML output only;
the PDF and Word versions keep the static Figure 1.

    python docs/report-build/make_stand_map.py

Reads the condition outputs from CONDITION_OUT_DATA (default: data/condition/outputs/data under the
repository root; the same files are in the S3 bucket under condition/parquet/). When they are not
there, the committed assets/map/stand_map.md is used as it is.
"""
import json
from pathlib import Path

import os
import shutil

HERE = Path(__file__).resolve().parent          # docs/report-build
OUT = HERE.parent / "report-assets"             # docs/report-assets: what the .qmd sources read
DATA = Path(os.environ.get("CONDITION_OUT_DATA", HERE.parents[1] / "data" / "condition" / "outputs" / "data"))

CLASSES = {  # key -> (label, colour)
    "good_condition": ("Good", "#2e7d32"),
    "indistinguishable": ("Indistinguishable", "#f9a825"),
    "poor_condition": ("Poor", "#c62828"),
}
TRAJ = {"declining": ("Declining", "#c62828"), "fluctuating": ("Fluctuating", "#f9a825"),
        "stable": ("Stable", "#9aa39a"), "improving": ("Improving", "#1565c0")}
TYPES = {"kalklindeskog": ("Kalklindeskog", "#7b1fa2"), "olivinskog": ("Olivinskog", "#ef6c00")}
TILSTAND = {"god": ("God", "#2e7d32"), "moderat": ("Moderat", "#f9a825"), "dårlig": ("Dårlig", "#ef6c00"),
            "sværtRedusert": ("Svært redusert", "#c62828"), "none": ("No field survey", "#b9bcae")}


def encode_values(vals) -> str:
    """Google polyline encoding of integer deltas (compact text for the outlines)."""
    out = []
    for d in vals:
        v = ~(d << 1) if d < 0 else d << 1
        while v >= 32:
            out.append(chr((v & 31 | 32) + 63))
            v >>= 5
        out.append(chr(v + 63))
    return "".join(out)


def encode_ring(ring) -> str:
    lat = lng = 0
    parts = []
    for x, y in ring:                       # x = lng, y = lat
        la, ln = round(y * 1e5), round(x * 1e5)
        parts.append(encode_values([la - lat]) + encode_values([ln - lng]))
        lat, lng = la, ln
    return "".join(parts)


def stands() -> dict:
    """One record per stand, column-wise (compact), plus its outline."""
    import geopandas as gpd
    import pandas as pd

    c = pd.read_parquet(DATA / "condition.parquet")
    c = c[c.year == 2025].set_index("poly_id")
    t = pd.read_parquet(DATA / "trajectory.parquet").set_index("poly_id")
    g = gpd.read_parquet(DATA / "polygons_geo.parquet").set_index("poly_id").loc[c.index].copy()
    g["geometry"] = g.geometry.simplify(3, preserve_topology=True)
    # the local-scope class, else the nature type (global) scope
    nolocal = ~c.condition_local.isin(CLASSES)
    cls = c.condition_local.where(~nolocal, c.condition_global)
    pt = g.geometry.representative_point()
    en = pd.DataFrame({"E": pt.x.round(), "N": pt.y.round()}, index=g.index)
    ll = gpd.GeoSeries(pt, crs=g.crs).to_crs(4326)
    g4326 = g.to_crs(4326)
    d = {k: [] for k in ("id", "layer", "ft", "cls", "nolocal", "score", "lo", "hi", "poor", "traj", "slope",
                         "ha", "tilstand", "year", "lat", "lng", "E", "N")}
    polys = []
    for pid in g.index:
        layer, sid = pid.split(":")
        score = c.at[pid, "dor_global" if nolocal[pid] else "dor_local"]
        sfx = "global" if nolocal[pid] else "local"
        d["id"].append(sid); d["layer"].append(layer); d["ft"].append(g.at[pid, "forest_type"])
        d["cls"].append(cls[pid]); d["nolocal"].append(bool(nolocal[pid]))
        d["score"].append(round(float(score), 3))
        d["lo"].append(round(float(c.at[pid, f"ci_lo_{sfx}"]), 3)); d["hi"].append(round(float(c.at[pid, f"ci_hi_{sfx}"]), 3))
        d["poor"].append(round(float(c.at[pid, f"dor_{sfx}_frac_poor"]), 3))
        d["traj"].append(t.at[pid, "trajectory"] if pid in t.index else None)
        d["slope"].append(round(float(t.at[pid, "sen_slope"]), 3) if pid in t.index else None)
        d["ha"].append(round(float(g.at[pid, "area_ha"]), 2))
        ts = g.at[pid, "tilstand"]
        d["tilstand"].append("none" if pd.isna(ts) else str(ts))
        sy = g.at[pid, "survey_year"]
        d["year"].append(None if pd.isna(sy) else int(sy))
        d["lat"].append(round(float(ll[pid].y), 5)); d["lng"].append(round(float(ll[pid].x), 5))
        d["E"].append(int(en.at[pid, "E"])); d["N"].append(int(en.at[pid, "N"]))
        geom = g4326.at[pid, "geometry"]
        parts = [p for p in getattr(geom, "geoms", [geom]) if not p.is_empty]
        polys.append(";".join(",".join(encode_ring(r.coords) for r in [p.exterior, *p.interiors]) for p in parts))
    return {"sites": d, "polys": polys}


PAGE = r"""```{=html}
<link rel="stylesheet" href="report-assets/map/leaflet.css">
<style>
#stand-map { height: 720px; width: 100%; border: 1px solid #ccc; border-radius: 4px; margin: 0.5em 0; }
.map-legend { background: #fff; padding: 6px 10px; line-height: 1.5; font-size: 13px; border-radius: 4px; box-shadow: 0 0 6px rgba(0,0,0,.3); }
.map-legend i { display: inline-block; width: 14px; height: 14px; margin-right: 6px; vertical-align: -2px; border-radius: 50%; }
.leaflet-control-layers-base label { font-size: 13px; }
.site-pop { font-size: 12.5px; min-width: 250px; }
.site-pop h4 { margin: 0 0 2px; font-size: 14px; }
.site-pop .sub { color: #57614E; margin-bottom: 6px; }
.site-pop table { border-collapse: collapse; width: 100%; margin: 0; }
.site-pop th { text-align: left; font-weight: 600; color: #57614E; padding: 3px 8px 3px 0; vertical-align: top; white-space: nowrap; border: 0; }
.site-pop td { padding: 3px 0; border: 0; }
.site-pop small { color: #8B927E; }
.site-pop .chip { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; }
.site-pop .links { margin-top: 8px; display: flex; gap: 10px; flex-wrap: wrap; }
</style>
<div id="stand-map" role="application" aria-label="Interactive map of all 737 stands"></div>
<p><em>Interactive map of all 737 stands. Choose what to colour them by with the layer list (condition class in 2025, trajectory 2017-2025, forest type, field condition). Zoom in to see the stand outlines and click a stand for its details and links to Norgeskart and Norge i bilder. The topographic map (Kartverket) can be switched on over the imagery.</em></p>
<script src="report-assets/map/leaflet.js"></script>
<script>
(function () {
  var D = __DATA__, d = D.sites, n = d.id.length;
  var P = __PALETTES__;
  var THEMES = [
    ['cond',  'Condition class 2025'],
    ['traj',  'Trajectory 2017-2025'],
    ['ft',    'Forest type'],
    ['tilst', 'Field condition (tilstand)']
  ];
  var OUTLINE_ZOOM = 12;
  function has(v) { return v !== null && v !== undefined && v !== '' && !(typeof v === 'number' && isNaN(v)); }
  function num(v, k) { return has(v) ? Number(v).toFixed(k) : '-'; }
  function row(label, value) { return '<tr><th>' + label + '</th><td>' + value + '</td></tr>'; }
  function entry(key, i) {
    switch (key) {
      case 'cond':  return P.cond[d.cls[i]];
      case 'traj':  return has(d.traj[i]) ? P.traj[d.traj[i]] : null;
      case 'ft':    return P.ft[d.ft[i]];
      case 'tilst': return P.tilst[d.tilstand[i]];
    }
  }
  function colourFor(key, i) { var e = entry(key, i); return e ? e[1] : '#b9bcae'; }

  var map = L.map('stand-map', {preferCanvas: true, minZoom: 4, maxZoom: 19});
  L.tileLayer('https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    {maxZoom: 19, maxNativeZoom: 17, attribution: 'Imagery &copy; Esri, Maxar, Earthstar Geographics'}).addTo(map);
  var topo = L.tileLayer('https://cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator/{z}/{y}/{x}.png',
    {maxZoom: 19, maxNativeZoom: 18, attribution: '&copy; <a href="https://www.kartverket.no/">Kartverket</a>'});

  function popup(i) {
    var cl = P.cond[d.cls[i]], tj = has(d.traj[i]) ? P.traj[d.traj[i]] : null, ft = P.ft[d.ft[i]][0];
    var h = '<div class="site-pop"><h4>' + ft + ' ' + d.id[i] + '</h4><div class="sub">' + d.layer[i].replace('_', ' ') + '</div><table>';
    h += row('Condition 2025', '<span class="chip" style="background:' + cl[1] + '"></span>' + cl[0] +
             '<br><small>' + (d.nolocal[i] ? 'nature type scope (too few good stands nearby for a local score)' : 'local scope') + '</small>');
    h += row('AE score', num(d.score[i], 2) + ' (95 % interval ' + num(d.lo[i], 2) + ' to ' + num(d.hi[i], 2) +
             ')<br><small>' + Math.round(100 * d.poor[i]) + ' % of the cells are below the good-poor threshold</small>');
    if (tj) h += row('Trajectory', '<span class="chip" style="background:' + tj[1] + '"></span>' + tj[0] +
             '<br><small>Theil-Sen slope ' + num(d.slope[i], 3) + ' z per year</small>');
    h += row('Area', num(d.ha[i], 2) + ' ha' + (d.ha[i] < 0.5 ? '<br><small>small stand: the score is less reliable</small>' : ''));
    h += row('Field survey', (has(d.year[i]) ? d.year[i] + ': ' : '') + P.tilst[d.tilstand[i]][0]);
    h += '</table><div class="links">' +
         '<a href="https://norgeskart.no/#!?project=norgeskart&layers=1002&zoom=14&lat=' + d.N[i] + '&lon=' + d.E[i] + '" target="_blank">Norgeskart</a>' +
         '<a href="https://www.norgeibilder.no/?x=' + d.E[i] + '&y=' + d.N[i] + '&level=15&utm=33" target="_blank">Norge i bilder</a></div></div>';
    return h;
  }

  // one point layer per theme (shown when zoomed out)
  var groups = {}, base = {};
  THEMES.forEach(function (t) {
    var g = L.layerGroup();
    for (var i = 0; i < n; i++) {
      (function (i) {
        L.circleMarker([d.lat[i], d.lng[i]], {radius: 4.5, color: '#fff', weight: 0.7, opacity: 0.9,
          fillColor: colourFor(t[0], i), fillOpacity: 0.95})
          .bindTooltip(P.ft[d.ft[i]][0] + ' ' + d.id[i]).bindPopup(function () { return popup(i); }).addTo(g);
      })(i);
    }
    groups[t[0]] = g; base[t[1]] = g;
  });
  var theme = 'cond';
  groups.cond.addTo(map);

  // outlines, shown when zoomed in, coloured like the selected layer
  function decode(str) {
    var i = 0, lat = 0, lng = 0, out = [];
    while (i < str.length) {
      var b, shift = 0, result = 0;
      do { b = str.charCodeAt(i++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
      lat += (result & 1) ? ~(result >> 1) : (result >> 1);
      shift = 0; result = 0;
      do { b = str.charCodeAt(i++) - 63; result |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20);
      lng += (result & 1) ? ~(result >> 1) : (result >> 1);
      out.push([lat / 1e5, lng / 1e5]);
    }
    return out;
  }
  var outlines = null;
  function styleFor(i) {
    var c = colourFor(theme, i);
    return {color: c, weight: 2.5, opacity: 1, fill: true, fillColor: c, fillOpacity: 0.25};
  }
  function buildOutlines() {
    outlines = L.featureGroup();
    for (var i = 0; i < n; i++) {
      if (!D.polys[i]) continue;
      var parts = D.polys[i].split(';').map(function (p) { return p.split(',').map(decode); });
      var poly = L.polygon(parts, styleFor(i));
      poly._site = i;
      poly.on('click', function (e) { L.popup().setLatLng(e.latlng).setContent(popup(this._site)).openOn(map); });
      outlines.addLayer(poly);
    }
  }
  function updateOutlines() {
    if (map.getZoom() >= OUTLINE_ZOOM) {
      if (!outlines) buildOutlines();
      if (!map.hasLayer(outlines)) outlines.addTo(map);
    } else if (outlines && map.hasLayer(outlines)) {
      map.removeLayer(outlines);
    }
  }

  // legends follow the selected layer
  var legends = {};
  THEMES.forEach(function (t) {
    var items = P[t[0]];
    legends[t[0]] = '<b>' + t[1] + '</b><br>' + Object.keys(items).map(function (k) {
      return '<i style="background:' + items[k][1] + '"></i>' + items[k][0]; }).join('<br>');
  });
  var lg = L.control({position: 'bottomright'});
  lg.onAdd = function () { this._d = L.DomUtil.create('div', 'map-legend'); this._d.innerHTML = legends.cond; return this._d; };
  lg.addTo(map);

  L.control.layers(base, {'Topographic map (Kartverket)': topo}, {collapsed: false}).addTo(map);
  L.control.scale({imperial: false, position: 'bottomleft'}).addTo(map);
  map.on('baselayerchange', function (e) {
    THEMES.forEach(function (t) { if (t[1] === e.name) theme = t[0]; });
    lg._d.innerHTML = legends[theme];
    if (outlines) outlines.eachLayer(function (l) { l.setStyle(styleFor(l._site)); });
  });
  map.on('zoomend', updateOutlines);
  map.fitBounds([[Math.min.apply(null, d.lat), Math.min.apply(null, d.lng)], [Math.max.apply(null, d.lat), Math.max.apply(null, d.lng)]]);
  updateOutlines();
})();
</script>
```
"""


def main() -> None:
    """Put the map assets next to the sources; rewrite stand_map.md when the condition data are at hand."""
    out = OUT / "map"
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(HERE / "assets" / "map", out, copy_function=shutil.copyfile)        # leaflet + the committed stand_map.md
    if not (DATA / "condition.parquet").exists():
        print(f"note: {DATA} not found; using the committed stand_map.md")
        return
    fc = stands()
    pal = {"cond": CLASSES, "traj": TRAJ, "ft": TYPES, "tilst": TILSTAND}
    pal = {k: {kk: list(vv) for kk, vv in v.items()} for k, v in pal.items()}
    page = (PAGE.replace("__DATA__", json.dumps(fc, separators=(",", ":")))
                .replace("__PALETTES__", json.dumps(pal, ensure_ascii=False)))
    page_path = out / "stand_map.md"
    page_path.write_text(page, encoding="utf-8")
    shutil.copyfile(page_path, HERE / "assets" / "map" / "stand_map.md")
    print(f"wrote {page_path.name}: {len(fc['polys'])} stands, {page_path.stat().st_size / 1e3:.0f} kB")


if __name__ == "__main__":
    main()
