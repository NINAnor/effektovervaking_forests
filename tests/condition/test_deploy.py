"""Deployment contract of the condition pipeline: vendored code, layout, steps."""

import hashlib
import importlib
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from effektovervaking_forests.cli import app
from effektovervaking_forests.condition import steps

runner = CliRunner()


@pytest.fixture
def no_data_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI callback creates the forestmon data tree; not in the checkout."""
    from effektovervaking_forests import config

    monkeypatch.setattr(config, "ensure_dirs", lambda: None)


# SHA-256 of condition/deploy/src/condition/core.py in Geethen_forests
# (deploy/MANIFEST.json, commit 5b93f83). core.py is vendored verbatim from
# DegreeofRecovery paper_release: if this fails, re-copy it, do not edit it.
CORE_SHA256 = "6c0f5d96976c2ecb8df7dfd4c7e3d40489f109e9c9662c61b8809b7a9750a06d"


def test_core_is_vendored_verbatim() -> None:
    import effektovervaking_forests.condition.core as core

    digest = hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest()
    assert digest == CORE_SHA256


def _python(code: str, cwd: Path, **env: str) -> str:
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(cwd / "home"), **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_import_creates_no_directories(tmp_path: Path) -> None:
    """No library module or step creates the data tree on import."""
    code = (
        "import importlib, pkgutil\n"
        "import effektovervaking_forests.condition as c\n"
        "from effektovervaking_forests.condition import steps\n"
        "for m in pkgutil.iter_modules(c.__path__):\n"
        "    importlib.import_module(f'{c.__name__}.{m.name}')\n"
        "for s in steps.STEPS:\n"
        "    importlib.import_module(steps.module_name(s))\n"
    )
    _python(code, tmp_path)
    # $HOME (= tmp_path/home) may hold numba's JIT cache: not the data tree.
    assert [p.name for p in tmp_path.iterdir() if p.name != "home"] == []


def test_root_defaults_under_forestmon_data_dir(tmp_path: Path) -> None:
    code = "from effektovervaking_forests.condition import paths; print(paths.ROOT)"
    assert _python(code, tmp_path, FORESTMON_DATA_DIR=str(tmp_path / "d")) == str(
        tmp_path / "d" / "condition"
    )
    assert _python(code, tmp_path, CONDITION_ROOT=str(tmp_path / "c")) == str(
        tmp_path / "c"
    )


def test_ensure_dirs_builds_the_tree(tmp_path: Path) -> None:
    code = (
        "from effektovervaking_forests.condition import paths; paths.ensure_dirs(); "
        "print(paths.OUT_TIF.is_dir() and paths.CACHE.is_dir())"
    )
    assert _python(code, tmp_path, CONDITION_ROOT=str(tmp_path / "c")) == "True"


@pytest.mark.parametrize("step", steps.STEPS, ids=lambda s: s.id)
def test_every_step_imports_and_takes_argv(step: steps.Step) -> None:
    mod = importlib.import_module(steps.module_name(step))
    assert list(inspect.signature(mod.main).parameters) == ["argv"]


def test_step_order() -> None:
    ids = [s.id for s in steps.STEPS]
    assert len(ids) == len(set(ids))
    # 03 reads the yearly scores 04 writes.
    assert ids.index("04") < ids.index("03")


def test_plan_slices_and_skips_earth_engine() -> None:
    todo, skipped = steps.plan("02d", "04", ee_project=None)
    assert [s.id for s, _ in todo] == ["02d", "04"]
    assert [s.id for s in skipped] == ["02e"]
    assert dict((s.id, a) for s, a in todo)["02d"] == ["--workers", "6"]
    todo, skipped = steps.plan("04", "07")
    assert [s.id for s, _ in todo] == ["04", "03", "05", "06", "07"]
    assert skipped == []


def test_plan_passes_ee_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(steps.importlib.util, "find_spec", lambda name: object())
    todo, skipped = steps.plan("02e", "02e", ee_project="my-proj")
    assert [(s.id, a) for s, a in todo] == [("02e", ["--project", "my-proj"])]
    assert skipped == []


@pytest.mark.parametrize(
    ("start", "stop"), [("09", None), ("xx", None), (None, "02z"), ("05", "04")]
)
def test_plan_rejects_bad_slices(start: str | None, stop: str | None) -> None:
    with pytest.raises(ValueError):
        steps.plan(start, stop)


def test_run_pipeline_stops_at_first_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing step's exit code is returned and later steps never start."""
    ran = []

    class FakeProc:
        def __init__(self, cmd, **_):
            ran.append(cmd[4])
            self.stdout = iter([f"step {cmd[4]}\n"])
            self.returncode = 3 if cmd[4] == "03" else 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(steps.subprocess, "Popen", FakeProc)
    todo, _ = steps.plan("04", "06")
    assert steps.run_pipeline(todo, tmp_path, echo=lambda _: None) == 3
    assert ran == ["04", "03"]
    logs = sorted(p.name.split("_", 2)[2] for p in tmp_path.iterdir())
    assert logs == ["03_trajectory.log", "04_condition.log"]


@pytest.mark.usefixtures("no_data_tree")
def test_cli_lists_steps_and_dry_runs() -> None:
    result = runner.invoke(app, ["condition", "steps"])
    assert result.exit_code == 0
    assert "s04_condition" in result.output
    result = runner.invoke(
        app, ["condition", "run", "--dry-run", "--from", "04", "--to", "05"]
    )
    assert result.exit_code == 0, result.output
    assert "s03_trajectory" in result.output
    assert "s06_rasterize" not in result.output


@pytest.mark.usefixtures("no_data_tree")
def test_cli_rejects_unknown_step() -> None:
    result = runner.invoke(app, ["condition", "step", "99"])
    assert result.exit_code == 1
    result = runner.invoke(app, ["condition", "run", "--from", "09"])
    assert result.exit_code == 1
