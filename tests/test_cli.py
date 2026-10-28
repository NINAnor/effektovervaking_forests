"""CLI smoke tests."""

import os
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from effektovervaking_forests import config
from effektovervaking_forests.cli import app

runner = CliRunner()


def test_help_lists_pipeline_stages() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for group in ("grid", "catalog", "obs", "sccd", "run", "alerts", "health"):
        assert group in result.output


def test_import_has_no_filesystem_side_effects(tmp_path: Path) -> None:
    """Importing the package must not create the data tree."""
    subprocess.run(
        [sys.executable, "-c", "import effektovervaking_forests.cli"],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home")},
        check=True,
    )
    assert list(tmp_path.iterdir()) == []


def test_data_dir_comes_from_environment(tmp_path: Path) -> None:
    code = "from effektovervaking_forests import config; print(config.OPS)"
    out = subprocess.run(
        [sys.executable, "-c", code],
        env={"FORESTMON_DATA_DIR": str(tmp_path), "PATH": ""},
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert Path(out) == tmp_path / "operational"


def test_unknown_aoi_names_the_search_dir() -> None:
    try:
        config.aoi_path("no-such-aoi")
    except KeyError as e:
        assert "FORESTMON_AOI_DIR" in str(e)
    else:
        raise AssertionError("expected KeyError")
