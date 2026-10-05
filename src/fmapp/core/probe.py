"""Read Frappe Manager state from disk and Docker, as plain JSON-able dicts.

Stdlib-only and Python 3.8+ on purpose: for this machine it runs in-process, and for a
server it is piped to ``python3 -`` over SSH (see ``remote.py``), so local and remote
benches are read by exactly the same code. ``fm list`` / ``fm info`` only print Rich
tables, so instead of scraping them this reads the same sources fm does.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

try:  # Python 3.11+; servers with an older python3 fall back to a minimal parser
    import tomllib
except ImportError:  # pragma: no cover - exercised on old remote Pythons
    tomllib = None

RELEASE_DIR = re.compile(r"^release_\d{8}_\d{6}$")
VERSION = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)
BACKUP = re.compile(r"^(\d{8}_\d{6})-.+?-(database\.sql(?:\.gz)?|files\.tar|private-files\.tar)$")
PLAIN_ENV = {"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "160"}


# -- config files ------------------------------------------------------------------------
def parse_toml(text: str) -> dict:
    if tomllib is not None:
        return tomllib.loads(text)
    return _minimal_toml(text)


def _minimal_toml(text: str) -> dict:
    """Enough TOML for bench_config.toml: tables, strings, bools, numbers, flat arrays."""
    root: dict = {}
    table = root
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            table = root
            for part in line.strip("[]").split("."):
                table = table.setdefault(part.strip(), {})
            continue
        key, sep, value = line.partition("=")
        if sep:
            table[key.strip().strip('"')] = _toml_value(value.strip())
    return root


def _toml_value(value: str):
    if value in ("true", "false"):
        return value == "true"
    if value.startswith('"') or value.startswith("["):
        try:
            return json.loads(value.replace("'", '"') if value.startswith("[") else value)
        except ValueError:
            return value.strip('"')
    if value.startswith("'"):
        return value.strip("'")
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


# -- git ---------------------------------------------------------------------------------
def git_info(repo_dir: str) -> tuple:
    """(branch, commit sha, remote url) read from ``.git`` without spawning git."""
    git_dir = os.path.join(repo_dir, ".git")
    if os.path.isfile(git_dir):  # worktree / submodule: "gitdir: <path>"
        with open(git_dir) as handle:
            target = handle.read().partition("gitdir:")[2].strip()
        git_dir = os.path.realpath(os.path.join(repo_dir, target))
    if not os.path.isdir(git_dir):
        return "", "", ""
    try:
        head = _read(os.path.join(git_dir, "HEAD")).strip()
    except OSError:
        return "", "", ""
    branch = commit = remote = ""
    if head.startswith("ref:"):
        ref = head[4:].strip()
        branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        commit = _resolve_ref(git_dir, ref)
    else:
        commit = head
    try:
        config = _read(os.path.join(git_dir, "config"))
    except OSError:
        config = ""
    for name in ("upstream", "origin"):
        match = re.search(r'\[remote "%s"\][^\[]*?url\s*=\s*(\S+)' % name, config, re.S)
        if match:
            remote = match.group(1)
            break
    return branch, commit, remote


def _resolve_ref(git_dir: str, ref: str) -> str:
    loose = os.path.join(git_dir, ref)
    if os.path.exists(loose):
        return _read(loose).strip()
    packed = os.path.join(git_dir, "packed-refs")
    if os.path.exists(packed):
        for line in _read(packed).splitlines():
            sha, _, name = line.partition(" ")
            if name.strip() == ref:
                return sha
    return ""


def _read(path: str) -> str:
    with open(path, errors="ignore") as handle:
        return handle.read()


# -- benches -----------------------------------------------------------------------------
def read_app(app_dir: str) -> dict:
    name = os.path.basename(app_dir)
    version = ""
    try:
        match = VERSION.search(_read(os.path.join(app_dir, name, "__init__.py")))
        if match:
            version = match.group(1)
    except OSError:
        pass
    branch, commit, remote = git_info(app_dir)
    return {"name": name, "version": version, "branch": branch, "commit": commit, "remote": remote}


def read_apps(bench_root: str) -> list:
    apps_dir = os.path.join(bench_root, "apps")
    if not os.path.isdir(apps_dir):
        return []
    try:  # apps.txt is bench's own install order; fall back to directory listing
        order = [ln.strip() for ln in _read(os.path.join(bench_root, "sites", "apps.txt")).splitlines()]
    except OSError:
        order = []
    on_disk = sorted(
        d for d in os.listdir(apps_dir) if not d.startswith(".") and os.path.isdir(os.path.join(apps_dir, d))
    )
    names = [n for n in order if n in on_disk] + [n for n in on_disk if n not in order]
    names.sort(key=lambda n: n != "frappe")  # frappe is what every other app builds on
    return [read_app(os.path.join(apps_dir, n)) for n in names]


def read_releases(workspace: str) -> list:
    if not os.path.isdir(workspace):
        return []
    link = os.path.join(workspace, "frappe-bench")
    active = os.path.basename(os.path.realpath(link)) if os.path.islink(link) else None
    names = [
        d for d in os.listdir(workspace) if RELEASE_DIR.match(d) and os.path.isdir(os.path.join(workspace, d))
    ]
    return [{"name": n, "active": n == active} for n in sorted(names, reverse=True)]


def read_site_flags(site_dir: str) -> dict:
    """Only these two switches are read; site_config.json also holds secrets."""
    try:
        config = json.loads(_read(os.path.join(site_dir, "site_config.json")))
    except (OSError, ValueError):
        config = {}
    return {
        "maintenance_mode": bool(config.get("maintenance_mode")),
        "scheduler_paused": bool(config.get("pause_scheduler")),
    }


def list_backups(backups_dir: str) -> list:
    """A site's backup folder grouped into runs, newest first."""
    if not os.path.isdir(backups_dir):
        return []
    runs: dict = {}
    for name in os.listdir(backups_dir):
        match = BACKUP.match(name)
        if match:
            runs.setdefault(match.group(1), {})[match.group(2)] = name
    backups = []
    for stamp, files in runs.items():
        database = files.get("database.sql.gz") or files.get("database.sql")
        if not database:
            continue
        parts = [database, files.get("files.tar"), files.get("private-files.tar")]
        size = sum(os.path.getsize(os.path.join(backups_dir, p)) for p in parts if p)
        backups.append(
            {
                "stamp": stamp,
                "database": parts[0],
                "public_files": parts[1],
                "private_files": parts[2],
                "size": size,
            }
        )
    return sorted(backups, key=lambda b: b["stamp"], reverse=True)


def read_bench(path: str) -> dict:
    name = os.path.basename(path)
    bench = {"name": name, "path": path, "config": {}, "error": ""}
    try:
        bench["config"] = parse_toml(_read(os.path.join(path, "bench_config.toml")))
        bench["name"] = str(bench["config"].get("name") or name)
    except FileNotFoundError:
        bench["error"] = "bench_config.toml is missing — run `fm migrate` or recreate this bench."
    except (OSError, ValueError) as exc:
        bench["error"] = "Unreadable bench_config.toml: %s" % exc
    workspace = os.path.join(path, "workspace")
    bench_root = os.path.join(workspace, "frappe-bench")
    site_dir = os.path.join(bench_root, "sites", bench["name"])
    bench["releases"] = read_releases(workspace)
    deployer = bool(bench["releases"]) or os.path.isdir(os.path.join(workspace, "deployment-data"))
    bench["kind"] = "deployer" if deployer else "fm"
    bench["apps"] = read_apps(bench_root)
    bench["backups"] = list_backups(os.path.join(site_dir, "private", "backups"))
    bench.update(read_site_flags(site_dir))
    bench["compose_file"] = os.path.realpath(os.path.join(path, "docker-compose.yml"))
    return bench


def discover(sites_dir: str) -> list:
    if not os.path.isdir(sites_dir):
        return []
    return [
        read_bench(os.path.join(sites_dir, d))
        for d in sorted(os.listdir(sites_dir))
        if os.path.isfile(os.path.join(sites_dir, d, "docker-compose.yml"))
    ]


# -- docker ------------------------------------------------------------------------------
def compose_state(status: str) -> str:
    """'running(10)' → running, 'running(2), exited(1)' → partial, else stopped."""
    states = re.findall(r"([a-z]+)\(\d+\)", status)
    if states and all(s == "running" for s in states):
        return "running"
    return "partial" if "running" in states else "stopped"


def _docker(docker: str, args: list, timeout: float) -> tuple:
    env = dict(os.environ, **PLAIN_ENV)
    try:
        proc = subprocess.run(
            [docker, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout


def docker_state(docker: str, ps: bool = True, stats: bool = False, disk: bool = False) -> dict:
    code, version = _docker(docker, ["info", "--format", "{{.ServerVersion}}"], 15)
    state = {
        "running": code == 0 and bool(version.strip()),
        "version": version.strip(),
        "compose": [],
        "ps": "",
        "stats": "",
        "df": "",
    }
    if not state["running"]:
        return state
    code, out = _docker(docker, ["compose", "ls", "--all", "--format", "json"], 15)
    try:
        state["compose"] = json.loads(out or "[]") if code == 0 else []
    except ValueError:
        state["compose"] = []
    if ps:
        state["ps"] = _docker(docker, ["ps", "-a", "--no-trunc", "--format", "{{json .}}"], 15)[1]
    if stats:
        state["stats"] = _docker(docker, ["stats", "--no-stream", "--format", "{{json .}}"], 30)[1]
    if disk:
        state["df"] = _docker(docker, ["system", "df", "--format", "{{json .}}"], 30)[1]
    return state


def apply_statuses(benches: list, compose: list) -> None:
    """Set each bench's ``status`` from ``docker compose ls`` (None → Docker unreachable)."""
    by_file = {}
    for project in compose or []:
        for config_file in str(project.get("ConfigFiles", "")).split(","):
            if config_file.strip():
                by_file[os.path.realpath(config_file.strip())] = compose_state(str(project.get("Status", "")))
    for bench in benches:
        if bench["error"]:
            bench["status"] = "broken"
        elif compose is None:
            bench["status"] = "unknown"
        else:
            bench["status"] = by_file.get(bench["compose_file"], "stopped")


# -- entry point -------------------------------------------------------------------------
def snapshot(
    fm_home: str = "~/frappe",
    docker: str = "docker",
    benches: bool = True,
    system: bool = True,
    stats: bool = False,
    disk: bool = False,
) -> dict:
    home = os.path.realpath(os.path.expanduser(fm_home))
    out = {"fm_home": home, "tools": {t: shutil.which(t) for t in ("fm", "fmd", "docker")}}
    state = (
        docker_state(docker, ps=system, stats=stats, disk=disk) if (system or benches) else {"running": False}
    )
    if benches:
        out["benches"] = discover(os.path.join(home, "sites"))
        apply_statuses(out["benches"], state.get("compose") if state["running"] else None)
    if system:
        out["docker"] = state
    return out


if __name__ == "__main__":  # remote: `python3 - '<json kwargs>'` with this file on stdin
    print(json.dumps(snapshot(**json.loads(sys.argv[1] if len(sys.argv) > 1 else "{}"))))
