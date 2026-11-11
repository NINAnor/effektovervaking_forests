// @ts-check
// Runs before the app. Classic, parser-blocking script: the document.write
// calls below only work while the parser waits, so no async/defer/module.

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

// config.js changes per deployment; the ?v= keeps a stale cached copy out.
document.write('<script src="config.js?v=' + Date.now() + '"><\/script>');
