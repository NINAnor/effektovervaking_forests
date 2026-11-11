// Deployment config, loaded by js/boot.js before the app. Nothing here is secret.
window.DASHBOARD_CONFIG = {
  // Folder holding index.json and the files it lists, written by
  // `forestmon publish dashboard`. Relative to the page, or a full URL
  // (e.g. an S3 prefix with public read and CORS).
  dataBase: 'data/',

  // Basemap tiles.
  basemap: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  basemapAttribution: '&copy; OpenStreetMap contributors'
};
