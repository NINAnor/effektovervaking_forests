// ESLint flat config for the dashboard in app/ (static files, no build step).
// Named .mjs because the repo has no package.json; eslint comes from prek.
// Globals are written out, not imported from the `globals` package, so a typo
// that matches a browser global (name, top, event) still fails no-undef.
// Re-derive the list from the no-undef output if the app uses new browser APIs.

export default [
  { ignores: ["app/vendor/**", "app/data/**"] },
  {
    files: ["app/**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "script",
      globals: {
        window: "readonly",
        document: "readonly",
        console: "readonly",
        localStorage: "readonly",
        fetch: "readonly",
        requestAnimationFrame: "readonly",
        cancelAnimationFrame: "readonly",
        performance: "readonly",
        matchMedia: "readonly",
        ResizeObserver: "readonly",
        maplibregl: "readonly",
        pmtiles: "readonly",
      },
    },
    rules: {
      "no-undef": "error",
      "no-unused-vars": "warn",
    },
  },
];
