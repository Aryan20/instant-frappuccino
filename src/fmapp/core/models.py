"""Domain objects shared by the core and the UI."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from fmapp.core.textutil import slug_to_app_name

_GITHUB = re.compile(
    r"^(?:https?://|ssh://git@|git@)github\.com[/:](?P<org>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"
)
# host + path of any git URL: https://host/path, git@host:path, ssh://git@host/path
_GIT_URL = re.compile(
    r"^(?:https?://(?:[^@/]+@)?|ssh://git@|git@)(?P<host>[^/:]+)[/:](?P<path>.+?)(?:\.git)?/?$"
)


@dataclass(frozen=True)
class AppRef:
    """A Frappe app source: repo + optional git ref + optional monorepo subdir.

    ``repo`` may be a bare app name (``erpnext`` → frappe org), ``org/repo`` or a full git
    URL. This mirrors what both ``fm create --apps`` and fmd ``[[apps]]`` accept.
    """

    repo: str
    ref: str = ""
    subdir: str = ""

    @classmethod
    def parse(cls, text: str) -> AppRef:
        text = text.strip()
        if not text:
            raise ValueError("App reference is empty")
        subdir = ""
        if "#" in text:
            text, subdir = text.split("#", 1)
        ref = ""
        head, sep, tail = text.rpartition(":")
        # A trailing ":ref" never contains "/", which keeps "https://host/x" and
        # "git@github.com:org/repo" intact.
        if sep and tail and "/" not in tail and head not in ("http", "https", "ssh"):
            text, ref = head, tail
        if not re.fullmatch(r"[\w.@:/+~-]+", text):
            raise ValueError(f"Invalid app repository: {text!r}")
        return cls(repo=text, ref=ref, subdir=subdir.strip("/"))

    @property
    def is_url(self) -> bool:
        return "://" in self.repo or self.repo.startswith("git@")

    @property
    def is_ssh(self) -> bool:
        return self.repo.startswith(("git@", "ssh://"))

    @property
    def org_repo(self) -> str | None:
        """``org/repo`` for GitHub sources, else ``None``."""
        if "/" not in self.repo:
            return f"frappe/{self.repo}"
        if not self.is_url:
            return self.repo
        match = _GITHUB.match(self.repo)
        return f"{match['org']}/{match['repo']}" if match else None

    @property
    def clone_url(self) -> str:
        if self.is_url:
            return self.repo
        return f"https://github.com/{self.org_repo}"

    @property
    def ssh_url(self) -> str:
        """The same repo over SSH (``git@host:path.git``), cloned with the user's SSH keys."""
        if self.repo.startswith("git@"):
            return self.repo
        if not self.is_url:
            return f"git@github.com:{self.org_repo}.git"
        match = _GIT_URL.match(self.repo)
        return f"git@{match['host']}:{match['path']}.git" if match else self.repo

    @property
    def name_guess(self) -> str:
        if self.subdir:
            return slug_to_app_name(self.subdir)
        return slug_to_app_name(self.org_repo or self.repo)

    def to_fm_arg(self) -> str:
        """``fm create --apps`` syntax: ``repo[:ref][#subdir]``."""
        arg = self.repo if self.is_url else (self.org_repo or self.repo)
        if self.repo.startswith("ssh://"):
            arg = self.ssh_url  # fm only understands the git@host:path form
        if self.ref:
            arg += f":{self.ref}"
        if self.subdir:
            arg += f"#{self.subdir}"
        elif self.ref and arg.startswith("git@"):
            arg += "#"  # fm's option check rejects git@host:path:ref unless a #subdir follows
        return arg

    def to_fmd_table(self) -> dict[str, Any]:
        """One ``[[apps]]`` entry of an fmd config."""
        table: dict[str, Any] = {"repo": self.org_repo or self.repo}
        if not self.org_repo:  # fmd reads ``repo`` as a GitHub org/repo; others need the URL
            match = _GIT_URL.match(self.repo)
            table = {"repo": match["path"] if match else self.repo, "repo_url": self.repo, "exists": True}
        if self.ref:
            table["ref"] = self.ref
        if self.subdir:
            table["subdir_path"] = self.subdir
            table["symlink"] = True
        return table

    @classmethod
    def from_fmd_table(cls, table: dict[str, Any]) -> AppRef:
        url = str(table.get("repo_url") or "")
        return cls(
            repo=url if _GIT_URL.match(url) else str(table.get("repo", "")),
            ref=str(table.get("ref", "")),
            subdir=str(table.get("subdir_path", "")),
        )

    def display(self) -> str:
        base = self.org_repo or self.repo
        return f"{base} @ {self.ref}" if self.ref else base


class BenchStatus(StrEnum):
    RUNNING = "running"
    PARTIAL = "partial"
    STOPPED = "stopped"
    UNKNOWN = "unknown"
    BROKEN = "broken"


class BenchKind(StrEnum):
    FM = "fm"
    DEPLOYER = "deployer"


@dataclass
class InstalledApp:
    name: str
    version: str = ""
    branch: str = ""
    commit: str = ""  # full sha
    remote: str = ""

    @property
    def short_commit(self) -> str:
        return self.commit[:8]


@dataclass
class Release:
    name: str
    active: bool = False

    @property
    def created(self) -> datetime | None:
        try:
            return datetime.strptime(self.name.removeprefix("release_"), "%Y%m%d_%H%M%S")
        except ValueError:
            return None


@dataclass
class Backup:
    """One ``bench backup`` run: a database dump plus optional file archives."""

    stamp: str  # e.g. 20260605_134807
    database: str  # file names inside the site's private/backups folder
    public_files: str | None = None
    private_files: str | None = None
    size: int = 0  # bytes, all files of the run

    @property
    def created(self) -> datetime | None:
        try:
            return datetime.strptime(self.stamp, "%Y%m%d_%H%M%S")
        except ValueError:
            return None


@dataclass
class Bench:
    name: str
    path: Path
    config: dict[str, Any] = field(default_factory=dict)
    apps: list[InstalledApp] = field(default_factory=list)
    releases: list[Release] = field(default_factory=list)
    status: BenchStatus = BenchStatus.UNKNOWN
    kind: BenchKind = BenchKind.FM
    error: str = ""
    backups: list[Backup] = field(default_factory=list)
    # Non-secret switches from sites/<site>/site_config.json
    maintenance_mode: bool = False
    scheduler_paused: bool = False

    @property
    def environment(self) -> str:
        return str(self.config.get("environment_type", "dev"))

    @property
    def developer_mode(self) -> bool:
        return bool(self.config.get("developer_mode", False))

    @property
    def admin_tools(self) -> bool:
        return bool(self.config.get("admin_tools", False))

    @property
    def alias_domains(self) -> list[str]:
        return list(self.config.get("alias_domains", []) or [])

    @property
    def url(self) -> str:
        scheme = "https" if self.config.get("ssl") else "http"
        return f"{scheme}://{self.name}"

    @property
    def workspace(self) -> Path:
        return self.path / "workspace"

    @property
    def bench_root(self) -> Path:
        return self.workspace / "frappe-bench"

    @property
    def site_dir(self) -> Path:
        return self.bench_root / "sites" / self.name

    @property
    def backups_dir(self) -> Path:
        return self.site_dir / "private" / "backups"

    @property
    def frappe_version(self) -> str:
        return next((a.version for a in self.apps if a.name == "frappe"), "")


@dataclass
class SiteSpec:
    """Everything the New Site wizard collects."""

    name: str
    kind: BenchKind = BenchKind.FM
    frappe_ref: str = "version-15"
    apps: list[AppRef] = field(default_factory=list)
    environment: str = "dev"
    developer_mode: bool = True
    admin_password: str = "admin"
    python_version: str = ""
    node_version: str = ""
    alias_domains: list[str] = field(default_factory=list)
    # fmd-only switch options
    maintenance_mode: bool = True
    backups: bool = True
    rollback: bool = True
    # fmd-only: an imported site.toml to build on (keys the wizard doesn't edit are kept)
    base_config: dict[str, Any] | None = None
