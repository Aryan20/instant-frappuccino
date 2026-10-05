"""Open a command in the user's own terminal app (their default on macOS, the desktop's on Linux)."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

from fmapp.core import paths
from fmapp.core.env import which

# (binary, args placed before the command) — first one installed wins.
_LINUX_TERMINALS = (
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]),
    ("tilix", ["-e"]),
    ("alacritty", ["-e"]),
    ("kitty", []),
    ("wezterm", ["start", "--"]),
    ("xterm", ["-e"]),
)


def write_command_script(command: list[str], name: str, folder: Path | None = None) -> Path:
    """A ``.command`` file macOS opens in the default terminal app.

    Opening it with ``open`` needs no Automation permission, unlike scripting Terminal via
    AppleScript — which macOS silently refuses for an app that was never granted it.
    """
    folder = folder or paths.cache_dir() / "terminal"
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.command"
    script.write_text(
        "#!/bin/bash\n"
        "clear\n"
        f"echo {shlex.quote('$ ' + shlex.join(command))}\n"
        f"{shlex.join(command)}\n"
        "status=$?\n"
        "echo\n"
        '[ $status -ne 0 ] && echo "Exited with status $status."\n'
        "read -rp 'Press Enter to close this window…' _\n"
    )
    script.chmod(0o700)
    return script


def terminal_argv(command: list[str], name: str = "shell", platform: str = sys.platform) -> list[str] | None:
    """argv that opens a new terminal window running ``command``; None if no terminal found."""
    if platform == "darwin":
        return ["/usr/bin/open", str(write_command_script(command, name))]
    line = shlex.join(command)
    for binary, prefix in _LINUX_TERMINALS:
        path = which(binary)
        if path:
            # Keep the window open after the command exits so errors stay readable.
            held = ["bash", "-lc", f"{line}; echo; read -rp 'Press Enter to close…' _"]
            return [path, *prefix, *held]
    return None
