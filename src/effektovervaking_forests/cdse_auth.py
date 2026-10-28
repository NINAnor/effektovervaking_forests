"""CDSE credentials.

CDSE has two independent credential systems and they are easy to confuse:

* **S3 keys** (access key + secret) generated at
  https://eodata-s3keysmanager.dataspace.copernicus.eu/ — these read the
  `s3://eodata` archive directly and are what this package uses. They allow
  byte-range reads, which is the whole point: we need a few hundred pixels out
  of a 100 MB JP2, not the file.
* **Account username/password** (OIDC), used by the OData/zipper endpoints.
  Those serve whole files, so they are a fallback, not a plan.

Keys are read from the environment, then from `~/.config/cdse/credentials`,
then from a `.env` in the working directory. The home-directory file is the intended
place and the one `forestmon` creates: the project drive is a network mount
that silently forces mode 755, so a secret stored there is readable by every
member of the group. $HOME is ext4 and honours 600.

Keys are never written to any artefact this package produces.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

from . import config

# In precedence order. Anything already in the environment wins over both, so a
# systemd unit or a container can inject credentials without touching disk.
CRED_FILES = (Path.home() / ".config" / "cdse" / "credentials", Path.cwd() / ".env")


def _load_files() -> None:
    for path in CRED_FILES:
        config.read_env_file(path)


def _warn_if_world_readable() -> None:
    """A credentials file on the shared project drive is a real exposure.

    chmod does not stick there (the mount forces 755), so this cannot be fixed
    in place — it can only be moved. Say so rather than failing silently.
    """
    repo_env = CRED_FILES[1]
    if not repo_env.is_file() or not (repo_env.stat().st_mode & 0o077):
        return
    # A .env holding only FORESTMON_* paths is fine to share; keys are not.
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

    `default_block_size` matters more than it looks: rasterio reads JP2 headers
    and then scattered tiles, so an oversized block turns a 4 KB window read
    into megabytes of transfer.
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
