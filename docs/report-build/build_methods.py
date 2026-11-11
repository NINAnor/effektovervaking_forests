"""Build the methods appendix (HTML, PDF, DOCX, QMD) from the Claude Doc export. Part of docs/report-build.

Source: src/methods/claude_doc_export.md, the doc's own Markdown export, with the pipeline
figure taken from its Word export (src/methods/media/image1.png). This script only reshapes
that text for Quarto (title block, equations, captioned figure); it does not change
the content.

    python docs/report-build/build_methods.py
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # docs/report-build
OUT = HERE.parent / "report-assets"             # docs/report-assets: what the .qmd sources read
DOCS = HERE.parent                              # docs/: the .qmd sources and their renders
OUT.mkdir(exist_ok=True)
sys.path.insert(0, str(HERE))
from fix_docx_tables import main as fix_docx  # noqa: E402
from hide_s3 import hide_s3  # noqa: E402

QMD = DOCS / "forest_monitoring_methods.qmd"

YAML = """---
title: "Methods: remote sensed monitoring of olivinskog and kalklindeskog, 2017-2025"
subtitle: "Methods, validation and uncertainty for the satellite monitoring of olivinskog and kalklindeskog"
author:
  - name: Geethen Singh
    email: geethen.singh@nina.no
    affiliations:
      - id: nina
        name: "Norwegian Institute for Nature Research (NINA)"
  - name: Andrew Møller Gray
    email: andrew.gray@nina.no
    affiliations:
      - ref: nina
  - name: Ida Marielle Mienna
    email: ida.mienna@nina.no
    affiliations:
      - ref: nina
date: last-modified
date-format: "D MMMM YYYY"
code-links:
  - text: Code repository
    icon: github
    href: https://github.com/NINAnor/effektovervaking_forests
lang: en-GB
bibliography: report-assets/references.bib
csl: report-assets/ambio.csl
fig-cap-location: bottom
format:
  html:
    embed-resources: true
    html-math-method: mathml   # no script: the default MathJax setup pulls polyfill.io
    toc: true
    toc-title: Contents
    toc-depth: 2
    smooth-scroll: true
    number-sections: false
    lightbox: true
    anchor-sections: true
  docx:
    reference-doc: report-assets/reference_a4.docx
    columns: 1000
    toc: true
    toc-depth: 2
    toc-title: Contents
  typst:
    filters:
      - report-assets/typst_wrap_code.lua
    citeproc: true
    papersize: a4
    margin:
      x: 2.2cm
      y: 2.2cm
    fontsize: 10pt
    toc: true
    toc-depth: 2
    output-file: forest_monitoring_methods.pdf
---

::: {.callout-note appearance="simple"}
This is the methods appendix of the report "Remote sensed monitoring of olivinskog and kalklindeskog,
2017-2025". It is a living document: it is updated together with the analysis, and the date above shows
when it last changed. Section, table and figure numbers are kept stable so that references from the
report remain valid.
:::
"""


# Plain-text citations -> citeproc keys in references.bib (the reference list is then generated).
CITES = {
    "(Brown et al. 2025)": "[@brown2025]",
    "(Hansen et al. 2013)": "[@hansen2013]",
    "(Zanaga et al. 2022)": "[@zanaga2022]",
    "(OPERA Project 2024)": "[@opera2024]",
    "(van der Woude et al. 2026)": "[@vanderwoude2026]",
    "(Ledoit and Wolf 2004)": "[@ledoit2004]",
    "(Youden 1950)": "[@youden1950]",
    "(Mann 1945; Kendall 1975)": "[@mann1945; @kendall1975]",
    "(Sen 1968)": "[@sen1968]",
    "(Composite2Change; Hermosilla et al. 2015)": "[Composite2Change, @hermosilla2015]",
    "(Key and Benson 2006)": "[@keybenson2006]",
    "(Wilson 1927)": "[@wilson1927]",
    "(Olofsson et al. 2014)": "[@olofsson2014]",
    "(S-CCD; Ye et al. 2021)": "[S-CCD, @ye2021]",
    "(Mann and Whitney 1947)": "[@mannwhitney1947]",
    "(Welch 1947)": "[@welch1947]",
}


def appendix_structure(text: str) -> str:
    """Same structure as the kystlynghei methods appendix: numbered level-1 sections with
    stable `section-N` anchors (the report links to them), citeproc references."""
    for old, new in CITES.items():
        assert old in text, old
        text = text.replace(old, new)
    # the typed reference list -> the generated one
    text, n = re.subn(r"^## References\n.*\Z", "# References {#references .unnumbered}\n\n::: {#refs}\n:::\n",
                      text, flags=re.M | re.S)
    assert n == 1, "references"

    def h2(m):
        return f"# {m.group(1)}. {m.group(2)} {{#section-{m.group(1)} .unnumbered}}"

    def h3(m):
        return f"## {m.group(1)}.{m.group(2)} {m.group(3)} {{#section-{m.group(1)}-{m.group(2)} .unnumbered}}"
    text = re.sub(r"^### (\d+)\.(\d+) (.+)$", h3, text, flags=re.M)
    text = re.sub(r"^## (\d+) (.+)$", h2, text, flags=re.M)
    text = re.sub(r"^## (Appendix: .+)$", lambda m: f"# {m.group(1)} {{#appendix-whitening .unnumbered}}", text, flags=re.M)
    return text


def main() -> None:
    text = hide_s3((HERE / "src" / "methods" / "claude_doc_export.md").read_text(encoding="utf-8"))
    lines = text.splitlines()
    # Title and byline ("Oct 22, 2026 · @Geethen Singh") move to the YAML.
    assert lines[0].startswith("# Methods:") and "@Geethen Singh" in lines[2], lines[:3]
    text = "\n".join(lines[3:]).lstrip("\n")

    # Embedded widget -> its rendered image from the Word export, as a captioned figure.
    pat = re.compile(r"&#91;embedded content: (.+?)\\?\]")
    m = pat.search(text)
    assert m and len(pat.findall(text)) == 1, "embedded content placeholder"
    text = pat.sub(lambda m: f"![{m.group(1)}](report-assets/media/image1.png){{#fig-pipeline width=100%}}", text)

    # ```latex blocks -> display math.
    text, n = re.subn(r"```latex\n(.+?)\n```", lambda m: f"$$\n{m.group(1)}\n$$", text, flags=re.S)
    print("equations:", n)

    text = appendix_structure(text)
    QMD.write_text(YAML + "\n" + text.strip() + "\n", encoding="utf-8")
    print("wrote", QMD.name)
    for f in ("ambio.csl", "references.bib", "typst_wrap_code.lua"):
        shutil.copyfile(HERE / "assets" / f, OUT / f)
    shutil.copytree(HERE / "src" / "methods" / "media", OUT / "media", dirs_exist_ok=True, copy_function=shutil.copyfile)
    subprocess.run(["bash", str(HERE / "render.sh"), QMD.name], cwd=DOCS, check=True)
    fix_docx(DOCS / f"{QMD.stem}.docx")
    print("rendered:", ", ".join(p.name for p in sorted(DOCS.glob(QMD.stem + ".*"))))


if __name__ == "__main__":
    main()
