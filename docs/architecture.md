# Architecture: ingestion to Parquet output

How a Sentinel-2 scene on CDSE becomes a row in the alerts Parquet. Module names
are in `src/effektovervaking_forests/`; see [operational.md](operational.md) for
the reasoning behind each step.

```mermaid
flowchart TD
    subgraph IN["1. Inputs"]
        AOI[("AOI polygons<br/>GeoParquet, one per AOI")]
        STAC["CDSE STAC<br/>sentinel-2-l2a"]
        S3[("CDSE S3 eodata<br/>JP2 band assets")]
    end

    subgraph IDX["2. Index"]
        GRID["aoi.py<br/>fixed 10 m pixel grid on S2 UTM lattice"]
        CAT["catalog.py<br/>scene index + acquisition schedule"]
        TILE["assign-tiles<br/>pin each pixel to its most interior MGRS tile"]
    end

    subgraph ING["3. Ingestion (per new scene)"]
        LEDGER["runs.py<br/>ledger: which scenes are processed"]
        READ["reader.py<br/>block-aligned windowed JP2 reads<br/>BOA offset, SCL cloud mask"]
        OBS["obs.py<br/>immutable part files, key (pix_id, acquisition)"]
    end

    subgraph MODEL["4. Detection"]
        SCCD["sccd.py<br/>S-CCD recursive update per pixel"]
        STATE[("state.py<br/>per-pixel SccdOutput pickle")]
        ALERT["alerts.py<br/>magnitude gate, append-only,<br/>human verdicts win"]
    end

    subgraph OUT["5. Parquet output"]
        P_GRID[/"grid/&lt;aoi&gt;_grid.parquet"/]
        P_CAT[/"catalog/&lt;aoi&gt;_scenes.parquet"/]
        P_OBS[/"obs/&lt;aoi&gt;/*.parquet<br/>compacted to one sorted file per year"/]
        P_ALERT[/"alerts/&lt;aoi&gt;_alerts.parquet"/]
    end

    PUB["publish.py<br/>COG, GeoJSON, PMTiles, GPKG"]

    AOI --> GRID --> P_GRID
    AOI --> STAC
    STAC --> CAT --> P_CAT
    CAT --> TILE
    GRID --> TILE
    P_CAT -. "new scenes" .-> LEDGER
    LEDGER --> READ
    S3 --> READ
    TILE --> READ
    READ --> OBS --> P_OBS
    P_OBS -- "compact" --> P_OBS
    OBS --> SCCD
    SCCD <--> STATE
    SCCD --> ALERT --> P_ALERT
    P_ALERT --> PUB
    P_OBS -. "confidence queries (DuckDB)" .-> ALERT
```

## Commit order within one scene

Each write is atomic (`store.write_parquet` writes a temp file, then renames), and
the order means a crash always leaves work to redo, never work silently lost.

```mermaid
sequenceDiagram
    participant R as reader
    participant O as obs store
    participant L as ledger
    participant S as state pickle
    participant A as alerts parquet
    R->>O: 1. write part file (idempotent by key)
    O->>L: 2. stage = observations_committed
    L->>S: 3. replace pickle atomically
    S->>L: 4. stage = state_applied
    S->>A: 5. write alerts, clear alerts_dirty
```

A scene stuck at `observations_committed` is replayed from disk on the next run,
not re-read from CDSE. An observation is never applied to the recursive model
twice.

## One-off gates before the cycle runs

`parity check` (CDSE equals Earth Engine on raw DN) must pass before
`sccd bootstrap` fits the initial per-pixel state. After that, `forestmon run
<aoi>` is the only command a timer needs.
