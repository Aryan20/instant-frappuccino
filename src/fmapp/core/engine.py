"""Control the Docker engine headlessly, whichever product provides it.

The user never has to open Docker Desktop (or OrbStack/Colima, or touch systemctl): we
detect the provider behind the active docker context and drive it through its own CLI.

=================  ==============================================  =====================
provider           start / stop / restart                          platform
=================  ==============================================  =====================
Docker Desktop     ``docker desktop start|stop|restart``           macOS, Linux
OrbStack           ``orb start|stop``                              macOS
Colima             ``colima start|stop|restart``                   macOS, Linux
systemd (rootful)  ``pkexec systemctl … docker`` (GUI auth prompt)  Linux
rootless docker    ``systemctl --user … docker``                   Linux
=================  ==============================================  =====================
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from fmapp.core.env import tool_env, which


class Provider(StrEnum):
    DOCKER_DESKTOP = "docker-desktop"
    ORBSTACK = "orbstack"
    COLIMA = "colima"
    SYSTEMD = "systemd"
    ROOTLESS = "rootless"
    UNKNOWN = "unknown"


LABELS = {
    Provider.DOCKER_DESKTOP: "Docker Desktop",
    Provider.ORBSTACK: "OrbStack",
    Provider.COLIMA: "Colima",
    Provider.SYSTEMD: "Docker Engine (systemd)",
    Provider.ROOTLESS: "Docker Engine (rootless)",
    Provider.UNKNOWN: "Docker",
}


@dataclass(frozen=True)
class EngineInfo:
    provider: Provider
    running: bool
    version: str = ""
    context: str = ""
    detail: str = ""

    @property
    def label(self) -> str:
        return LABELS[self.provider]

    @property
    def controllable(self) -> bool:
        return self.provider is not Provider.UNKNOWN


def _out(argv: list[str], timeout: float = 8) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=tool_env(), check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout.strip()


def current_context(docker: str) -> tuple[str, str]:
    """(context name, endpoint) of the active docker context."""
    code, out = _out([docker, "context", "inspect", "--format", "{{.Name}} {{.Endpoints.docker.Host}}"])
    if code != 0 or not out:
        return "", os.environ.get("DOCKER_HOST", "")
    name, _, host = out.partition(" ")
    return name, host


def classify(
    context: str, endpoint: str, platform: str = sys.platform, have: set[str] | None = None
) -> Provider:
    """Pure provider detection from the docker context (testable without Docker)."""
    have = have if have is not None else _installed()
    ctx, host = context.lower(), endpoint.lower()
    if "orbstack" in ctx or ".orbstack" in host:
        return Provider.ORBSTACK
    if "colima" in ctx or ".colima" in host:
        return Provider.COLIMA
    if ctx == "desktop-linux" or "/.docker/run/docker.sock" in host or "docker-desktop" in ctx:
        return Provider.DOCKER_DESKTOP
    if ctx == "rootless" or ("/run/user/" in host and host.endswith("docker.sock")):
        return Provider.ROOTLESS
    if platform.startswith("linux"):
        return Provider.SYSTEMD if "systemctl" in have else Provider.UNKNOWN
    # macOS "default" context pointing at /var/run/docker.sock: whoever installed the symlink.
    for provider, marker in (
        (Provider.ORBSTACK, "orb"),
        (Provider.DOCKER_DESKTOP, "docker-desktop"),
        (Provider.COLIMA, "colima"),
    ):
        if marker in have:
            return provider
    return Provider.UNKNOWN


def _installed() -> set[str]:
    found = {name for name in ("orb", "colima", "systemctl", "pkexec") if which(name)}
    if Path("/Applications/Docker.app").exists() or Path.home().joinpath(".docker/desktop").exists():
        found.add("docker-desktop")
    if Path("/Applications/OrbStack.app").exists():
        found.add("orb")
    return found


def _pick(override: str, context: str, endpoint: str) -> Provider:
    if override and override != "auto":
        try:
            return Provider(override)
        except ValueError:
            pass
    return classify(context, endpoint)


def provider(docker_path: str = "", override: str = "auto") -> Provider:
    """Who provides Docker, from settings / the docker context only (no daemon round trip)."""
    if override and override != "auto":
        return _pick(override, "", "")
    docker = which("docker", docker_path)
    return _pick(override, *current_context(docker)) if docker else Provider.UNKNOWN


def detect(docker_path: str = "", override: str = "auto") -> EngineInfo:
    docker = which("docker", docker_path)
    if not docker:
        return EngineInfo(Provider.UNKNOWN, False, detail="Docker CLI not found")
    context, endpoint = current_context(docker)
    provider = _pick(override, context, endpoint)
    code, version = _out([docker, "info", "--format", "{{.ServerVersion}}"], timeout=12)
    running = code == 0 and bool(version)
    return EngineInfo(
        provider, running, version if running else "", context, "" if running else "Engine is not running"
    )


def control_argv(provider: Provider, action: str, docker: str) -> list[list[str]]:
    """Commands that perform ``action`` (start | stop | restart) for a provider."""
    if action not in ("start", "stop", "restart"):
        raise ValueError(f"Unknown engine action {action!r}")
    if provider is Provider.DOCKER_DESKTOP:
        if sys.platform.startswith("linux") and not _has_desktop_cli(docker):
            return [["systemctl", "--user", action, "docker-desktop"]]
        if not _has_desktop_cli(docker):
            return _desktop_legacy(action)
        return [[docker, "desktop", action, "--timeout", "300"]]
    if provider is Provider.ORBSTACK:
        orb = which("orb") or "orb"
        if action == "restart":
            return [[orb, "stop"], [orb, "start"]]
        return [[orb, action]]
    if provider is Provider.COLIMA:
        return [[which("colima") or "colima", action]]
    if provider is Provider.ROOTLESS:
        return [["systemctl", "--user", action, "docker"]]
    if provider is Provider.SYSTEMD:
        # pkexec shows the desktop's graphical password prompt (polkit).
        return [["pkexec", "systemctl", action, "docker"]]
    raise ValueError("Couldn't tell which product runs Docker here; pick one in Settings → Engine.")


def _has_desktop_cli(docker: str) -> bool:
    """Docker Desktop ≥ 4.37 ships a `docker desktop` CLI plugin; look for the file, don't run it."""
    plugin = "docker-desktop"
    candidates = [
        Path.home() / ".docker" / "cli-plugins" / plugin,
        Path("/Applications/Docker.app/Contents/Resources/cli-plugins") / plugin,
        Path("/usr/lib/docker/cli-plugins") / plugin,
        Path("/usr/local/lib/docker/cli-plugins") / plugin,
        Path(docker).resolve().parent.parent / "cli-plugins" / plugin,
    ]
    return any(path.exists() for path in candidates)


def _desktop_legacy(action: str) -> list[list[str]]:
    # Docker Desktop < 4.37 (no `docker desktop` CLI): launch hidden / quit via AppleScript.
    start = ["open", "-g", "-j", "-a", "Docker"]
    stop = ["osascript", "-e", 'quit app "Docker"']
    return {"start": [start], "stop": [stop], "restart": [stop, start]}[action]


def wait_ready_argv(docker: str, timeout: int = 180) -> list[str]:
    """POSIX-sh loop that exits 0 once the daemon answers (works on macOS and Linux)."""
    script = (
        f"i=0; while [ $i -lt {timeout} ]; do "
        f'"$0" info >/dev/null 2>&1 && echo "Docker engine is ready." && exit 0; '
        f'i=$((i+2)); sleep 2; done; echo "Timed out waiting for Docker."; exit 1'
    )
    return ["/bin/sh", "-c", script, docker]
