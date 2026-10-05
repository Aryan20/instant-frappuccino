"""Process environment and external-tool discovery.

GUI apps launched from Finder / a desktop launcher do not inherit the user's shell PATH,
so ``fm`` (usually in ``~/.local/bin``) and ``docker`` (Docker Desktop, OrbStack, Colima,
distro packages) would not be found. We extend PATH with the well-known install
locations for both macOS and Linux and force plain, uncoloured CLI output.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

_EXTRA_PATHS = (
    "~/.local/bin",
    "~/.cargo/bin",
    "~/.docker/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/snap/bin",
    "/Applications/Docker.app/Contents/Resources/bin",
    "/Applications/OrbStack.app/Contents/MacOS/xbin",
)

# Rich/typer (used by fm and fmd) honour these and fall back to plain text.
_PLAIN_OUTPUT = {
    "NO_COLOR": "1",
    "TERM": "dumb",
    "COLUMNS": "160",
    "PYTHONUNBUFFERED": "1",
    "PYTHONIOENCODING": "utf-8",
}


def tool_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    for raw in _EXTRA_PATHS:
        path = os.path.expanduser(raw)
        if path not in parts and os.path.isdir(path):
            parts.append(path)
    env["PATH"] = os.pathsep.join(parts)
    env.pop("FORCE_COLOR", None)
    env.update(_PLAIN_OUTPUT)
    if extra:
        env.update({k: v for k, v in extra.items() if v is not None})
    return env


def which(name: str, override: str = "") -> str | None:
    if override:
        expanded = os.path.expanduser(override)
        return expanded if os.access(expanded, os.X_OK) else None
    return shutil.which(name, path=tool_env()["PATH"])


@dataclass(frozen=True)
class ToolStatus:
    name: str
    path: str | None
    version: str = ""
    ok: bool = False
    detail: str = ""


def _run(argv: list[str], timeout: float = 10) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=tool_env(), check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def probe_tool(name: str, version_args: list[str], override: str = "") -> ToolStatus:
    path = which(name, override)
    if not path:
        return ToolStatus(name, None, detail=f"{name} not found on PATH")
    code, out = _run([path, *version_args])
    first_line = out.splitlines()[0].strip() if out else ""
    return ToolStatus(name, path, version=first_line, ok=code == 0, detail="" if code == 0 else out)


def probe_docker(override: str = "") -> ToolStatus:
    path = which("docker", override)
    if not path:
        return ToolStatus("docker", None, detail="Docker CLI not found")
    code, out = _run([path, "info", "--format", "{{.ServerVersion}}"], timeout=15)
    if code != 0:
        return ToolStatus("docker", path, detail="Docker daemon is not running")
    return ToolStatus("docker", path, version=out.splitlines()[-1].strip(), ok=True)


def probe_all(fm_path: str = "", fmd_path: str = "", docker_path: str = "") -> dict[str, ToolStatus]:
    return {
        "docker": probe_docker(docker_path),
        "fm": probe_tool("fm", ["--version"], fm_path),
        "fmd": probe_tool("fmd", ["--version"], fmd_path),
        "uv": probe_tool("uv", ["--version"]),
        "git": probe_tool("git", ["--version"]),
    }
