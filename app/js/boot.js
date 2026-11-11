// @ts-check
// Runs before the app. Classic, parser-blocking script: the document.write
// calls below only work while the parser waits, so no async/defer/module.

// Theme, applied before first paint so dark does not flash white. Light is the
// default; prefers-color-scheme is deliberately not consulted.
try {
  if (localStorage.getItem('forestmon:theme') === 'dark') document.documentElement.dataset.theme = 'dark';
} catch { /* storage blocked */ }

// CDN fallback, used only if vendor/ is missing. The SRI digests are of the
// files in app/vendor/; change the version and the digests together.
if (typeof maplibregl === 'undefined') {
  document.write('<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/'
    + 'maplibre-gl.js" integrity="sha384-SYKAG6cglRMN0RVvhNeBY0r3FYKNOJtznw'
    + 'A0v7B5Vp9tr31xAHsZC0DqkQ/pZDmj" crossorigin="anonymous"><\/script>');
  document.write('<link href="https://unpkg.com/maplibre-gl@4.7.1/dist/'
    + 'maplibre-gl.css" rel="stylesheet" integrity="sha384-MinO0mNliZ3vwppuPO'
    + 'UnGa+iq619pfMhLVUXfC4LHwSCvF9H+6P/KO4Q7qBOYV5V" crossorigin="anonymous">');
}

// pmtiles.js reads PMTiles from S3 with range requests.
if (typeof pmtiles === 'undefined') {
  document.write('<script src="https://unpkg.com/pmtiles@3.0.6/dist/'
    + 'pmtiles.js" integrity="sha384-4sbA4B4Oqxkzs6apu8HaZcGrL3BySmt0tO/LQf6al3hkgq8ijZFxkTkvxxZZ/3PU" '  // pragma: allowlist secret (SRI hash)
    + 'crossorigin="anonymous"><\/script>');
}

// config.js changes per deployment; the ?v= keeps a stale cached copy out.
document.write('<script src="config.js?v=' + Date.now() + '"><\/script>');
