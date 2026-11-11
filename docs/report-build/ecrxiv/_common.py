"""Python port of ecRxiv's `_common.R` (template branch).

Same job, same outputs: read the document's top YAML, build the metadata table,
print the status / version / open-science / licence badges, and assemble the
recommended citation. Import it from the first chunk of a Python-engine qmd:

    import sys, pathlib
    sys.path.insert(0, str(next(p for p in [pathlib.Path.cwd(), *pathlib.Path.cwd().parents]
                                if (p / "_common.py").exists())))
    from _common import load_document
    doc = load_document()

Dependencies: pyyaml, pandas, tabulate, IPython (all in a standard Jupyter env).
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yaml
from IPython.display import HTML, Markdown

BADGES = "https://raw.githubusercontent.com/NINAnor/ecRxiv/main/docs"
WIKI = "https://github.com/NINAnor/ecRxiv/wiki"

STATUS_BADGE = {
    "complete": f"{BADGES}/badge_status_operational.svg",
    "incomplete": f"{BADGES}/badge_status_under_development.svg",
    "deprecated": f"{BADGES}/badge_status_deprecated.svg",
}
LEVELS = ("gold", "silver", "bronze", "none")

# YAML key -> label shown in the metadata table (as in _common.R's recode)
LABELS = {
    "indicatorID": "Indicator ID",
    "indicatorName": "Indicator Name",
    "continent": "Continent",
    "country": "Country",
    "ECT": "Ecosystem Condition Typology Class",
    "yearAdded": "Year added",
    "yearLastUpdate": "Last update",
    "VersionComment": "Version comment",
    "SpatialAggregationPathway": "Spatial aggregation pathway",
}
META_ROWS = [
    "Indicator ID", "Indicator Name", "verbatimName", "Continent", "Country",
    "Ecosystem Condition Typology Class", "Realm", "Biome", "Ecosystem",
    "verbatimEcosystem", "Year added", "Last update", "Version",
    "Version comment", "Normalised", "Spatial aggregation pathway", "Precision",
]


def find_root(start: Path | None = None) -> Path:
    """Project root, like here::here(): the nearest folder holding .here or .git."""
    start = (start or Path.cwd()).resolve()
    for p in [start, *start.parents]:
        if (p / ".here").exists() or (p / ".git").exists():
            return p
    return start


def find_qmd(path: str | Path | None = None) -> Path:
    """The document being rendered. Quarto runs Jupyter chunks with the
    document's folder as the working directory but does not pass the file
    name, so take the only .qmd there unless told otherwise."""
    if path is not None:
        return Path(path).resolve()
    qmds = sorted(Path.cwd().glob("*.qmd"))
    if len(qmds) != 1:
        raise RuntimeError(
            f"found {len(qmds)} .qmd files in {Path.cwd()}; "
            "pass the file name: load_document('MY_ID.qmd')"
        )
    return qmds[0].resolve()


def read_front_matter(qmd: Path) -> dict:
    lines = qmd.read_text(encoding="utf-8").splitlines()
    dashes = [i for i, line in enumerate(lines) if line.strip() == "---"]
    return yaml.safe_load("\n".join(lines[dashes[0] + 1 : dashes[1]])) or {}


def flatten_to_string(x) -> str:
    """Nested lists/dicts -> one '; '-joined string (as _common.R)."""
    out: list[str] = []

    def walk(v):
        if isinstance(v, dict):
            for w in v.values():
                walk(w)
        elif isinstance(v, (list, tuple)):
            for w in v:
                walk(w)
        elif v is not None:
            out.append(str(v))

    walk(x)
    return "; ".join(out)


def _link(image: str, url: str) -> str:
    return (
        f'<a href="{url}" target="_blank" rel="noopener noreferrer">'
        f'<img src="{image}" height="20px"></a>'
    )


def _level(value) -> str:
    v = str(value).strip().lower()
    if v not in LEVELS:
        raise ValueError(f"badge level must be one of {LEVELS}, got {value!r}")
    return v


def version_badge_svg(version: str, color: str = "#add8e6") -> str:
    """A shields-style 'Version | x.y.z' badge (replaces anybadger)."""
    label, value = "Version", str(version)
    lw, vw = 6 * len(label) + 10, 6 * len(value) + 10
    w = lw + vw
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" role="img" '
        f'aria-label="{label}: {html.escape(value)}">'
        f'<rect width="{lw}" height="20" fill="#555"/>'
        f'<rect x="{lw}" width="{vw}" height="20" fill="{color}"/>'
        '<g fill="#fff" text-anchor="middle" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">'
        f'<text x="{lw / 2}" y="14">{label}</text>'
        f'<text x="{lw + vw / 2}" y="14" fill="#333">{html.escape(value)}</text>'
        "</g></svg>"
    )


@dataclass
class Document:
    qmd: Path
    root: Path
    yaml: dict
    meta: pd.DataFrame

    def _get(self, key: str) -> str:
        return flatten_to_string(self.yaml.get(key, ""))

    # --- the values _common.R exposes as globals ---------------------------
    @property
    def status(self) -> str:
        return self._get("status")

    @property
    def version(self) -> str:
        return self._get("Version")

    @property
    def authors(self) -> str:
        return self._get("AuthorList")

    @property
    def year(self) -> str:
        return self._get("yearLastUpdate")

    @property
    def name(self) -> str:
        return self._get("indicatorName")

    @property
    def folder_name(self) -> str:
        return self._get("folderName")

    @property
    def url(self) -> str:
        return self._get("url")

    # --- rendered pieces ---------------------------------------------------
    def reviewers(self) -> HTML:
        revs = self.yaml.get("codeReviewers") or []
        if not revs:
            return HTML("")
        names = "; ".join(r["name"] for r in revs)
        return HTML(
            '<div id="title-block-header" class="quarto-title-block default">'
            '<div class="quarto-title-meta"><div>'
            '<div class="quarto-title-meta-heading">Reviewed by</div>'
            f'<div class="quarto-title-meta-contents">{html.escape(names)}</div>'
            "</div></div></div>"
        )

    def badges(self) -> HTML:
        """All six badges in one row (status, version, data, code, open science, licence)."""
        img_dir = self.root / "img"
        img_dir.mkdir(exist_ok=True)
        vb = img_dir / f"badge_version_{self.version}.svg"
        vb.write_text(version_badge_svg(self.version), encoding="utf-8")
        rel = Path("..") / "img" / vb.name if self.qmd.parent != self.root else Path("img") / vb.name

        status = self.status.strip().lower()
        if status not in STATUS_BADGE:
            raise ValueError(f"status must be one of {list(STATUS_BADGE)}, got {status!r}")
        items = [
            _link(STATUS_BADGE[status], f"{WIKI}#status-badge"),
            _link(rel.as_posix(), f"{WIKI}#naming-convention"),
            _link(f"{BADGES}/badge_data_{_level(self._get('data_availability'))}.svg",
                  f"{WIKI}#data-availability"),
            _link(f"{BADGES}/badge_code_{_level(self._get('code_reproducibility'))}.svg",
                  f"{WIKI}#code-reproducibility"),
            _link(f"{BADGES}/badge_overall_{_level(self._get('open_science_badge'))}.svg",
                  f"{WIKI}#open-science-badges"),
            _link(f"{BADGES}/badge_gplv3.svg", "https://www.gnu.org/licenses/gpl-3.0"),
        ]
        return HTML('<div style="display:flex;gap:6px;flex-wrap:wrap">' + "".join(items) + "</div>")

    def citation(self) -> str:
        return (
            f"{self.authors} {self.year}. {self.name} (ID(s): {self.folder_name}) "
            f"v. {self.version}. ecRxiv: {self.url}"
        )

    def meta_table(self) -> Markdown:
        m = self.meta[self.meta["Variable"].isin(META_ROWS)]
        return Markdown(m.to_markdown(index=False))


def load_document(path: str | Path | None = None) -> Document:
    qmd = find_qmd(path)
    y = read_front_matter(qmd)
    meta = pd.DataFrame(
        [(LABELS.get(k, k), flatten_to_string(v)) for k, v in y.items() if k != "authors"],
        columns=["Variable", "Value"],
    )
    return Document(qmd=qmd, root=find_root(qmd.parent), yaml=y, meta=meta)


def session_info(packages=("pandas", "numpy", "yaml", "IPython")) -> str:
    """Python counterpart of sessionInfo()."""
    import platform
    import sys
    from importlib.metadata import PackageNotFoundError, version

    rows = [f"Python {sys.version.split()[0]} ({platform.platform()})",
            f"executable: {sys.executable}"]
    for p in packages:
        dist = {"yaml": "PyYAML", "IPython": "ipython"}.get(p, p)
        try:
            rows.append(f"{p} {version(dist)}")
        except PackageNotFoundError:
            rows.append(f"{p} (not installed)")
    return "\n".join(rows)
