"""Build the report (HTML, DOCX, PDF) from the Claude Doc export. Part of docs/report-build (see README.md).

Source: src/claude_doc_export.md, made with
    pandoc src/claude_doc_export.docx -t markdown-smart-multiline_tables-grid_tables-simple_tables \
        --wrap=none --extract-media=src -o src/claude_doc_export.md
from the doc's own Word export. This script only reshapes that text for Quarto
(title block, numbered sections, figure and table captions) and removes
authoring notes; it does not change the findings.

    python docs/report-build/build_report.py   # .qmd into docs/, figures into docs/report-assets/, renders into docs/, tidy the docx
"""
import importlib.metadata
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import make_stand_map
from hide_s3 import hide_s3

HERE = Path(__file__).resolve().parent          # docs/report-build
ASSETS = HERE / "assets"
OUT = HERE.parent / "report-assets"             # docs/report-assets: what the .qmd sources read
DOCS = HERE.parent                              # docs/: the .qmd sources and their renders
OUT.mkdir(exist_ok=True)
ECRXIV = HERE / "ecrxiv"
sys.path.insert(0, str(ECRXIV))
import _common as ecrxiv  # ecRxiv badge and metadata helpers (Python port of _common.R)

SRC = HERE / "src" / "report"
MEDIA = SRC / "media"
FIG = OUT / "figures"
QMD = DOCS / "forest_monitoring_report.qmd"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


YAML = """---
title: "Remote sensed monitoring of olivinskog and kalklindeskog, 2017-2025"
subtitle: "Condition, trajectories and forest loss across all mapped olivinskog and kalklindeskog stands"
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
date: 2026-11-11
date-format: "D MMMM YYYY"
lang: en-GB
bibliography: report-assets/references.bib
csl: report-assets/ambio.csl
callout-icon: false            # as in the ecRxiv template
code-links:
  - text: Code repository
    icon: github
    href: https://github.com/NINAnor/effektovervaking_forests
repo-url: https://github.com/NINAnor/effektovervaking_forests
# The methods appendix is rendered next to the report, so it is linked relatively
methods-url: forest_monitoring_methods.html
number-sections: true
fig-cap-location: bottom
tbl-cap-location: top
format:
  html:
    output-file: index.html
    embed-resources: true
    toc: true
    toc-title: Contents
    toc-depth: 3
    toc-location: left
    smooth-scroll: true
    lightbox: true
  docx:
    reference-doc: report-assets/reference_a4.docx
    columns: 1000             # pipe tables then autofit to their content in Word
    toc: true
    toc-depth: 2
    toc-title: Contents
  typst:
    template-partials:
      - report-assets/typst-template.typ    # Quarto's own, plus a gutter between the author columns
    citeproc: true
    papersize: a4
    margin:
      x: 2.2cm
      y: 2.2cm
    fontsize: 10pt
    toc: true
    toc-depth: 2
    section-numbering: "1.1"
    output-file: forest_monitoring_report.pdf
---
"""

VERSION = "0.1"   # first release; the log below starts here
CITATION = f"Singh, G., Gray, A. M. and Mienna, I. M. 2026. Remote sensed monitoring of olivinskog and kalklindeskog, 2017-2025. Version {VERSION}. Norwegian Institute for Nature Research (NINA)."
LOG = "- 11 Nov. 2026 - v0.1: first release"

# ecRxiv metadata table and badges. Values come from the submission's own front matter so the
# two stay in step; only what differs for the report is overridden here.
META_OVERRIDE = {
    "Version": VERSION,
    "Version comment": "First release (11 Nov. 2026).",
}


def ecrxiv_front() -> tuple[str, str]:
    """Badge row (HTML only) and the collapsible Metadata table, as in the ecRxiv template."""
    y = ecrxiv.read_front_matter(ECRXIV / "NO_FSAT_001_002.qmd")
    vb = FIG / f"badge_version_{VERSION}.svg"
    vb.write_text(ecrxiv.version_badge_svg(VERSION), encoding="utf-8")
    level = lambda k: ecrxiv._level(ecrxiv.flatten_to_string(y.get(k, "none")))
    wiki, badges = ecrxiv.WIKI, ecrxiv.BADGES
    items = [
        ecrxiv._link(ecrxiv.STATUS_BADGE[ecrxiv.flatten_to_string(y["status"]).lower()], f"{wiki}#status-badge"),
        ecrxiv._link(f"report-assets/figures/{vb.name}", f"{wiki}#naming-convention"),
        ecrxiv._link(f"{badges}/badge_data_{level('data_availability')}.svg", f"{wiki}#data-availability"),
        ecrxiv._link(f"{badges}/badge_code_{level('code_reproducibility')}.svg", f"{wiki}#code-reproducibility"),
        ecrxiv._link(f"{badges}/badge_overall_{level('open_science_badge')}.svg", f"{wiki}#open-science-badges"),
        ecrxiv._link(f"{badges}/badge_gplv3.svg", "https://www.gnu.org/licenses/gpl-3.0"),
    ]
    row = '<div style="display:flex;gap:6px;flex-wrap:wrap">' + "".join(items) + "</div>"

    values = {ecrxiv.LABELS.get(k, k): ecrxiv.flatten_to_string(v) for k, v in y.items()}
    values.update(META_OVERRIDE)
    # the submission still carries the 40/20 label count; the report uses 41/19 (stable sort of same-second relabels)
    values["Precision"] = values["Precision"].replace("40 loss, 20 degradation", "41 loss, 19 degradation")
    rows = [f"| {k} | {values[k].replace('|', '/').replace(chr(10), ' ')} |"
            for k in ecrxiv.META_ROWS if values.get(k)]
    table = "| Variable | Value |\n|:---|:---|\n" + "\n".join(rows)
    badge_block = '::: {.content-visible when-format="html"}\n```{=html}\n' + row + "\n```\n:::\n\n"
    meta_block = '::: {.callout-tip collapse="true"}\n## Metadata\n\n' + table + "\n:::\n\n"
    return badge_block, meta_block
# "Analysis code" callouts; the snippets in them were run against the published outputs.
CODE_TRAJ = (HERE / "callouts" / "trajectory.md").read_text(encoding="utf-8").strip()
CODE_LOSS = (HERE / "callouts" / "loss.md").read_text(encoding="utf-8").strip()

# Plain fixes of typos and authoring notes. Each must match once.
REPLACE = [
    ("The companion methods document (docs/condition/METHODS.md)",
     "The [methods appendix]({{< meta methods-url >}})"),
    ("**Condition trajectory** **2017-2025:**", "**Condition trajectory 2017-2025:**"),
    ("attributed toa wet", "attributed to a wet"),
    ("a AE score", "an AE score"),
    ("*A simple overview map will go here.* It will show the 737 stands coloured by condition in 2025. ",
     "Figure 1 shows all 737 stands coloured by condition class in 2025. "),
    ("The stand table behind this report will be written when the report is converted to its final format.",
     "The stand table behind this report will be published with the final version."),
]
# Edits already made in the doc after the last export: applied only while the export lacks them.
DOC_SYNC = [
    ("TBD", "First run pending"),
    ("(OPERA Project 2023)", "(OPERA Project 2024)"),
]
# Plain-text citations -> pandoc citeproc keys in references.bib.
CITES: list[tuple[str, str]] = [
    (
        "(Composite2Change, C2C; Hermosilla mfl. 2015)",
        "[Composite2Change, C2C, @hermosilla2015]",
    ),
    (
        "(Brown mfl. 2025; et årlig sammendrag av flere satellittsignaler for hver 10 m-celle)",
        "[@brown2025, et årlig sammendrag av flere satellittsignaler for hver 10 m-celle]",
    ),
    (
        "(Brown et al. 2025; a yearly summary of multiple satellite signals of each 10 m cell)",
        "[@brown2025, a yearly summary of multiple satellite signals of each 10 m cell]",
    ),
    ("(S-CCD; Ye mfl. 2021)", "[S-CCD, @ye2021]"),
    ("(S-CCD; Ye et al. 2021)", "[S-CCD, @ye2021]"),
    (
        "(Composite2Change, C2C; Hermosilla et al. 2015)",
        "[Composite2Change, C2C, @hermosilla2015]",
    ),
    ("(Hansen et al. 2013)", "[@hansen2013]"),
    ("(Artsdatabanken, 2018)", "[@artsdatabanken2018]"),
    ("(Direktoratet for naturforvaltning, 2011)", "[@dn2011]"),
    ("(Miljødirektoratet, 2025)", "[@miljodirektoratet2025]"),
    ("(Brown et al. 2025)", "[@brown2025]"),
    ("(Zanaga et al. 2022)", "[@zanaga2022]"),
    (
        "(Youden's J: the share of god stands above plus the share of poor stands below is largest; Youden 1950)",
        "[Youden's J: the share of god stands above plus the share of poor stands below is largest, @youden1950]",
    ),
    (
        "(Youden's J: the share of good stands above plus the share of poor stands below is largest; Youden 1950)",
        "[Youden's J: the share of good stands above plus the share of poor stands below is largest, @youden1950]",
    ),
    ("(Theil-Sen slope; Sen 1968)", "[Theil-Sen slope, @sen1968]"),
    (
        "(Composite2Change; Hermosilla et al. 2015)",
        "[Composite2Change, @hermosilla2015]",
    ),
    ("(NBR; Key & Benson 2006)", "[NBR, @keybenson2006]"),
    (
        "(RAdar for Detecting Deforestation, from Sentinel-1; van der Woude et al. 2026)",
        "[RAdar for Detecting Deforestation, from Sentinel-1, @vanderwoude2026]",
    ),
    ("(OPERA Project 2024)", "[@opera2024]"),
    ("(Miljødirektoratet 2025)", "[@miljodirektoratet2025]"),
]

DROP = [
    r"^\*To be formatted in the same citation style.*\*$",
    r"^\*\*Software\.\*\* .*$",
]
UNNUMBERED = ("Sammendrag", "Summary", "References")


def pipe_table(block: str) -> str:
    """Re-space a pipe table: column widths from the longest cell, numbers right-aligned."""
    rows = [[c.strip() for c in l.strip().strip("|").split("|")] for l in block.strip("\n").split("\n")]
    head, body = rows[0], [r for r in rows[2:]]
    num = [all(re.fullmatch(r"[<\\\d.,% ()\-of]*|First run pending", r[i]) for r in body) for i in range(len(head))]
    sep = ["---:" if n else ":---" for n in num]
    out = [head, sep] + body
    return "\n".join("| " + " | ".join(out_r) + " |" for out_r in out) + "\n"


def figure4() -> None:
    """Stack the Figure 4 chart and the four aerial photos into one image."""
    chart = Image.open(MEDIA / "image4.png").convert("RGB")
    W = chart.width
    years = ["2021", "2022", "2023", "2026"]
    labels = ["Intact forest, score 0.57", "Felled, score 0.45",
              "Excavation, score 0.24", "Road and tunnel"]
    gap, pad = 16, 24
    side = (W - 2 * pad - 3 * gap) // 4
    fb, fr = ImageFont.truetype(FONT_B, 26), ImageFont.truetype(FONT, 22)
    strip_h = 40 + side + 44
    out = Image.new("RGB", (W, chart.height + strip_h + 10), "white")
    out.paste(chart, (0, 0))
    d = ImageDraw.Draw(out)
    y0 = chart.height + 10
    for i, n in enumerate([5, 6, 7, 8]):
        x = pad + i * (side + gap)
        d.text((x, y0), years[i], fill="#222", font=fb)
        out.paste(Image.open(MEDIA / f"image{n}.jpeg").convert("RGB").resize((side, side)), (x, y0 + 36))
        d.text((x, y0 + 36 + side + 8), labels[i], fill="#444", font=fr)
    out.save(FIG / "fig4_porsgrunn.png")


def reference_docx() -> None:
    """Pandoc's default Word template, with an A4 page and 2.2 cm margins."""
    tmp = OUT / "reference_default.docx"
    subprocess.run(["pandoc", "-o", str(tmp), "--print-default-data-file", "reference.docx"],
                   check=True)
    dst = OUT / "reference_a4.docx"
    with zipfile.ZipFile(tmp) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                xml = data.decode("utf-8")
                sect = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                        '<w:pgMar w:top="1247" w:right="1247" w:bottom="1247" w:left="1247" '
                        'w:header="708" w:footer="708" w:gutter="0"/></w:sectPr>')
                xml = re.sub(r"<w:sectPr.*?</w:sectPr>|<w:sectPr[^>]*/>", "", xml, flags=re.S)
                xml = xml.replace("</w:body>", sect + "</w:body>")
                data = xml.encode("utf-8")
            zout.writestr(item, data)
    tmp.unlink()


def fix_docx(path: Path) -> None:
    """Quarto nests each captioned table in a centred layout table, so Word shows
    the data table narrow and centred. Give data tables the full text width,
    columns sized by their longest cell, and keep each column's own alignment."""
    TEXT_W = 11906 - 2 * 1247                      # A4 minus the reference-doc margins (twips)
    with zipfile.ZipFile(path) as z:
        items = {i.filename: z.read(i.filename) for i in z.infolist()}
    xml = items["word/document.xml"].decode("utf-8")
    xml = re.sub(r'(<w:jc w:val="(?:left|right)" />)<w:jc w:val="center" />', r"\1", xml)

    def table(m):
        t = m.group(0)
        if '<w:tblW w:type="auto" w:w="0" />' not in t:
            return t
        rows = re.findall(r"<w:tr>.*?</w:tr>", t, re.S)
        cells = [[len("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", c)))
                  for c in re.findall(r"<w:tc>.*?</w:tc>", r, re.S)] for r in rows]
        n = max(len(c) for c in cells)
        need = [min(30, max(r[i] for r in cells if i < len(r))) + 8 for i in range(n)]
        cols = [int(TEXT_W * w / sum(need)) for w in need]
        grid = "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}" />' for w in cols) + "</w:tblGrid>"
        t = re.sub(r"<w:tblGrid>.*?</w:tblGrid>", grid, t, count=1, flags=re.S)
        return t.replace('<w:tblW w:type="auto" w:w="0" />', '<w:tblW w:type="pct" w:w="5000" />', 1)
    # innermost tables only (no nested <w:tbl> inside)
    xml = re.sub(r"<w:tbl>(?:(?!<w:tbl>).)*?</w:tbl>", table, xml, flags=re.S)
    items["word/document.xml"] = xml.encode("utf-8")
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in items.items():
            z.writestr(name, data)
    tmp.replace(path)


def software_md() -> str:
    """Markdown bullet list of Python, key packages, Quarto and pandoc versions."""
    pkgs = [
        "numpy", "pandas", "geopandas", "pyarrow", "scipy", "scikit-learn",
        "earthengine-api", "pyxccd", "rasterio", "shapely", "duckdb", "pillow",
    ]
    lines = [f"- Python {platform.python_version()}"]
    for pkg in pkgs:
        try:
            lines.append(f"- {pkg} {importlib.metadata.version(pkg)}")
        except importlib.metadata.PackageNotFoundError:
            pass
    try:
        q = subprocess.run(["quarto", "--version"], capture_output=True, text=True, check=True)
        lines.append(f"- Quarto {q.stdout.strip()}")
    except Exception:
        pass
    try:
        p = subprocess.run(["pandoc", "--version"], capture_output=True, text=True, check=True)
        p_ver = p.stdout.splitlines()[0].split()[-1]
        lines.append(f"- pandoc {p_ver}")
    except Exception:
        pass
    return "\n".join(lines)


def main() -> None:
    FIG.mkdir(exist_ok=True)
    for n, name in [(1, "fig1_map"), (2, "fig2_score_by_tilstand"),
                    (3, "fig3_year_effect"), (9, "fig5_loss_per_year")]:
        shutil.copy(MEDIA / f"image{n}.png", FIG / f"{name}.png")
    figure4()
    reference_docx()
    for f in ("ambio.csl", "references.bib", "typst-template.typ"):
        shutil.copyfile(ASSETS / f, OUT / f)
    text = hide_s3((SRC / "claude_doc_export.md").read_text(encoding="utf-8"))
    for old, new in REPLACE:
        if text.count(old) == 0:                    # already fixed (or dropped) in the doc
            print("note: fix not needed, text not in the export:", old[:60])
            continue
        assert text.count(old) == 1, old
        text = text.replace(old, new)
    for old, new in DOC_SYNC:
        assert text.count(old) <= 1, old
        text = text.replace(old, new)
    for old, new in CITES:
        if old not in text:                           # the doc no longer uses this form
            print("note: citation not in the export:", old)
            continue
        text = text.replace(old, new)
    lines = text.splitlines()

    # Title block and version log: replaced by the YAML and a status note.
    start = lines.index("## Sammendrag")
    lines = lines[start:]
    lines = [l for l in lines if not any(re.match(p, l) for p in DROP)]
    text = "\n".join(lines)

    # Figures: image + "Source:" line + "**Figure N.** caption" -> one captioned figure.
    figs = {"image1.png": "fig1_map", "image2.png": "fig2_score_by_tilstand",
            "image3.png": "fig3_year_effect", "image9.png": "fig5_loss_per_year"}
    pat = re.compile(r"!\[[^\]]*\]\((?:\./|src/)media/(image\d\.png)\)\{[^}]*\}\n\nSource: (.+?)\n\n"
                     r"\*\*Figure (\d)\.\*\* (.+?)\n")
    def fig(m):
        img, src, n, cap = m.groups()
        return f"![{cap} Source: {src}](report-assets/figures/{figs[img]}.png){{#fig-{n} width=100%}}\n"
    text, nfig = pat.subn(fig, text)
    assert nfig == 4, nfig

    # Figure 4: chart + photo table -> the stacked image.
    pat4 = re.compile(r"!\[[^\]]*\]\((?:\./|src/)media/image4\.png\)\{[^}]*\}\n\nSource: (.+?)\n\n"
                      r"(?:\|.*\n)+\n\*\*Figure 4\.\*\* (.+?)\n")
    m = pat4.search(text)
    assert m, "figure 4"
    text = (text[:m.start()] + f"![{m.group(2)} Source: {m.group(1)}](report-assets/figures/fig4_porsgrunn.png)"
            f"{{#fig-4 width=100%}}\n" + text[m.end():])

    # Tables: "**Table N.** caption" -> a Quarto table caption before the table.
    text, ntab = re.subn(r"^\*\*Table (\d)\.\*\* (.+)$", r": \2 {#tbl-\1}", text, flags=re.M)
    assert ntab == 7, ntab

    text = re.sub(r"(?:^\|.*\n?)+", lambda m: pipe_table(m.group(0)), text, flags=re.M)

    # 2. Headings one level up: record typed numbers to section IDs and promote hashes.
    NUM2ID: dict[str, str] = {}
    def heading(m):
        hashes, title = m.group(1), m.group(2)
        hashes = hashes[1:]
        num_m = re.match(r"^(\d+(?:\.\d+)*)\s+", title)
        if num_m:
            num = num_m.group(1)
            clean_title = title[num_m.end():]
            slug = re.sub(r"[^a-z0-9]+", "-", clean_title.lower()).strip("-")
            sec_id = f"sec-{slug}"
            NUM2ID[num] = sec_id
            return f"{hashes} {clean_title} {{#{sec_id}}}"
        if title == "Appendix":
            return f"{hashes} {title} {{.appendix .unnumbered}}"
        tag = " {.unnumbered}" if title in UNNUMBERED else ""
        return f"{hashes} {title}{tag}"
    text = re.sub(r"^(#{2,4}) (.+)$", heading, text, flags=re.M)

    # 3. Section cross-references: replace with section IDs, except when preceded by "Methods ".
    def sec_ref(m):
        num = m.group(1)
        assert num in NUM2ID, num
        return f"@{NUM2ID[num]}"
    text = re.sub(r"(?<!Methods )\b[Ss]ection (\d+(?:\.\d+)*)\b", sec_ref, text)

    # 4. Figure / Table cross-references in body text (skip captions).
    def replace_refs(line: str) -> str:
        if line.startswith("![") or line.startswith(": "):
            return line
        line = re.sub(r"\bFigure (\d)\b", r"@fig-\1", line)
        line = re.sub(r"\bTable (\d)\b", r"@tbl-\1", line)
        return line
    text = "\n".join(replace_refs(l) for l in text.splitlines())
    for n in re.findall(r"@fig-(\d)", text):
        assert f"{{#fig-{n}" in text, f"fig-{n}"
    for n in re.findall(r"@tbl-(\d)", text):
        assert f"{{#tbl-{n}" in text, f"tbl-{n}"

    # 4b. "Methods section 2.1" -> link to the appendix anchor, as in the kystlynghei report.
    text = re.sub(r"Methods section (\d+)(?:\.(\d+))?",
                  lambda m: f"[Methods appendix, Section {m.group(1)}{'.' + m.group(2) if m.group(2) else ''}]"
                            f"({{{{< meta methods-url >}}}}#section-{m.group(1)}{'-' + m.group(2) if m.group(2) else ''})",
                  text)

    # 5. Tight lists: drop blank lines between list items and normalise marker spacing.
    item_pat = re.compile(r"^(-|\d+\.)\s+")
    lines = text.splitlines()
    lines = [item_pat.sub(r"\1 ", l) if item_pat.match(l) else l for l in lines]
    out = []
    for i, l in enumerate(lines):
        if (l.strip() == "" and i > 0 and item_pat.match(lines[i - 1])
                and i + 1 < len(lines) and item_pat.match(lines[i + 1])):
            continue
        out.append(l)
    text = "\n".join(out)

    # 6. References: replace references list with citeproc div.
    refs_block = "# References {.unnumbered}\n\n::: {#refs}\n:::\n\n"
    text, n = re.subn(r"# References[^\n]*\n.*?(?=# Appendix)", refs_block, text, flags=re.S)
    assert n == 1, "References replacement failed"

    # 7. Appendix: formatted author contributions and dynamic software environment.
    pat_app = re.compile(r"^# Appendix \{\.appendix \.unnumbered\}\n\n## Methodology\n\n(?P<meth>.*?)\n\n## Author contributions\n\n(?P<contrib>.*)\Z", re.M | re.S)
    m = pat_app.search(text)
    assert m, "Appendix pattern not found"
    contrib = m.group("contrib").strip()
    appendix_block = (
        "{{< pagebreak >}}\n\n"    # keeps the Software heading with its box
        "# Appendix {.appendix .unnumbered}\n\n"
        "## Methodology {.unnumbered}\n\n"
        "The full methodology, including the equations, parameters, validation results and known limitations, "
        "is given in the methods appendix, which is updated together with the analysis: "
        "**[Methods: remote sensed monitoring of olivinskog and kalklindeskog, 2017-2025]({{< meta methods-url >}})**.\n\n"
        "## Author contributions {.unnumbered}\n\n"
        f"{contrib}\n\n"
        "## Software {.unnumbered}\n\n"
        "::: {.callout-note collapse=\"true\"}\n"
        "## Python environment\n\n"
        f"{software_md()}\n"
        ":::\n"
    )
    text = text[:m.start()] + appendix_block

    # 8. Front matter: recommended citation and log before Sammendrag.
    badge_block, meta_block = ecrxiv_front()
    front_matter = (
        f"{badge_block}"
        f"> **Recommended citation**: {CITATION}\n\n"
        f"{meta_block}"
        f"::: {{.callout-tip collapse=\"true\"}}\n"
        f"## Log\n\n"
        f"{LOG}\n"
        f":::\n\n"
    )
    assert "# Sammendrag {.unnumbered}" in text
    body = text.replace("# Sammendrag {.unnumbered}", front_matter + "# Sammendrag {.unnumbered}", 1)

    # 9. Analysis code callouts: replace placeholders with module constants.
    assert "*Analysis code: trajectory classification (collapsible in the HTML version).*" in body
    body = body.replace("*Analysis code: trajectory classification (collapsible in the HTML version).*", CODE_TRAJ)
    assert "*Analysis code: loss detection (collapsible in the HTML version).*" in body
    body = body.replace("*Analysis code: loss detection (collapsible in the HTML version).*", CODE_LOSS)

    # 10. Interactive stand map: HTML only; PDF and Word keep the static Figure 1.
    anchor = "until the real series are connected."
    assert body.count(anchor) == 1, anchor
    body = body.replace(anchor, anchor + "\n\n::: {.content-visible when-format=\"html\"}\n"
                        "{{< include report-assets/map/stand_map.md >}}\n:::")
    make_stand_map.main()

    QMD.write_text(YAML + "\n" + body.strip() + "\n", encoding="utf-8")
    print(f"wrote {QMD.name}: {nfig + 1} figures, {ntab} tables")

    subprocess.run(["bash", str(HERE / "render.sh"), QMD.name], cwd=DOCS, check=True)
    fix_docx(DOCS / f"{QMD.stem}.docx")
    print("rendered:", ", ".join(p.name for p in sorted(DOCS.glob(QMD.stem + ".*"))))


if __name__ == "__main__":
    main()
