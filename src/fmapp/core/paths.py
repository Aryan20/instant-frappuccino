"""Filesystem locations, resolved per-OS.

FM's own layout (``~/frappe`` by default, overridable with ``FRAPPE_MANAGER_HOME``) is the
same on macOS and Linux. Our own config/cache dirs follow each platform's convention via
platformdirs (``~/Library/Application Support`` on macOS, XDG dirs on Linux).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from platformdirs import user_cache_path, user_config_path

from fmapp import APP_ID

LEGACY_APP_ID = "fm-app"  # the app's name before it became Instant Frappuccino


def fm_home() -> Path:
    env = os.environ.get("FRAPPE_MANAGER_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / "frappe"


def benches_dir() -> Path:
    return fm_home() / "sites"


def config_dir() -> Path:
    path = user_config_path(APP_ID, appauthor=False)
    if not path.exists():
        migrate_legacy(user_config_path(LEGACY_APP_ID, appauthor=False), path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def migrate_legacy(old: Path, new: Path) -> bool:
    """Carry settings, My Apps and deploy configs over from the old app id (once)."""
    if new.exists() or not old.is_dir():
        return False
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(new))
    return True


def cache_dir() -> Path:
    path = user_cache_path(APP_ID, appauthor=False)
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_private(path: Path, text: str) -> None:
    """Atomically write a file that is owner-only (0600) from the moment it exists."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    tmp.replace(path)


def deployer_configs_dir() -> Path:
    path = config_dir() / "deployer"
    path.mkdir(parents=True, exist_ok=True)
    return path
