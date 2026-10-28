"""The condition pipeline's steps, in run order, and the runner behind
`forestmon condition run`.

Each step is a module with `main(argv)` and its own argparse interface, ported
unchanged from the deployed `condition/deploy/scripts/NN_*.py`; the step id is
the old script's number, so the research notes and METHODS.md still point at
the right code. `run_pipeline` replaces `run_all.sh`: each step runs in its own
interpreter (as the shell script ran them, so memory is returned between the
long steps and 04's spawned workers import a clean module), its output is teed
to `<FORESTMON_LOG_DIR>/condition/`, and the first failure stops the run,
because every later step consumes the previous one's table.
"""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Step:
    id: str
    module: str
    summary: str
    default_args: tuple[str, ...] = ()
    needs_ee: bool = False  # skipped by `run` unless an EE project is given
    in_run: bool = True  # part of `run`; False = invoked only explicitly


STEPS: tuple[Step, ...] = (
    # stage A: inputs and plan
    Step("00", "s00_build_polygons", "polygons, roles, survey years, blocks"),
    Step("01", "s01_plan_reads", "which AlphaEarth blocks the AOI needs"),
    # stage B: embeddings (02 and 02d are the downloads; both resume)
    Step("02", "s02_extract_embeddings", "download stand pixels", ("--workers", "6")),
    Step("02b", "s02b_membership", "overlap-safe pixel <-> stand model"),
    Step("02c", "s02c_plan_bad_pool", "set-cover for the transformed pool"),
    Step(
        "02d", "s02d_extract_bad_pool", "download transformed pool", ("--workers", "6")
    ),
    # stage C: the independent disturbance reference (optional, needs EE)
    Step("02e", "s02e_hansen_reference", "Hansen loss year per pixel", needs_ee=True),
    # stage D: 04 before 03 -- the trajectory reads the yearly scores 04 writes
    Step("04", "s04_condition", "STEP 2: condition score per pixel and stand"),
    Step("03", "s03_trajectory", "STEP 1: trajectory class per pixel and stand"),
    # stage E: validation and products
    Step("05", "s05_validate", "checks against the field labels"),
    Step("06", "s06_rasterize", "GeoTIFF per spatial block"),
    Step("07", "s07_package", "one GeoPackage for QGIS"),
    Step("08", "s08_report_data", "report numbers and HTML report"),
    # once per deployment, never in `run`
    Step(
        "09",
        "s09_check_gee_parity",
        "mirror vs Earth Engine",
        needs_ee=True,
        in_run=False,
    ),
)
BY_ID = {s.id: s for s in STEPS}


def module_name(step: Step) -> str:
    return f"{__name__}.{step.module}"


def run_step(step_id: str, argv: list[str] | None = None) -> None:
    """Run one step in this process."""
    from effektovervaking_forests.condition import paths

    if not {"-h", "--help"} & set(argv or ()):
        paths.ensure_dirs()
    sys.argv[0] = f"forestmon condition step {step_id}"  # argparse's prog name
    importlib.import_module(module_name(BY_ID[step_id])).main(argv)


def plan(
    start: str | None = None,
    stop: str | None = None,
    ee_project: str | None = None,
) -> tuple[list[tuple[Step, list[str]]], list[Step]]:
    """The steps `run` would execute, with their arguments, in order -- and the
    Earth Engine steps in that slice it has to skip (no project, or no `ee`)."""
    ids = [s.id for s in STEPS if s.in_run]
    for name, val in (("--from", start), ("--to", stop)):
        if val is not None and val not in ids:
            raise ValueError(f"{name} {val!r}: not a pipeline step ({', '.join(ids)})")
    lo = ids.index(start) if start else 0
    hi = ids.index(stop) if stop else len(ids) - 1
    if lo > hi:
        raise ValueError(f"--from {start} comes after --to {stop}")
    have_ee = bool(ee_project) and importlib.util.find_spec("ee") is not None
    todo, skipped = [], []
    for s in [BY_ID[i] for i in ids[lo : hi + 1]]:
        args = list(s.default_args)
        if s.needs_ee:
            if not have_ee:
                skipped.append(s)
                continue
            args += ["--project", ee_project]
        todo.append((s, args))
    return todo, skipped


def run_pipeline(
    steps: list[tuple[Step, list[str]]],
    log_dir: Path,
    echo=print,
) -> int:
    """Run `steps` in subprocesses, teeing each to a log. Returns the exit code
    of the first failing step, or 0."""
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    for step, args in steps:
        log = log_dir / f"{stamp}_{step.id}_{step.module.split('_', 1)[1]}.log"
        echo(f"\n=== {step.id} {step.module} {' '.join(args)}".rstrip())
        # -u: the step's prints reach the log as they happen, and survive an
        # OOM kill of a long step.
        cmd = [sys.executable, "-u", "-m", __name__, step.id, *args]
        with (
            log.open("w", encoding="utf-8", buffering=1) as fh,
            subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            ) as proc,
        ):
            for line in proc.stdout:
                sys.stdout.write(line)
                fh.write(line)
        if proc.returncode:
            echo(f"step {step.id} failed (exit {proc.returncode}); log: {log}")
            return proc.returncode
    return 0
