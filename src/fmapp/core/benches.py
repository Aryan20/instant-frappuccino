"""Bench models from probe snapshots (``probe.py`` does the reading, here or on a server)."""

from __future__ import annotations

from pathlib import Path

from fmapp.core import paths, probe
from fmapp.core.models import Backup, Bench, BenchKind, BenchStatus, InstalledApp, Release


def bench_from_raw(raw: dict) -> Bench:
    return Bench(
        name=raw["name"],
        path=Path(raw["path"]),
        config=raw["config"],
        apps=[InstalledApp(**app) for app in raw["apps"]],
        releases=[Release(**release) for release in raw["releases"]],
        backups=[Backup(**backup) for backup in raw["backups"]],
        status=BenchStatus(raw.get("status", "unknown")),
        kind=BenchKind(raw["kind"]),
        error=raw["error"],
        maintenance_mode=raw["maintenance_mode"],
        scheduler_paused=raw["scheduler_paused"],
    )


def discover(benches_dir: Path | None = None, compose: list | None = None) -> list[Bench]:
    """Benches under ``benches_dir`` (default: this machine's FM home).

    ``compose`` is ``docker compose ls --format json`` output; ``None`` means Docker is
    unreachable, so every healthy bench's status is unknown.
    """
    raws = probe.discover(str(benches_dir or paths.benches_dir()))
    probe.apply_statuses(raws, compose)
    return [bench_from_raw(raw) for raw in raws]
