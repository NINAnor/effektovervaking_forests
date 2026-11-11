"""Build the report and its methods appendix and publish them in docs/.

    python docs/report-build/publish.py

Runs build_report.py and build_methods.py (each writes its .qmd and inputs into docs/report-src/ and
renders HTML, PDF and Word into docs/), with the report written directly to index.html, as in
kystlynghei_condition (report + methods appendix, rendered next to each other, linked section by section):

    docs/index.html                       the report (HTML, GitHub Pages)
    docs/forest_monitoring_methods.html   the methods appendix, linked from the report
    docs/forest_monitoring_{report,methods}.{pdf,docx}
    docs/report-src/                      the Quarto project: the two .qmd, their bibliography, figures and templates
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # docs/report-build
DOCS = HERE.parent
SRC = DOCS / "report-src"                       # written by the builds (all but _quarto.yml): cleared first so nothing stale is left


def main() -> None:
    if shutil.which("quarto") is None:
        raise SystemExit("Quarto is not installed or not on PATH. Install Quarto >= 1.4 before publishing.")
    for p in SRC.iterdir():
        if p.name != "_quarto.yml":
            shutil.rmtree(p) if p.is_dir() else p.unlink()
    for script in ("build_report.py", "build_methods.py"):    # the report first: it writes reference_a4.docx
        subprocess.run([sys.executable, str(HERE / script)], check=True)
    (DOCS / ".nojekyll").touch()
    print("published:", ", ".join(sorted(p.name for p in DOCS.iterdir())))


if __name__ == "__main__":
    main()
