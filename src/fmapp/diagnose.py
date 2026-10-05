"""``--diagnose``: run the app's external-tool paths inside the real (frozen) environment.

Prints what a GUI click would do — tool discovery, the login-URL round trip, the terminal
launcher — so problems that only appear in the packaged .app can be seen. Session ids are
redacted; nothing is opened or changed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

from fmapp.core.env import tool_env, which
from fmapp.core.hosts import LOCAL
from fmapp.core.operations import Operations
from fmapp.core.settings import Settings
from fmapp.core.source import Source
from fmapp.core.terminal import terminal_argv


def run() -> int:
    settings = Settings.load()
    settings.apply_env()
    ops = Operations(settings)
    env = tool_env(settings.secret_env())
    print(f"frozen={getattr(sys, 'frozen', False)} python={sys.version.split()[0]}")
    print("PATH=" + env["PATH"])
    for key in sorted(k for k in os.environ if k.startswith(("_PYI", "PYTHON", "DYLD", "QT_"))):
        print(f"env {key}={os.environ[key]}")
    for tool in ("fm", "docker", "osascript", "open"):
        print(f"which {tool}: {which(tool)}")
    running = [b.name for b in Source(settings, LOCAL).benches() if b.status.value == "running"]
    print("running sites:", running)
    if running:
        argv = ops.login_url_argv(running[0])
        started = time.time()
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=120, env=env, stdin=subprocess.DEVNULL, check=False
        )
        out = re.sub(r"sid=\w+", "sid=<redacted>", proc.stdout + proc.stderr)
        print(
            f"login: exit={proc.returncode} in {time.time() - started:.1f}s "
            f"url={ops.parse_login_url(proc.stdout + proc.stderr) is not None}"
        )
        print("login output tail:", out.strip().splitlines()[-4:])
        print("terminal argv:", terminal_argv(ops.shell_command(running[0])))
    return 0


def run_actions(site: str, which_: str = "both") -> int:
    """Click-equivalent of Log in as Admin and Terminal, inside the real app runtime."""
    from PySide6.QtCore import QEventLoop
    from PySide6.QtWidgets import QApplication, QMessageBox

    from fmapp.ui import site_tools
    from fmapp.ui.context import AppContext

    app = QApplication([])
    ctx = AppContext()
    events: list[str] = []
    ctx.notify.connect(lambda text, _ms: text and events.append(f"status: {text}"))
    for name in ("warning", "information"):
        setattr(QMessageBox, name, staticmethod(lambda *a, n=name: events.append(f"{n}: {a[1]} | {a[2]}")))

    if which_ in ("both", "terminal"):
        site_tools.open_terminal(ctx, None, site)
    if which_ in ("both", "login"):
        site_tools.login_as_admin(ctx, None, site)

    def finished() -> bool:
        if which_ == "terminal":
            return bool(events)
        return any(e.startswith(("status: Opened " + site, "warning:", "information:")) for e in events)

    deadline = time.time() + 120
    while time.time() < deadline and not finished():
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 100)
        time.sleep(0.05)
    for event in events:
        print(re.sub(r"sid=\w+", "sid=<redacted>", event))
    return 0
