"""Containers, resource usage and disk usage — the parts of Docker Desktop's UI we replace.

Only containers belonging to Frappe Manager are shown: the global ``services`` project and
every bench project whose compose files live under the FM home directory.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from fmapp.core import paths
from fmapp.core.env import tool_env, which

GLOBAL_PROJECT = "services"
GLOBAL_SERVICES = {
    "global-db": "MariaDB (global database)",
    "global-nginx-proxy": "Nginx proxy (routes *.localhost)",
}
WEB_PORTS = ("80", "443")


@dataclass
class Container:
    id: str
    name: str
    project: str
    service: str
    state: str  # running | exited | created | restarting | paused | dead
    status: str  # human, e.g. "Up 3 weeks"
    image: str
    ports: str
    bench: str | None = None  # None → global service, "" → not FM
    cpu: str = ""
    memory: str = ""

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def is_global(self) -> bool:
        return self.project == GLOBAL_PROJECT

    @property
    def publishes_web_ports(self) -> bool:
        return any(f":{p}->" in self.ports for p in WEB_PORTS)


def _labels(raw: str) -> dict[str, str]:
    out = {}
    for pair in raw.split(","):
        key, sep, value = pair.partition("=")
        if sep:
            out[key] = value
    return out


def parse_ps(lines: str, fm_home: Path) -> list[Container]:
    home = str(fm_home.resolve())
    sites = str((fm_home / "sites").resolve())
    containers = []
    for line in lines.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        labels = _labels(row.get("Labels", ""))
        project = labels.get("com.docker.compose.project", "")
        workdir = labels.get("com.docker.compose.project.working_dir", "")
        if project == GLOBAL_PROJECT and workdir.startswith(home):
            bench = None
        elif workdir.startswith(sites + "/"):
            bench = Path(workdir).name
        else:
            bench = ""
        containers.append(
            Container(
                id=row.get("ID", ""),
                name=row.get("Names", ""),
                project=project,
                service=labels.get("com.docker.compose.service", ""),
                state=row.get("State", ""),
                status=row.get("Status", ""),
                image=row.get("Image", ""),
                ports=row.get("Ports", ""),
                bench=bench,
            )
        )
    return containers


def list_containers(docker_path: str = "", include_foreign: bool = False) -> list[Container]:
    docker = which("docker", docker_path)
    if not docker:
        return []
    out = _run([docker, "ps", "-a", "--no-trunc", "--format", "{{json .}}"])
    containers = parse_ps(out, paths.fm_home())
    return containers if include_foreign else [c for c in containers if c.bench != ""]


def port_conflicts(containers: list[Container]) -> list[Container]:
    """Running non-FM containers holding ports 80/443 — they block fm's nginx proxy."""
    return [c for c in containers if c.bench == "" and c.running and c.publishes_web_ports]


def attach_stats(containers: list[Container], docker_path: str = "") -> None:
    """Fill cpu/memory for running containers (``docker stats`` takes ~2s)."""
    docker = which("docker", docker_path)
    running = [c for c in containers if c.running]
    if not docker or not running:
        return
    out = _run(
        [docker, "stats", "--no-stream", "--format", "{{json .}}", *[c.id for c in running]], timeout=30
    )
    by_id: dict[str, dict] = {}
    for line in out.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        by_id[row.get("ID", "")[:12]] = row
    for container in running:
        row = by_id.get(container.id[:12])
        if row:
            container.cpu = row.get("CPUPerc", "")
            container.memory = row.get("MemUsage", "").split(" / ")[0]


@dataclass
class DiskRow:
    kind: str
    size: str
    reclaimable: str
    count: str


def disk_usage(docker_path: str = "") -> list[DiskRow]:
    docker = which("docker", docker_path)
    if not docker:
        return []
    rows = []
    for line in _run([docker, "system", "df", "--format", "{{json .}}"], timeout=30).splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append(
            DiskRow(
                row.get("Type", ""),
                row.get("Size", ""),
                row.get("Reclaimable", ""),
                row.get("TotalCount", ""),
            )
        )
    return rows


def _run(argv: list[str], timeout: float = 15) -> str:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=tool_env(), check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""
