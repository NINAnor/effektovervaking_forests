// Deployment config, loaded by js/boot.js before the app. Nothing here is secret.
window.DASHBOARD_CONFIG = {
  // Folder holding index.json and the files it lists, written by
  // `forestmon publish dashboard`. Relative to the page, or a full URL
  // (e.g. an S3 prefix with public read and CORS).
  dataBase: 'data/',

  // Condition per 10 m cell, read by range requests (needs CORS with Range).
  // Empty: condition/pmtiles/condition_pixels.pmtiles under the S3 prefix the
  // data were baked from (index.json `source`); hidden when baked from a folder.
  conditionCells: '',

  // Sentinel-2 time series for a clicked S-CCD pixel; {lon} and {lat} are filled in.
  timeseriesUrl: 'https://jstnbraaten.users.earthengine.app/view/eo-timeseries-explorer#run=true;sensor=Sentinel-2%20SR;lon={lon};lat={lat};index=NBR;rgb=SWIR1%2FNIR%2FGREEN;duration=12;cloud=30;chipwidth=2;',

  // Basemaps offered in the layer panel; the first is the default.
  basemaps: {
    satellite: {
      label: 'Satellite (Esri World Imagery)',
      short: 'Satellite',
      tiles: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      maxzoom: 19,
      attribution: 'Imagery &copy; Esri, Maxar, Earthstar Geographics'
    },
    osm: {
      label: 'Map (OpenStreetMap)',
      short: 'Map',
      tiles: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
      maxzoom: 19,
      attribution: '&copy; OpenStreetMap contributors'
    }
  }
};
