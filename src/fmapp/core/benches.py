"""Read bench state straight from disk and Docker.

``fm list`` / ``fm info`` only print Rich tables, so instead of scraping them we read the
same sources fm does: ``~/frappe/sites/<bench>/bench_config.toml``, the bench's ``apps/``
directory, and ``docker compose ls``. fmd-managed benches are recognised by their release
layout (``workspace/release_*`` + ``frappe-bench`` symlink).
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from pathlib import Path

from fmapp.core import paths
from fmapp.core.env import tool_env, which
from fmapp.core.models import Backup, Bench, BenchKind, BenchStatus, InstalledApp, Release

_RELEASE_DIR = re.compile(r"^release_\d{8}_\d{6}$")
_VERSION = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)


def compose_statuses(docker_path: str = "") -> dict[Path, BenchStatus] | None:
    """Map each compose file to its project's status; ``None`` if Docker is unreachable."""
    docker = which("docker", docker_path)
    if not docker:
        return None
    try:
        proc = subprocess.run(
            [docker, "compose", "ls", "--all", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            env=tool_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        projects = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return None

    statuses: dict[Path, BenchStatus] = {}
    for project in projects:
        state = _parse_compose_state(str(project.get("Status", "")))
        for config_file in str(project.get("ConfigFiles", "")).split(","):
            if config_file.strip():
                statuses[Path(config_file.strip()).resolve()] = state
    return statuses


def _parse_compose_state(status: str) -> BenchStatus:
    # e.g. "running(10)", "exited(3)", "running(2), exited(1)"
    states = re.findall(r"([a-z]+)\(\d+\)", status)
    if not states:
        return BenchStatus.STOPPED
    if all(s == "running" for s in states):
        return BenchStatus.RUNNING
    if "running" in states:
        return BenchStatus.PARTIAL
    return BenchStatus.STOPPED


def discover(benches_dir: Path | None = None, statuses: dict[Path, BenchStatus] | None = None) -> list[Bench]:
    root = benches_dir or paths.benches_dir()
    if not root.is_dir():
        return []
    benches = [
        load_bench(child, statuses)
        for child in sorted(root.iterdir())
        if child.is_dir() and (child / "docker-compose.yml").exists()
    ]
    return benches


def load_bench(path: Path, statuses: dict[Path, BenchStatus] | None = None) -> Bench:
    bench = Bench(name=path.name, path=path)
    config_file = path / "bench_config.toml"
    try:
        bench.config = tomllib.loads(config_file.read_text())
        bench.name = str(bench.config.get("name") or path.name)
    except FileNotFoundError:
        bench.error = "bench_config.toml is missing — run `fm migrate` or recreate this bench."
        bench.status = BenchStatus.BROKEN
    except (tomllib.TOMLDecodeError, OSError) as exc:
        bench.error = f"Unreadable bench_config.toml: {exc}"
        bench.status = BenchStatus.BROKEN

    bench.releases = read_releases(bench.workspace)
    if bench.releases or (bench.workspace / "deployment-data").is_dir():
        bench.kind = BenchKind.DEPLOYER
    bench.apps = read_apps(bench.bench_root)
    bench.maintenance_mode, bench.scheduler_paused = read_site_flags(bench.site_dir)

    if bench.status is not BenchStatus.BROKEN:
        if statuses is None:
            bench.status = BenchStatus.UNKNOWN
        else:
            compose = (path / "docker-compose.yml").resolve()
            bench.status = statuses.get(compose, BenchStatus.STOPPED)
    return bench


def read_site_flags(site_dir: Path) -> tuple[bool, bool]:
    """(maintenance_mode, scheduler paused). Only these keys are kept; the file holds secrets."""
    try:
        config = json.loads((site_dir / "site_config.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False, False
    return bool(config.get("maintenance_mode")), bool(config.get("pause_scheduler"))


_BACKUP = re.compile(r"^(\d{8}_\d{6})-.+?-(database\.sql(?:\.gz)?|files\.tar|private-files\.tar)$")


def list_backups(backups_dir: Path) -> list[Backup]:
    """Group a site's backup folder into runs, newest first."""
    if not backups_dir.is_dir():
        return []
    runs: dict[str, dict[str, Path]] = {}
    for file in backups_dir.iterdir():
        match = _BACKUP.match(file.name)
        if match:
            runs.setdefault(match.group(1), {})[match.group(2)] = file
    backups = []
    for stamp, files in runs.items():
        database = files.get("database.sql.gz") or files.get("database.sql")
        if database:
            backups.append(Backup(stamp, database, files.get("files.tar"), files.get("private-files.tar")))
    return sorted(backups, key=lambda b: b.stamp, reverse=True)


def read_releases(workspace: Path) -> list[Release]:
    if not workspace.is_dir():
        return []
    link = workspace / "frappe-bench"
    active = link.resolve().name if link.is_symlink() else None
    releases = [
        Release(name=d.name, active=d.name == active)
        for d in workspace.iterdir()
        if d.is_dir() and _RELEASE_DIR.match(d.name)
    ]
    return sorted(releases, key=lambda r: r.name, reverse=True)


def read_apps(bench_root: Path) -> list[InstalledApp]:
    apps_dir = bench_root / "apps"
    if not apps_dir.is_dir():
        return []
    # apps.txt is bench's own install order; fall back to directory listing.
    order: list[str] = []
    apps_txt = bench_root / "sites" / "apps.txt"
    if apps_txt.exists():
        order = [line.strip() for line in apps_txt.read_text().splitlines() if line.strip()]
    on_disk = sorted(d.name for d in apps_dir.iterdir() if d.is_dir() and not d.name.startswith("."))
    names = [n for n in order if n in on_disk] + [n for n in on_disk if n not in order]
    # frappe is the framework every other app builds on; it always comes first.
    names.sort(key=lambda n: n != "frappe")
    return [read_app(apps_dir / name) for name in names]


def read_app(app_dir: Path) -> InstalledApp:
    app = InstalledApp(name=app_dir.name)
    init = app_dir / app_dir.name / "__init__.py"
    try:
        match = _VERSION.search(init.read_text(errors="ignore"))
        if match:
            app.version = match.group(1)
    except OSError:
        pass
    app.branch, app.commit, app.remote = git_info(app_dir)
    return app


def git_info(repo_dir: Path) -> tuple[str, str, str]:
    """(branch, commit sha, remote url) read from ``.git`` without spawning git."""
    git_dir = repo_dir / ".git"
    if git_dir.is_file():  # worktree / submodule: "gitdir: <path>"
        target = git_dir.read_text().partition("gitdir:")[2].strip()
        git_dir = (repo_dir / target).resolve()
    if not git_dir.is_dir():
        return "", "", ""
    branch = commit = remote = ""
    try:
        head = (git_dir / "HEAD").read_text().strip()
    except OSError:
        return "", "", ""
    if head.startswith("ref:"):
        ref = head[4:].strip()
        branch = ref.removeprefix("refs/heads/")
        commit = _resolve_ref(git_dir, ref)
    else:
        commit = head
    try:
        config = (git_dir / "config").read_text()
    except OSError:
        config = ""
    for remote_name in ("upstream", "origin"):
        match = re.search(rf'\[remote "{remote_name}"\][^\[]*?url\s*=\s*(\S+)', config, re.S)
        if match:
            remote = match.group(1)
            break
    return branch, commit, remote


def _resolve_ref(git_dir: Path, ref: str) -> str:
    loose = git_dir / ref
    if loose.exists():
        return loose.read_text().strip()
    packed = git_dir / "packed-refs"
    if packed.exists():
        for line in packed.read_text().splitlines():
            sha, _, name = line.partition(" ")
            if name.strip() == ref:
                return sha
    return ""
