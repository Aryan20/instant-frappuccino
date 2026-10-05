"""Run commands on a server over SSH.

The same ``fm`` / ``fmd`` / ``docker`` argv the app builds for this machine is sent to the
server's login shell, so the tools resolve from the server's own PATH. SSH runs with
``BatchMode`` (keys or agent only; it never hangs on a password prompt) and connection
multiplexing, so the periodic status poll reuses one connection instead of reconnecting.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from importlib import resources

from fmapp.core import paths
from fmapp.core.env import tool_env, which
from fmapp.core.hosts import Host

# fm and rich print plain text with these; a terminal session keeps the server's own TERM.
_PLAIN_ENV = ("NO_COLOR=1", "TERM=dumb", "COLUMNS=160", "PYTHONUNBUFFERED=1")
_SOCKET_PATH_MAX = 100  # sun_path is 104 bytes on macOS, 108 on Linux; %C expands to 40 chars


class RemoteError(RuntimeError):
    """An expected failure talking to a server (unreachable, refused, no python3)."""


def _home_path(path: str) -> str:
    """Shell-quote a path but keep a leading ``~/`` expandable on the server."""
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def ssh_base(host: Host) -> list[str]:
    argv = [
        which("ssh") or "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
    ]
    sockets = paths.cache_dir() / "ssh"
    if len(str(sockets)) + 41 <= _SOCKET_PATH_MAX:  # else ssh refuses; connect each time instead
        sockets.mkdir(mode=0o700, exist_ok=True)
        argv += ["-o", "ControlMaster=auto", "-o", f"ControlPath={sockets}/%C", "-o", "ControlPersist=120"]
    if host.port:
        argv += ["-p", str(host.port)]
    return argv


def remote_script(host: Host, argv: list[str], plain: bool = True) -> str:
    exports = list(_PLAIN_ENV) if plain else []
    if host.fm_home.rstrip("/") not in ("", "~/frappe"):
        exports.append(f"FRAPPE_MANAGER_HOME={_home_path(host.fm_home)}")
    prefix = f"export {' '.join(exports)}; " if exports else ""
    return prefix + shlex.join(argv)


def wrap(host: Host, argv: list[str], tty: bool = False) -> list[str]:
    """``argv`` as it should be run for ``host``: unchanged locally, via SSH for a server."""
    if host.is_local:
        return argv
    script = remote_script(host, argv, plain=not tty)
    return [
        *ssh_base(host),
        *(["-tt"] if tty else []),
        host.destination,
        "--",
        "bash -lc " + shlex.quote(script),
    ]


def login_argv(host: Host) -> list[str]:
    """An interactive ``ssh`` for a terminal window (accepts a new host key, asks for a passphrase)."""
    return [which("ssh") or "ssh", *(["-p", str(host.port)] if host.port else []), host.destination]


def probe_source() -> str:
    return resources.files("fmapp.core").joinpath("probe.py").read_text()


def run_probe(host: Host, timeout: float = 60, **kwargs) -> dict:
    """Run ``probe.snapshot(**kwargs)`` on the server and return its result."""
    kwargs.setdefault("fm_home", host.fm_home)
    argv = wrap(host, ["python3", "-", json.dumps(kwargs)])
    try:
        proc = subprocess.run(
            argv,
            input=probe_source(),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=tool_env(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteError(f"{host.name} didn't answer within {timeout:.0f}s.") from exc
    except OSError as exc:
        raise RemoteError(f"Couldn't run ssh: {exc}") from exc
    lines = proc.stdout.strip().splitlines()
    if proc.returncode != 0 or not lines:
        raise RemoteError(_explain(host, proc.stderr or proc.stdout))
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise RemoteError(f"Unexpected reply from {host.name}: {lines[-1][:200]}") from exc


def _explain(host: Host, output: str) -> str:
    tail = output.strip().splitlines()[-1] if output.strip() else "no output"
    if "Host key verification failed" in output or "REMOTE HOST IDENTIFICATION" in output:
        return f"{host.destination}'s host key isn't trusted yet. Connect once in Terminal to accept it."
    if "Permission denied" in output:
        return (
            f"SSH refused the login to {host.destination} ({tail}). "
            "Add your key to the server or to ssh-agent; passwords aren't supported."
        )
    if "python3: command not found" in output or ("No such file" in output and "python3" in output):
        return f"python3 isn't installed on {host.name}; it's needed to read the server's sites."
    return f"Couldn't reach {host.name}: {tail}"
