"""One place to read a host's state — this computer in-process, a server through SSH."""

from __future__ import annotations

from dataclasses import dataclass, field

from fmapp.core import benches, containers, engine, paths, probe, remote
from fmapp.core.env import which
from fmapp.core.hosts import Host
from fmapp.core.models import Bench
from fmapp.core.settings import Settings


@dataclass
class SystemState:
    engine: engine.EngineInfo
    containers: list[containers.Container] = field(default_factory=list)
    disk: list[containers.DiskRow] | None = None  # None = not measured this time
    tools: dict[str, str | None] = field(default_factory=dict)  # fm / fmd / docker on the host


class Source:
    def __init__(self, settings: Settings, host: Host) -> None:
        self.settings, self.host = settings, host

    def _snapshot(self, **kwargs) -> dict:
        if self.host.is_local:
            docker = which("docker", self.settings.docker_path) or "docker"
            return probe.snapshot(fm_home=str(paths.fm_home()), docker=docker, **kwargs)
        return remote.run_probe(self.host, **kwargs)

    def benches(self) -> list[Bench]:
        raw = self._snapshot(benches=True, system=False)
        return [benches.bench_from_raw(b) for b in raw["benches"]]

    def system(self, stats: bool = False, disk: bool = False) -> SystemState:
        raw = self._snapshot(benches=False, system=True, stats=stats, disk=disk)
        state = raw["docker"]
        provider, context = engine.Provider.UNKNOWN, ""
        if self.host.is_local:  # only this machine's engine can be started/stopped from here
            provider = engine.provider(self.settings.docker_path, self.settings.engine_provider)
            docker = which("docker", self.settings.docker_path)
            context = engine.current_context(docker)[0] if docker else ""
        found = containers.parse_ps(state.get("ps", ""), raw["fm_home"])
        containers.apply_stats(found, state.get("stats", ""))
        return SystemState(
            engine=engine.EngineInfo(provider, state["running"], state.get("version", ""), context),
            containers=found,
            disk=containers.parse_df(state["df"]) if disk and state.get("df") else None,
            tools=raw.get("tools", {}),
        )
