# Building the report and its methods appendix

The report ("Remote sensed monitoring of olivinskog and kalklindeskog, 2017-2025") and its methods
appendix are written as Claude Docs. This folder turns their exports into Quarto sources and renders
HTML, PDF and Word. Nothing outside this repository is needed.

Install [Quarto >= 1.4](https://quarto.org/docs/get-started/) (including its bundled Typst engine)
and check that `quarto --version` works in your terminal. The VS Code Quarto extension alone does not
install the command-line tool.

To render the existing Quarto sources, run from the repository root; no Python packages are needed:

```bash
bash docs/report-build/render.sh                      # HTML, PDF and Word
bash docs/report-build/render.sh --to html            # HTML only (faster)
```

The Quarto project is `docs/report-src/` and renders only the report and methods appendix, not the
ecRxiv helper documents. The wrapper builds a copy of it in local temporary storage and copies the
finished files into `docs/` (the report as `docs/index.html`). Rendering directly on the shared project drive fails with Quarto's
`database is locked` project-cache error or `WouldBlock` during cleanup. Keep `TMPDIR` on a local
filesystem, not the shared drive. On a local checkout, `quarto render docs/report-src --to all` also
works, but leaves the renders in `docs/report-src/`.

To render just one document, pass its source filename before any options:

```bash
bash docs/report-build/render.sh forest_monitoring_methods.qmd --to html
```

To regenerate the Quarto sources from the original exports and render all formats:

```bash
pip install -r docs/report-build/requirements.txt
python docs/report-build/publish.py
```

| Output | What |
|---|---|
| `docs/index.html` | the report (the GitHub Pages entry) |
| `docs/forest_monitoring_methods.html` | the methods appendix; the report links to its sections |
| `docs/forest_monitoring_{report,methods}.{pdf,docx}` | the same in PDF and Word |
| `docs/report-src/` | the Quarto project: `forest_monitoring_{report,methods}.qmd` with `_quarto.yml`, `references.bib`, `ambio.csl`, `figures/`, `map/`, `media/` and the Word and Typst templates; render with the wrapper above |

## Updating from the Claude Docs

Export each doc again and replace the files in `src/`:

- **Report** (`src/report/`): export the Word file as `claude_doc_export.docx`, then
  `pandoc claude_doc_export.docx -t markdown-smart-multiline_tables-grid_tables-simple_tables --wrap=none --extract-media=. -o claude_doc_export.md`
  (run inside `src/report/`; the figures land in `src/report/media/`).
- **Methods** (`src/methods/`): export the Markdown as `claude_doc_export.md`. The pipeline figure is
  `src/methods/media/image1.png` (from the doc's Word export).

The scripts only reshape the text for Quarto (title block, numbered sections, figure and table captions,
citations, links to the appendix). The findings are not changed. `REPLACE`, `DOC_SYNC` and `CITES` in
`build_report.py` and `CITES` in `build_methods.py` list every substitution; a note is printed for
those no longer needed.

## Files

| File | Role |
|---|---|
| `publish.py` | clears `docs/report-src/` (all but `_quarto.yml`) and runs the two builds |
| `render.sh` | renders `docs/report-src/` in local temporary storage, then copies HTML, PDF and Word into `docs/` |
| `build_report.py` | report: `.qmd`, figures, ecRxiv badges and metadata, render, tidy the Word tables |
| `build_methods.py` | methods appendix: `.qmd` with `section-N` anchors and citeproc references, render |
| `make_stand_map.py` | interactive stand map (HTML only). Rebuilt when `CONDITION_OUT_DATA` (default `data/condition/outputs/data`) holds `condition.parquet`, `trajectory.parquet` and `polygons_geo.parquet` (also in the S3 bucket under `condition/parquet/`); otherwise the committed `assets/map/stand_map.md` is used |
| `fix_docx_tables.py` | gives Word tables the full text width |
| `assets/` | `references.bib`, `ambio.csl`, the Typst template and filter, the Leaflet files |
| `callouts/` | the "Analysis code" panels of the report |
| `ecrxiv/` | ecRxiv badge helper and the metadata the report's table is read from |

The metadata table, status note, version (`VERSION`) and log in `build_report.py` are written by hand.
Update them with each release.
