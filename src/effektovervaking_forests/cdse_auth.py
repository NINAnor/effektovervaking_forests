"""CDSE S3 credentials (access key + secret for `s3://eodata`).

Keys are read from the environment, then `~/.config/cdse/credentials`, then a
`.env` in the working directory. Prefer the home-directory file: the project
drive is a network mount that forces mode 755. Keys are never written to any
artefact this package produces.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

from . import config

# Anything already in the environment wins over both files.
CRED_FILES = (Path.home() / ".config" / "cdse" / "credentials", Path.cwd() / ".env")


def _load_files() -> None:
    for path in CRED_FILES:
        config.read_env_file(path)


def _warn_if_world_readable() -> None:
    """Warn if a `.env` holding keys is group/world readable (chmod cannot fix)."""
    repo_env = CRED_FILES[1]
    if not repo_env.is_file() or not (repo_env.stat().st_mode & 0o077):
        return
    # Only keys matter; a .env with just paths is fine.
    if any(
        line.strip().startswith((config.S3_KEY_ENV, config.S3_SECRET_ENV))
        for line in repo_env.read_text().splitlines()
    ):
        warnings.warn(
            f"{repo_env} is group/world readable and cannot be chmod'd on this "
            f"filesystem. Move the keys to {CRED_FILES[0]} (mode 600).",
            stacklevel=2,
        )


def s3_credentials() -> tuple[str, str]:
    """(access_key, secret_key) for s3://eodata, or a clear error explaining setup."""
    _load_files()
    _warn_if_world_readable()
    key = os.environ.get(config.S3_KEY_ENV)
    secret = os.environ.get(config.S3_SECRET_ENV)
    if not key or not secret:
        raise RuntimeError(
            "CDSE S3 credentials not found.\n"
            "  1. Register at https://dataspace.copernicus.eu/ (free).\n"
            "  2. Generate S3 keys at "
            "https://eodata-s3keysmanager.dataspace.copernicus.eu/\n"
            f"  3. Put them in {CRED_FILES[0]} (mode 600):\n"
            f"       {config.S3_KEY_ENV}=...\n"
            f"       {config.S3_SECRET_ENV}=...\n"
            "     or export them in the service unit's environment.\n"
            "     Do NOT put them on the project drive: it forces mode 755."
        )
    return key, secret


def s3_filesystem():
    """An s3fs filesystem pointed at CDSE's object store.

    The 1 MiB block size is deliberately small: JP2 reads are scattered windows.
    """
    import s3fs

    key, secret = s3_credentials()
    return s3fs.S3FileSystem(
        key=key,
        secret=secret,
        client_kwargs={"endpoint_url": config.S3_ENDPOINT},
        config_kwargs={"signature_version": "s3v4"},
        default_block_size=1 * 1024 * 1024,
    )


def have_credentials() -> bool:
    try:
        s3_credentials()
        return True
    except RuntimeError:
        return False
