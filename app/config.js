// Deployment config, loaded by js/boot.js before the app. Nothing here is secret.
window.DASHBOARD_CONFIG = {
  // Folder holding index.json and the files it lists, written by
  // `forestmon publish dashboard`. Relative to the page, or a full URL
  // (e.g. an S3 prefix with public read and CORS).
  dataBase: 'data/',

  // Condition per 10 m cell (pmtiles, read by range requests; the dms bucket sends
  // no CORS yet, so it only loads where a CORS extension or rule is in place).
  // Empty: condition_pixels.pmtiles in the condition folder the data were baked
  // from (index.json `condition`, FORESTMON_CONDITION_SOURCE).
  conditionCells: '',

  // C2C yearly change pixels (pmtiles, as conditionCells). Empty: no C2C layer.
  c2cPixels: 'https://s3-int-1.nina.no/miljodata-test/156004_08_effektovervaking_2026_2030_opsjon_2_fjernmaling/c2c/pmtiles/c2c_pixels.pmtiles',

  // Sentinel-2 time series for a clicked S-CCD pixel; {lon} and {lat} are filled in.
  timeseriesUrl: 'https://jstnbraaten.users.earthengine.app/view/eo-timeseries-explorer#run=true;sensor=Sentinel-2%20SR;lon={lon};lat={lat};index=NBR;rgb=SWIR1%2FNIR%2FGREEN;duration=12;cloud=30;chipwidth=2;',

  // Esri World Imagery Wayback: the release list behind the "Past" background.
  waybackConfig: 'https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json',

  // Basemaps offered in the layer panel; the first is the default.
  basemaps: {
    satellite: {
      label: 'Satellite (Esri World Imagery)',
      short: 'Satellite',
      tiles: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      maxzoom: 19,
      attribution: 'Imagery &copy; Esri, Maxar, Earthstar Geographics'
    },
    // Kartverket's open topographic map (CC BY 4.0): Norway only, which is all we show.
    topo: {
      label: 'Map (Kartverket topo)',
      short: 'Map',
      tiles: 'https://cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator/{z}/{y}/{x}.png',
      maxzoom: 18,
      attribution: '&copy; <a href="https://www.kartverket.no/">Kartverket</a>'
    }
  }
};
