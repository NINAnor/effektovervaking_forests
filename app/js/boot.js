// @ts-check
// Runs before the app. Classic, parser-blocking script: the document.write
// calls below only work while the parser waits, so no async/defer/module.

// Theme, applied before first paint so dark does not flash white. Light is the
// default; prefers-color-scheme is deliberately not consulted.
try {
  if (localStorage.getItem('forestmon:theme') === 'dark') document.documentElement.dataset.theme = 'dark';
} catch { /* storage blocked */ }

// config.js changes per deployment; the ?v= keeps a stale cached copy out.
document.write('<script src="config.js?v=' + Date.now() + '"><\/script>');
