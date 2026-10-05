"""Register Instant Frappuccino as a login item: a LaunchAgent on macOS, XDG autostart on Linux.

The login item launches the app with ``--background`` (tray only, no window), which then
brings the engine, global services and the chosen sites up.
"""

from __future__ import annotations

import plistlib
import shlex
import sys
from pathlib import Path

from fmapp import APP_ID, APP_NAME

LABEL = "com.rtcamp.instant-frappuccino"
LEGACY_LABEL = "com.rtcamp.fm-app"
LEGACY_APP_ID = "fm-app"
BACKGROUND_FLAG = "--background"


def launch_command() -> list[str]:
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        return [sys.executable, BACKGROUND_FLAG]
    return [sys.executable, "-m", "fmapp", BACKGROUND_FLAG]


def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _linux_desktop() -> Path:
    return Path.home() / ".config" / "autostart" / f"{APP_ID}.desktop"


def item_path() -> Path:
    return _mac_plist() if sys.platform == "darwin" else _linux_desktop()


def is_enabled() -> bool:
    return item_path().exists()


def enable(command: list[str] | None = None) -> Path:
    command = command or launch_command()
    target = item_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        target.write_bytes(
            plistlib.dumps(
                {
                    "Label": LABEL,
                    "ProgramArguments": command,
                    "RunAtLoad": True,
                    "ProcessType": "Interactive",
                    "LimitLoadToSessionType": "Aqua",
                }
            )
        )
    else:
        target.write_text(
            "[Desktop Entry]\nType=Application\n"
            f"Name={APP_NAME}\nExec={shlex.join(command)}\nIcon={APP_ID}\n"
            "X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=5\nNoDisplay=true\n"
        )
    return target


def disable() -> None:
    item_path().unlink(missing_ok=True)


def _legacy_item() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_LABEL}.plist"
    return Path.home() / ".config" / "autostart" / f"{LEGACY_APP_ID}.desktop"


def sync(mode: str) -> None:
    """Make the login item match the ``autostart`` setting (off | launch | login)."""
    _legacy_item().unlink(missing_ok=True)  # pre-rename login item would launch a stale command
    if mode == "login":
        enable()
    elif is_enabled():
        disable()
