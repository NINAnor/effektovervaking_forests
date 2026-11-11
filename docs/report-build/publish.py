"""Build the report and its methods appendix and publish them in docs/.

    python docs/report-build/publish.py

Runs build_report.py and build_methods.py (each writes its .qmd and renders HTML, PDF and Word in
docs/source), then moves the renders next to the docs, as in kystlynghei_condition (report + methods
appendix, rendered next to each other, linked section by section):

    docs/index.html                       the report (HTML, GitHub Pages)
    docs/forest_monitoring_methods.html   the methods appendix, linked from the report
    docs/forest_monitoring_{report,methods}.{pdf,docx}
    docs/source/                          the Quarto sources, to host in NINAnor/effektovervaaking-2026-2030
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # docs/report-build
DOCS = HERE.parent
SRC = DOCS / "source"


def main() -> None:
    if SRC.exists():
        shutil.rmtree(SRC)                      # nothing stale: the sources are rebuilt from docs/report-build
    for script in ("build_report.py", "build_methods.py"):    # the report first: it writes reference_a4.docx
        subprocess.run([sys.executable, str(HERE / script)], check=True)
    shutil.move(SRC / "forest_monitoring_report.html", DOCS / "index.html")
    for f in ("forest_monitoring_report.pdf", "forest_monitoring_report.docx", "forest_monitoring_methods.html",
              "forest_monitoring_methods.pdf", "forest_monitoring_methods.docx"):
        shutil.move(SRC / f, DOCS / f)
    (DOCS / ".nojekyll").touch()
    for p in SRC.glob("*_files"):
        shutil.rmtree(p, ignore_errors=True)
    for p in SRC.glob("forest_monitoring_*.typ"):        # Quarto's intermediate files, not typst-template.typ
        p.unlink()
    print("published:", ", ".join(sorted(p.name for p in DOCS.iterdir())))


if __name__ == "__main__":
    main()
