"""Give the methods docx tables the full text width, columns sized by content.

Like report/build.py's fix_docx, but every column is at least as wide as its
longest word, so Word never breaks "0.236" or "representation" mid-word.

    python fix_docx_tables.py condition_methods.docx
"""
import re
import sys
import zipfile
from pathlib import Path

TEXT_W = 11906 - 2 * 1247          # A4 minus the reference-doc margins (twips)


def cell_text(tc: str) -> str:
    return "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", tc))


def table(m: re.Match) -> str:
    t = m.group(0)
    if '<w:tblW w:type="auto" w:w="0" />' not in t:
        return t
    rows = [[cell_text(c) for c in re.findall(r"<w:tc>.*?</w:tc>", r, re.S)]
            for r in re.findall(r"<w:tr>.*?</w:tr>", t, re.S)]
    n = max(len(r) for r in rows)
    col = [[r[i] for r in rows if i < len(r)] for i in range(n)]
    word = [max((len(w) for c in cells for w in c.split()), default=1) for cells in col]
    text = [min(24, max(len(c) for c in cells)) for cells in col]
    need = [max(w + 3, t) + 5 for w, t in zip(word, text)]   # +5: cell margins at 10 pt
    cols = [int(TEXT_W * w / sum(need)) for w in need]
    grid = "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}" />' for w in cols) + "</w:tblGrid>"
    t = re.sub(r"<w:tblGrid>.*?</w:tblGrid>", grid, t, count=1, flags=re.S)
    return t.replace('<w:tblW w:type="auto" w:w="0" />', '<w:tblW w:type="pct" w:w="5000" />', 1)


def main(path: Path) -> None:
    with zipfile.ZipFile(path) as z:
        items = {i.filename: z.read(i.filename) for i in z.infolist()}
    xml = items["word/document.xml"].decode("utf-8")
    xml = re.sub(r"<w:tbl>(?:(?!<w:tbl>).)*?</w:tbl>", table, xml, flags=re.S)
    items["word/document.xml"] = xml.encode("utf-8")
    tmp = path.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in items.items():
            z.writestr(name, data)
    tmp.replace(path)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
