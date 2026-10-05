"""User preferences persisted as JSON in the platform config dir."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from fmapp.core import paths

FRAPPE_BRANCHES = ("version-15", "version-16", "develop")


@dataclass
class Settings:
    github_token: str = ""
    fm_home: str = ""  # empty = fm default (~/frappe)
    fm_path: str = ""  # empty = look up on PATH
    fmd_path: str = ""
    docker_path: str = ""
    default_frappe_branch: str = "version-15"
    default_admin_password: str = "admin"
    theme: str = "system"  # system | light | dark
    refresh_seconds: int = 10
    # Engine & background behaviour
    engine_provider: str = "auto"  # auto | docker-desktop | orbstack | colima | systemd | rootless
    autostart: str = "off"  # off | launch | login — bring engine, services & sites up
    autostart_sites: list[str] = field(default_factory=list)
    tray: bool = True  # menu-bar / system-tray icon with quick actions
    close_to_tray: bool = True  # closing the window keeps the app running in the tray
    stop_engine_with_everything: bool = False  # "Stop everything" also stops the engine
    command_history: list[str] = field(default_factory=list)  # Run command… recents (newest first)
    hosts: list[dict] = field(default_factory=list)  # servers reached over SSH (see hosts.Host)
    active_host: str = "local"

    def remember_command(self, command: str, limit: int = 25) -> None:
        self.command_history = [command, *[c for c in self.command_history if c != command]][:limit]

    @staticmethod
    def file() -> Path:
        return paths.config_dir() / "settings.json"

    @classmethod
    def load(cls) -> Settings:
        try:
            raw = json.loads(cls.file().read_text())
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        # Holds a GitHub token: owner-readable only.
        paths.write_private(self.file(), json.dumps(asdict(self), indent=2))
        self.apply_env()

    def apply_env(self) -> None:
        """Expose settings that child processes (fm, fmd) read from the environment."""
        if self.fm_home:
            os.environ["FRAPPE_MANAGER_HOME"] = os.path.expanduser(self.fm_home)
        else:
            os.environ.pop("FRAPPE_MANAGER_HOME", None)

    def secret_env(self) -> dict[str, str]:
        return {"GITHUB_TOKEN": self.github_token} if self.github_token else {}
