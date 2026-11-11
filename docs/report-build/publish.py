"""Build the report and its methods appendix and publish them in docs/.

    python docs/report-build/publish.py

Runs build_report.py and build_methods.py (each writes its .qmd and renders HTML, PDF and Word in
docs/), with the report written directly to index.html, as in kystlynghei_condition (report + methods
appendix, rendered next to each other, linked section by section):

    docs/index.html                       the report (HTML, GitHub Pages)
    docs/forest_monitoring_methods.html   the methods appendix, linked from the report
    docs/forest_monitoring_{report,methods}.{pdf,docx}
    docs/forest_monitoring_{report,methods}.qmd   the Quarto sources, with their bibliography, figures and templates beside them
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # docs/report-build
DOCS = HERE.parent
# What the builds write into docs/ besides the renders: removed first so nothing stale is left.
GENERATED = ("forest_monitoring_report.qmd", "forest_monitoring_methods.qmd", "references.bib", "ambio.csl",
             "reference_a4.docx", "typst-template.typ", "typst_wrap_code.lua", "figures", "map", "media")


def main() -> None:
    if shutil.which("quarto") is None:
        raise SystemExit("Quarto is not installed or not on PATH. Install Quarto >= 1.4 before publishing.")
    for name in GENERATED:
        p = DOCS / name
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    for script in ("build_report.py", "build_methods.py"):    # the report first: it writes reference_a4.docx
        subprocess.run([sys.executable, str(HERE / script)], check=True)
    (DOCS / ".nojekyll").touch()
    for p in DOCS.glob("*_files"):
        shutil.rmtree(p, ignore_errors=True)
    for p in DOCS.glob("forest_monitoring_*.typ"):        # Quarto's intermediate files, not typst-template.typ
        p.unlink()
    print("published:", ", ".join(sorted(p.name for p in DOCS.iterdir())))


if __name__ == "__main__":
    main()
