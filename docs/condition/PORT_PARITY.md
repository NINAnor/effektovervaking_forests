# Port parity: condition/deploy -> effektovervaking_forests.condition

The condition pipeline was moved into this package on 2026-10-28 from
`Geethen_forests/condition/deploy/` (MANIFEST.json: Geethen_forests commit
`5b93f83`, every file unchanged against its recorded SHA-256, no upstream drift).

## What changed in the code

- `src/condition/*` -> `src/effektovervaking_forests/condition/*`; imports
  rewritten. `core.py` is byte-identical (pinned by
  `tests/condition/test_deploy.py::test_core_is_vendored_verbatim`).
- `scripts/NN_name.py` -> `condition/steps/sNN_name.py`, with `main(argv)`
  instead of `main()`; the `sys.path` / `_paths` shims are gone and step 07
  imports step 06 as a module instead of loading it by file path.
- `paths.py`: the root is `CONDITION_ROOT`, else `<FORESTMON_DATA_DIR>/condition`
  (was: the deploy directory), and directories are created by
  `paths.ensure_dirs()` when a step starts rather than at import.
- `run_all.sh` -> `forestmon condition run` (`condition/steps/__init__.py`).
- `ruff format` and the safe `ruff check --fix` rewrites (import order,
  pyupgrade) over everything except `core.py`.

## How it was verified

Steps 04 -> 08 were run twice on identical copies of the production inputs
(the 13 tables 00-02e write, plus `aef_index.parquet`) in the same uv
environment (numba 0.68.1) with the same flags
(`--boot-rng per-stand --batch-small 200 --workers 8 --parallel process`):
once with the original deploy scripts, once through the ported package and the
new CLI (`forestmon condition step 04`, then `forestmon condition run --from 03 --to 08`).

| product | original vs port |
|---|---|
| `pixel_condition`, `condition`, `trajectory`, `trajectory_pixels` parquet | byte-identical |
| `threshold.json`, `whitener.json`, `trajectory_gate.json`, `condition_combined.csv` | byte-identical |
| 66 GeoTIFFs | identical pixels, profile and band descriptions |
| 5 VRTs, `index.csv`, `report_data.json` | byte-identical |
| `condition_results.gpkg` | all rows equal in `condition_pixels` (168,832), `condition_stands` (737), `readme` (19) |

82 of 82 products equal. Against the production run of 2026-10-23 the eight
tables are value-identical too (the parquet files differ only in metadata
bytes), and `05`/`08` print the documented stand AUC 0.836 (`local`) / 0.779
(`global`). Wall clock on this workstation: 04 in 3.0 min (orig 3.0 min), 03-08
in under 40 s.

Upstream, `forestmon condition run --from 00 --to 01` and `step 02b` (from the
production `polygon_pixels.parquet`) reproduce `polygons`, `polygons_geo`,
`read_plan`, `pixels`, `pixel_roles`, `pixel_sites` and `polygon_overlap`
value-for-value against production. `pixel_polygons.edge_m` differs in 518 of
222,874 rows by at most 2.8e-9 m; the original 02b script run in the same
environment gives a byte-identical file to the port, so this is the GEOS
upgrade (3.13.1 in the `geo` pixi env, 3.14.1 in the uv venv), not the port.

After review, four small hardening changes went in: `aef_backend._tile_info`
fetches the tile index if it is missing, `loader_available()` also requires
`dask`, steps 02e/09 take `--project` from `FORESTMON_EE_PROJECT` (02e used to
default to `ee-gsingh`), and the runner starts steps with `python -u`. 03-08
were re-run through the runner afterwards: still 82 of 82 equal.

Not re-run: the two download steps (02, 02d), 02c and the Earth Engine steps
(02e, 09); their library code is covered by the offline tests and the seven
`-m network` tests.
