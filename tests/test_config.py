"""Configuration surface: .env handling, AOI discovery, credentials, publish."""

import os
import shutil
import stat
import subprocess
import sys
import warnings
from pathlib import Path

import pandas as pd
import pytest

from effektovervaking_forests import cdse_auth, confidence, config

CDSE_KEYS = (config.S3_KEY_ENV, config.S3_SECRET_ENV)


def _run_py(code: str, cwd: Path, **env: str) -> str:
    """Run `code` in a fresh interpreter with an explicit minimal environment."""
    full = {"PATH": os.environ.get("PATH", ""), "HOME": str(cwd / "home"), **env}
    return subprocess.run(
        [sys.executable, "-c", code],
        env=full,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# ── read_env_file ────────────────────────────────────────────────────────────
def test_read_env_file_sets_missing_and_respects_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fills unset/empty vars, never overrides a non-empty one."""
    for k in ("T_NEW", "T_SET", "T_EMPTY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("T_SET", "keep")
    monkeypatch.setenv("T_EMPTY", "")
    env = tmp_path / ".env"
    env.write_text("T_NEW=1\nT_SET=other\nT_EMPTY=filled\n")
    config.read_env_file(env)
    assert os.environ["T_NEW"] == "1"
    assert os.environ["T_SET"] == "keep"
    assert os.environ["T_EMPTY"] == "filled"


def test_read_env_file_ignores_noise_and_strips_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for k in ("T_BLANK", "T_Q1", "T_Q2", "T_CDSE_X", "T_OK", "T_NOEQ", "T_C1", "T_C2"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n\nT_BLANK=\nT_NOEQ\nT_Q1=\"a b\"\nT_Q2='c'\n"
        "T_CDSE_X=secret\nT_OK = spaced \n"
        'T_C1="/a b"   # inline\nT_C2=100 # n\n'
    )
    config.read_env_file(env, skip=frozenset({"T_CDSE_X"}))
    assert "T_BLANK" not in os.environ
    assert "T_NOEQ" not in os.environ
    assert os.environ["T_Q1"] == "a b"
    assert os.environ["T_Q2"] == "c"
    assert "T_CDSE_X" not in os.environ
    assert os.environ["T_OK"] == "spaced"
    assert os.environ["T_C1"] == "/a b"
    assert os.environ["T_C2"] == "100"


def test_read_env_file_missing_file_is_noop(tmp_path: Path) -> None:
    config.read_env_file(tmp_path / "nope.env")


# ── import-time .env ─────────────────────────────────────────────────────────
_PRINT_DATA = "from effektovervaking_forests import config; print(config.DATA)"


def test_import_reads_cwd_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"FORESTMON_DATA_DIR={tmp_path / 'from_env'}\n")
    assert Path(_run_py(_PRINT_DATA, tmp_path)) == tmp_path / "from_env"


def test_real_env_var_beats_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"FORESTMON_DATA_DIR={tmp_path / 'from_env'}\n")
    out = _run_py(_PRINT_DATA, tmp_path, FORESTMON_DATA_DIR=str(tmp_path / "real"))
    assert Path(out) == tmp_path / "real"


def test_import_does_not_load_cdse_keys(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("CDSE_S3_ACCESS_KEY=leak\n")
    code = (
        "import os, effektovervaking_forests.config as c; "
        "print(os.environ.get(c.S3_KEY_ENV))"
    )
    assert _run_py(code, tmp_path) == "None"


# ── AOI discovery ────────────────────────────────────────────────────────────
def test_discover_aois_accepts_both_names(tmp_path: Path) -> None:
    (tmp_path / "a.parquet").touch()
    (tmp_path / "b_union.parquet").touch()
    (tmp_path / "ignore.txt").touch()
    found = config._discover_aois(tmp_path)
    assert found == {"a": tmp_path / "a.parquet", "b": tmp_path / "b_union.parquet"}


def test_discover_aois_missing_dir(tmp_path: Path) -> None:
    assert config._discover_aois(tmp_path / "absent") == {}


def test_aoi_path_known_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(config, "AOI_PARQUET", {"x": tmp_path / "x.parquet"})
    assert config.aoi_path("x") == tmp_path / "x.parquet"


def test_aoi_path_unknown_mentions_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "AOI_PARQUET", {})
    with pytest.raises(KeyError, match="FORESTMON_AOI_DIR"):
        config.aoi_path("zzz")


# ── cdse_auth precedence ─────────────────────────────────────────────────────
# Precedence is the subject here; the world-readable warning has its own tests.
pytestmark = pytest.mark.filterwarnings("ignore:.*readable:UserWarning")


@pytest.fixture
def cred_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    for k in CDSE_KEYS:
        monkeypatch.delenv(k, raising=False)
    home, dotenv = tmp_path / "creds", tmp_path / ".env"
    monkeypatch.setattr(cdse_auth, "CRED_FILES", (home, dotenv))
    return home, dotenv


def test_home_credentials_beat_dotenv(cred_files: tuple[Path, Path]) -> None:
    home, dotenv = cred_files
    home.write_text(f"{config.S3_KEY_ENV}=home\n{config.S3_SECRET_ENV}=hs\n")
    dotenv.write_text(f"{config.S3_KEY_ENV}=dot\n{config.S3_SECRET_ENV}=ds\n")
    assert cdse_auth.s3_credentials() == ("home", "hs")


def test_empty_placeholder_does_not_mask_home(cred_files: tuple[Path, Path]) -> None:
    home, dotenv = cred_files
    home.write_text(f"{config.S3_KEY_ENV}=home\n{config.S3_SECRET_ENV}=hs\n")
    dotenv.write_text(f"{config.S3_KEY_ENV}=\n{config.S3_SECRET_ENV}=\n")
    assert cdse_auth.s3_credentials() == ("home", "hs")


def test_environment_beats_files(
    cred_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, dotenv = cred_files
    home.write_text(f"{config.S3_KEY_ENV}=home\n{config.S3_SECRET_ENV}=hs\n")
    dotenv.write_text(f"{config.S3_KEY_ENV}=dot\n")
    monkeypatch.setenv(config.S3_KEY_ENV, "env")
    monkeypatch.setenv(config.S3_SECRET_ENV, "es")
    assert cdse_auth.s3_credentials() == ("env", "es")
    assert cdse_auth.have_credentials()


def test_no_credentials_raises(cred_files: tuple[Path, Path]) -> None:
    assert not cdse_auth.have_credentials()


# ── world-readable warning ───────────────────────────────────────────────────
def _readable_env(tmp_path: Path, text: str) -> Path:
    env = tmp_path / ".env"
    env.write_text(text)
    env.chmod(0o644)
    if not env.stat().st_mode & stat.S_IROTH:
        pytest.skip("filesystem does not honour chmod")
    return env


def test_no_warning_for_forestmon_only_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _readable_env(tmp_path, "FORESTMON_DATA_DIR=/x\n")
    monkeypatch.setattr(cdse_auth, "CRED_FILES", (tmp_path / "none", env))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cdse_auth._warn_if_world_readable()


def test_warning_when_dotenv_holds_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _readable_env(tmp_path, f"{config.S3_KEY_ENV}=abc\n")
    monkeypatch.setattr(cdse_auth, "CRED_FILES", (tmp_path / "none", env))
    with pytest.warns(UserWarning, match="readable"):
        cdse_auth._warn_if_world_readable()


# ── opera_agreement default path ─────────────────────────────────────────────
def _breaks() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "lon": [10.0, 10.5],
            "lat": [60.0, 60.0],
            "break_date": pd.to_datetime(["2025-06-01", "2025-06-01"]),
        }
    )


def test_opera_agreement_uses_configured_parquet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pq = tmp_path / "opera.parquet"
    pd.DataFrame(
        {"lon": [10.0], "lat": [60.0], "veg_dist_date": pd.to_datetime(["2025-06-10"])}
    ).to_parquet(pq)
    monkeypatch.setattr(config, "OPERA_PARQUET", pq)
    assert confidence.opera_agreement(_breaks()).tolist() == [True, False]


def test_opera_agreement_missing_parquet_is_all_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(config, "OPERA_PARQUET", tmp_path / "absent.parquet")
    assert confidence.opera_agreement(_breaks()).tolist() == [False, False]


# ── ensure_dirs ──────────────────────────────────────────────────────────────
def test_ensure_dirs_creates_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    names = ("OPS", "GRID_DIR", "CATALOG_DIR", "STATE_DIR", "OBS_DIR", "ALERTS_DIR")
    names += ("RUNS_DIR",)
    for n in names:
        monkeypatch.setattr(config, n, tmp_path / "ops" / n)
    config.ensure_dirs()
    for n in names:
        assert (tmp_path / "ops" / n).is_dir()


# ── cron script .env parser ──────────────────────────────────────────────────
@pytest.mark.skipif(
    not (shutil.which("flock") and shutil.which("realpath")),
    reason="needs flock and realpath",
)
def test_cycle_script_parses_dotenv(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    script = repo / "scripts" / "forestmon_cycle.sh"
    shutil.copy(Path(__file__).parent.parent / "scripts" / "forestmon_cycle.sh", script)
    web = tmp_path / "web"
    (repo / ".env").write_text(
        "   # indented comment\n"
        f'FORESTMON_WEB_OUT="{web}"   # inline\n'
        "FORESTMON_DATA_DIR=./rel\n"
        "FORESTMON_GIS_OUT=\n"
        "CDSE_S3_ACCESS_KEY=nope\n"
    )
    fake = tmp_path / "fake_forestmon"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'echo "FAKE web=[$FORESTMON_WEB_OUT] gis=[${FORESTMON_GIS_OUT-unset}]'
        ' cdse=[${CDSE_S3_ACCESS_KEY:-}] args=[$*]"\n'
    )
    fake.chmod(0o755)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "FORESTMON_BIN": str(fake),
        "FORESTMON_AOIS": "a1",
        "FORESTMON_LOCK": str(tmp_path / "lock"),
        "FORESTMON_PUSH": "0",
    }
    proc = subprocess.run(
        [shutil.which("bash") or "/bin/bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    log = (repo / "rel" / "logs" / "cycle.log").read_text()
    assert f"FAKE web=[{web}] gis=[unset] cdse=[]" in log
    assert "args=[run a1]" in log


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_cycle_script_is_executable_in_git() -> None:
    # A CIFS checkout shows every file as 755, so check the mode git records.
    root = Path(__file__).parent.parent
    out = subprocess.run(
        [shutil.which("git") or "git", "ls-files", "-s", "scripts/forestmon_cycle.sh"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    if not out:
        pytest.skip("not a git checkout")
    assert out.startswith("100755"), out
