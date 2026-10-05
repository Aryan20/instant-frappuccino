"""Containers, resource usage and disk usage — the parts of Docker Desktop's UI we replace.

Only containers belonging to Frappe Manager are shown: the global ``services`` project and
every bench project whose compose files live under the FM home directory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

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


def parse_ps(lines: str, fm_home: str | Path) -> list[Container]:
    """``docker ps -a --format '{{json .}}'`` → FM containers (``fm_home`` already resolved)."""
    home = str(fm_home)
    sites = str(Path(fm_home) / "sites")
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


def port_conflicts(containers: list[Container]) -> list[Container]:
    """Running non-FM containers holding ports 80/443 — they block fm's nginx proxy."""
    return [c for c in containers if c.bench == "" and c.running and c.publishes_web_ports]


def apply_stats(containers: list[Container], lines: str) -> None:
    """Fill cpu/memory from ``docker stats --no-stream --format '{{json .}}'`` output."""
    by_id: dict[str, dict] = {}
    for line in lines.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        by_id[row.get("ID", "")[:12]] = row
    for container in containers:
        row = by_id.get(container.id[:12])
        if row and container.running:
            container.cpu = row.get("CPUPerc", "")
            container.memory = row.get("MemUsage", "").split(" / ")[0]


@dataclass
class DiskRow:
    kind: str
    size: str
    reclaimable: str
    count: str


def parse_df(lines: str) -> list[DiskRow]:
    """``docker system df --format '{{json .}}'`` output → rows."""
    rows = []
    for line in lines.splitlines():
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
