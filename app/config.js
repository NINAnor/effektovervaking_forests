// Deployment config, loaded by js/boot.js before the app. Nothing here is secret.
window.DASHBOARD_CONFIG = {
  // Folder holding index.json and the files it lists, written by
  // `forestmon publish dashboard`. Relative to the page, or a full URL
  // (e.g. an S3 prefix with public read and CORS).
  dataBase: 'data/',

  // Condition per 10 m cell, read by range requests (needs CORS with Range).
  // Empty hides the layer.
  conditionCells: 'https://s3-int-1.nina.no/miljodata-test/156004_08_effektovervaking_2026_2030_opsjon_2_fjernmaling/condition/pmtiles/condition_pixels.pmtiles',

  // Basemap tiles.
  basemap: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  basemapAttribution: '&copy; OpenStreetMap contributors'
};
