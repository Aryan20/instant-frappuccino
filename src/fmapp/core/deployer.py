"""fmd (Frappe Deployer) site configs.

Each deployer site gets a ``<site>.toml`` in our config dir — the desired state of the
site, like a Frappe Cloud "bench group". Deploying = ``fmd deploy pull --config <file>``,
which builds a new immutable release and atomically switches to it.
"""

from __future__ import annotations

import copy
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from fmapp.core import paths
from fmapp.core.models import AppRef


@dataclass
class DeployOptions:
    maintenance_mode: bool = True
    backups: bool = True
    rollback: bool = True
    migrate: bool = True
    releases_retain_limit: int = 5
    python_version: str = ""
    node_version: str = ""


REMOTE_DIR = ".instant-frappuccino/deployer"  # on servers, relative to the SSH login's home


def config_path(site: str, host_id: str = "local") -> Path:
    """This machine's copy of a site's config; servers' copies are kept per server."""
    folder = paths.deployer_configs_dir()
    if host_id != "local":
        folder = folder / host_id
        folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{site}.toml"


def remote_path(site: str) -> str:
    return f"{REMOTE_DIR}/{site}.toml"


def dumps(config: dict[str, Any]) -> str:
    return tomli_w.dumps(config)


def build_config(site: str, apps: list[AppRef], options: DeployOptions) -> dict[str, Any]:
    release: dict[str, Any] = {"releases_retain_limit": options.releases_retain_limit}
    if options.python_version:
        release["python_version"] = options.python_version
    if options.node_version:
        release["node_version"] = options.node_version
    return {
        "site_name": site,
        "bench_name": site,
        # fmd substitutes ${VAR}; the token itself is passed via env, never written to disk.
        "github_token": "${GITHUB_TOKEN}",
        "apps": [a.to_fmd_table() for a in apps],
        "release": release,
        "switch": {
            "migrate": options.migrate,
            "maintenance_mode": options.maintenance_mode,
            "maintenance_mode_phases": ["migrate"],
            "backups": options.backups,
            "rollback": options.rollback,
            "install_apps": True,
        },
    }


def load(site: str, host_id: str = "local") -> dict[str, Any] | None:
    try:
        return tomllib.loads(config_path(site, host_id).read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None


def save(site: str, config: dict[str, Any], host_id: str = "local") -> Path:
    target = config_path(site, host_id)
    paths.write_private(target, dumps(config))  # imported configs may carry credentials
    return target


def apps_of(config: dict[str, Any] | None) -> list[AppRef]:
    if not config:
        return []
    return [AppRef.from_fmd_table(t) for t in config.get("apps", []) if t.get("repo")]


def options_of(config: dict[str, Any] | None) -> DeployOptions:
    if not config:
        return DeployOptions()
    switch = config.get("switch", {})
    release = config.get("release", {})
    defaults = DeployOptions()
    return DeployOptions(
        maintenance_mode=switch.get("maintenance_mode", defaults.maintenance_mode),
        backups=switch.get("backups", defaults.backups),
        rollback=switch.get("rollback", defaults.rollback),
        migrate=switch.get("migrate", defaults.migrate),
        releases_retain_limit=release.get("releases_retain_limit", defaults.releases_retain_limit),
        python_version=release.get("python_version", ""),
        node_version=release.get("node_version", ""),
    )


# -- importing an existing fmd config ------------------------------------------------------
class ConfigError(ValueError):
    pass


_HOOK = re.compile(r"^(host_)?(before|after)_")
_SECRET_KEY = re.compile(r"token|secret|password|api_key|license", re.I)
# Sections and keys the wizard edits; everything else in an imported file is kept verbatim.
_KEPT_SECTIONS = {
    "ship": "ship target",
    "remote_worker": "remote worker",
    "fc": "Frappe Cloud sync",
    "configure": "configure options",
}


@dataclass
class ImportedConfig:
    path: Path
    config: dict[str, Any]
    site: str
    frappe_ref: str
    apps: list[AppRef]  # everything except frappe itself
    options: DeployOptions
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        count = len(self.apps)
        parts = [
            f"{self.site}",
            f"Frappe {self.frappe_ref or '(default)'}",
            f"{count} app{'s' if count != 1 else ''}",
        ]
        return " · ".join(parts + self.notes)


def _app_key(table_or_ref: dict[str, Any] | AppRef) -> tuple[str, str]:
    ref = table_or_ref if isinstance(table_or_ref, AppRef) else AppRef.from_fmd_table(table_or_ref)
    return ((ref.org_repo or ref.repo).lower(), ref.subdir.strip("/"))


def load_file(path: str | Path) -> ImportedConfig:
    """Read and validate a user-provided fmd ``site.toml``."""
    path = Path(path).expanduser()
    try:
        config = tomllib.loads(path.read_text())
    except OSError as exc:
        raise ConfigError(f"Couldn't read {path.name}: {exc.strerror or exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid TOML: {exc}") from exc

    site = str(config.get("site_name", "")).strip().lower()
    if not site:
        raise ConfigError(f"{path.name} has no site_name — it's required by fmd.")
    bench = str(config.get("bench_name", "") or "").strip().lower()
    if bench and bench != site:
        raise ConfigError(
            f"bench_name ({bench}) differs from site_name ({site}). Frappe Manager names a bench "
            "after its site, so set both to the same value (or leave bench_name empty)."
        )

    tables = config.get("apps", [])
    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise ConfigError("[[apps]] must be a list of tables with repo/ref keys.")
    apps: list[AppRef] = []
    frappe_ref, has_frappe = "", False
    for index, table in enumerate(tables, start=1):
        if not table.get("repo"):
            raise ConfigError(f"App #{index} in [[apps]] has no repo.")
        try:
            ref = AppRef.parse(str(table["repo"]))
        except ValueError as exc:
            raise ConfigError(f"App #{index}: {exc}") from exc
        ref = AppRef(ref.repo, str(table.get("ref", "") or ref.ref), str(table.get("subdir_path", "") or ""))
        if ref.name_guess == "frappe" and not ref.subdir:
            frappe_ref, has_frappe = ref.ref, True
        else:
            apps.append(ref)

    notes = []
    hooked = sum(1 for t in tables if any(_HOOK.match(k) and v for k, v in t.items()))
    if hooked:
        notes.append(f"hooks on {hooked} app{'s' if hooked > 1 else ''} kept")
    for section, label in _KEPT_SECTIONS.items():
        if any(v not in ("", None, [], {}) for v in (config.get(section) or {}).values()):
            notes.append(f"{label} kept")
    if not has_frappe:
        notes.append("no frappe entry — the default Frappe branch will be added")
    return ImportedConfig(path, config, site, frappe_ref, apps, options_of(config), notes)


def merge_config(
    base: dict[str, Any], site: str, apps: list[AppRef], options: DeployOptions
) -> dict[str, Any]:
    """Apply wizard choices onto an imported config without dropping anything we don't edit.

    Per-app extras (hooks, ``symlink``, ``shallow_clone``…) follow their app by repo; unknown
    sections ([ship], [fc], [remote_worker]…) and unknown keys pass through untouched.
    """
    config = copy.deepcopy(base)
    config["site_name"] = site
    config["bench_name"] = site
    if not config.get("github_token"):
        config["github_token"] = "${GITHUB_TOKEN}"
    originals = {_app_key(t): t for t in base.get("apps", []) if isinstance(t, dict) and t.get("repo")}
    tables = []
    for app in apps:
        original = originals.get(_app_key(app), {})
        extras = {k: v for k, v in original.items() if k not in ("repo", "ref", "subdir_path")}
        table = {**app.to_fmd_table(), **extras}
        if app.org_repo and original.get("repo") and _app_key(original) == _app_key(app):
            table["repo"] = original["repo"]  # keep the user's spelling (URL vs org/repo)
        tables.append(table)
    config["apps"] = tables

    release = config.setdefault("release", {})
    for key in ("python_version", "node_version"):
        value = getattr(options, key)
        if value:
            release[key] = value
        else:
            release.pop(key, None)
    release.setdefault("releases_retain_limit", options.releases_retain_limit)
    switch = config.setdefault("switch", {})
    switch.update(
        migrate=options.migrate,
        maintenance_mode=options.maintenance_mode,
        backups=options.backups,
        rollback=options.rollback,
    )
    switch.setdefault("maintenance_mode_phases", ["migrate"])
    switch.setdefault("install_apps", True)
    return config


def secrets_of(config: dict[str, Any]) -> list[str]:
    """Literal credentials inside a config, so job logs can mask them."""
    found: list[str] = []

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, k)
        elif isinstance(node, list):
            for item in node:
                walk(item, key)
        elif isinstance(node, str) and _SECRET_KEY.search(key) and node and not node.startswith("$"):
            found.append(node)

    walk(config)
    return found
