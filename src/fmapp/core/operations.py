"""Translate user intents into jobs: ordered steps of fm / fmd invocations.

A :class:`Job` is pure data, so every command the GUI will run can be unit-tested and
shown to the user ("Review" page) before anything executes. The UI layer only knows how
to execute steps; it never assembles CLI arguments itself.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from fmapp.core import deployer, engine, paths
from fmapp.core.env import which
from fmapp.core.models import AppRef, Backup, Bench, BenchKind, SiteSpec
from fmapp.core.settings import Settings

Context = dict[str, Any]
BENCH_DIR = "/workspace/frappe-bench"
# Lock key for engine / global-service jobs so they never overlap each other.
SYSTEM = "@system"
# `bench get-app` stores the clone URL as the git remote; drop any token we embedded.
_SCRUB_TOKENS = (
    'for d in apps/*/; do u=$(git -C "$d" remote get-url upstream 2>/dev/null) || continue; '
    'case "$u" in *x-access-token:*) git -C "$d" remote set-url upstream '
    '"$(printf %s "$u" | sed -E \'s#//[^@/]+@#//#\')";; esac; done'
)


SENSITIVE_FLAGS = {"--admin-pass", "--github-token", "--fc-key", "--fc-secret", "--newrelic-license-key"}


def display_argv(argv: list[str]) -> str:
    """Shell-quoted command with the values of sensitive flags hidden."""
    shown = ["••••••" if i and argv[i - 1] in SENSITIVE_FLAGS else arg for i, arg in enumerate(argv)]
    return shlex.join(shown).replace("'••••••'", "••••••")


def strip_credentials(url: str) -> str:
    return re.sub(r"(https?://)[^@/]+@", r"\1", url)


@dataclass
class Step:
    title: str
    argv: list[str] | None = None
    # Lazily computed argv (runs right before the step; may depend on earlier steps).
    build: Callable[[Context], list[str] | None] | None = None
    # In-process work (e.g. writing a config file). Returns text for the log.
    action: Callable[[Context], str | None] | None = None


@dataclass
class Job:
    title: str
    bench: str | None
    steps: list[Step]
    env: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    context: Context = field(default_factory=dict)

    def preview(self) -> str:
        """Human-readable command list for confirmation dialogs."""
        lines = []
        for step in self.steps:
            if step.argv:
                lines.append("$ " + display_argv(step.argv))
            elif step.build:
                lines.append(f"# {step.title} (computed when it runs)")
            else:
                lines.append(f"# {step.title}")
        text = "\n".join(lines)
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "••••••")
        return text


class MissingTool(RuntimeError):
    pass


def _check_app_names(apps: list[str]) -> list[str]:
    """App names are interpolated into shell snippets: allow Python identifiers only."""
    for app in apps:
        if not re.fullmatch(r"[A-Za-z0-9_]+", app):
            raise ValueError(f"Not a valid app name: {app!r}")
    return apps


class Operations:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- tool paths ---------------------------------------------------------------------
    def fm(self) -> str:
        path = which("fm", self.settings.fm_path)
        if not path:
            raise MissingTool("Frappe Manager (fm) is not installed. Install it from Settings.")
        return path

    def fmd(self) -> str:
        path = which("fmd", self.settings.fmd_path)
        if not path:
            raise MissingTool("Frappe Deployer (fmd) is not installed. Install it from Settings.")
        return path

    def _job(self, title: str, bench: str | None, steps: list[Step]) -> Job:
        token = self.settings.github_token
        return Job(title, bench, steps, env=self.settings.secret_env(), secrets=[token] if token else [])

    def _bench_sh(self, bench: str, script: str, title: str) -> Step:
        """Run a shell snippet inside the bench's frappe container."""
        return Step(
            title,
            [self.fm(), "shell", bench, "--shell-path", "/bin/bash", "-c", f"cd {BENCH_DIR} && {script}"],
        )

    # -- site lifecycle -----------------------------------------------------------------
    def create_site(self, spec: SiteSpec) -> Job:
        if spec.kind is BenchKind.DEPLOYER:
            return self._create_deployer_site(spec)
        return self._create_fm_site(spec)

    def _fm_create_argv(self, spec: SiteSpec, apps: list[AppRef]) -> list[str]:
        argv = [self.fm(), "-n", "create", spec.name, "--environment", spec.environment]
        argv += ["--developer-mode", "enable" if spec.developer_mode else "disable"]
        argv += ["--admin-pass", spec.admin_password or "admin"]
        for app in apps:
            argv += ["--apps", app.to_fm_arg()]
        if spec.python_version:
            argv += ["--python", spec.python_version]
        if spec.node_version:
            argv += ["--node", spec.node_version]
        if spec.alias_domains:
            argv += ["--alias-domains", ",".join(spec.alias_domains)]
        return argv

    def _create_fm_site(self, spec: SiteSpec) -> Job:
        apps = [AppRef("frappe/frappe", spec.frappe_ref), *spec.apps]
        job = self._job(
            f"Create {spec.name}",
            spec.name,
            [
                Step(f"Create bench {spec.name}", self._fm_create_argv(spec, apps)),
            ],
        )
        return job

    def deployer_config(self, spec: SiteSpec) -> dict[str, Any]:
        """The fmd config a Deployer site will be created with (shown on the Review step)."""
        options = deployer.DeployOptions(
            maintenance_mode=spec.maintenance_mode,
            backups=spec.backups,
            rollback=spec.rollback,
            python_version=spec.python_version,
            node_version=spec.node_version,
        )
        apps = [AppRef("frappe/frappe", spec.frappe_ref), *spec.apps]
        if spec.base_config is not None:
            return deployer.merge_config(spec.base_config, spec.name, apps, options)
        return deployer.build_config(spec.name, apps, options)

    def _create_deployer_site(self, spec: SiteSpec) -> Job:
        apps = [AppRef("frappe/frappe", spec.frappe_ref), *spec.apps]
        config = self.deployer_config(spec)
        base = replace(spec, environment="prod", developer_mode=False, apps=[])
        job = self._job(
            f"Create {spec.name} (Deployer)",
            spec.name,
            [
                Step("Create production bench", self._fm_create_argv(base, [apps[0]])),
                self._write_config_step(spec.name, config),
                Step(
                    "Build first release and switch",
                    [self.fmd(), "deploy", "pull", "--config", str(deployer.config_path(spec.name))],
                ),
            ],
        )
        job.secrets += deployer.secrets_of(config)
        return job

    def _write_config_step(self, site: str, config: dict[str, Any]) -> Step:
        def write(_: Context) -> str:
            return f"Wrote deploy config → {deployer.save(site, config)}"

        return Step("Write deploy config", action=write)

    def start(self, bench: str) -> Job:
        return self._job(f"Start {bench}", bench, [Step("Start", [self.fm(), "-n", "start", bench])])

    def stop(self, bench: str) -> Job:
        return self._job(f"Stop {bench}", bench, [Step("Stop", [self.fm(), "-n", "stop", bench])])

    def restart(self, bench: str) -> Job:
        return self._job(f"Restart {bench}", bench, [Step("Restart", [self.fm(), "-n", "restart", bench])])

    def delete(self, bench: str, drop_db: bool = True) -> Job:
        argv = [self.fm(), "-n", "delete", bench, "--yes"]
        argv.append("--delete-db-from-global-db" if drop_db else "--no-delete-db-from-global-db")

        def forget_config(_: Context) -> str | None:
            path = deployer.config_path(bench)
            if path.exists():
                path.unlink()
                return f"Removed deploy config {path}"
            return None

        return self._job(
            f"Delete {bench}", bench, [Step("Delete bench", argv), Step("Clean up", action=forget_config)]
        )

    def update(self, bench: str, **flags: str) -> Job:
        """``fm update`` with flags like ``developer_mode="enable"``."""
        argv = [self.fm(), "-n", "update", bench]
        for key, value in flags.items():
            argv += [f"--{key.replace('_', '-')}", value]
        return self._job(f"Update {bench}", bench, [Step("Update settings", argv)])

    def logs(self, bench: str, service: str = "") -> Job:
        argv = [self.fm(), "logs", bench, "--follow"]
        if service:
            argv += ["--service", service]
        return self._job(f"Logs {bench}", None, [Step("Follow logs", argv)])

    def migrate(self, bench: str) -> Job:
        return self._job(
            f"Migrate {bench}",
            bench,
            [self._bench_sh(bench, f"bench --site {shlex.quote(bench)} migrate", "bench migrate")],
        )

    def build(
        self,
        bench: str,
        apps: list[str] | None = None,
        production: bool = False,
        force: bool = False,
        clear_cache: bool = False,
    ) -> Job:
        """``bench build`` for the whole bench, or ``--apps a,b`` for just some apps."""
        apps = _check_app_names([a for a in (apps or []) if a])
        script = "bench build"
        if apps:
            script += f" --apps {','.join(apps)}"
        if production:
            script += " --production"
        if force:
            script += " --force"
        target = ", ".join(apps) if apps else "all apps"
        steps = [self._bench_sh(bench, script, f"Build {target}")]
        if clear_cache:
            steps.append(
                self._bench_sh(bench, f"bench --site {shlex.quote(bench)} clear-cache", "Clear cache")
            )
        return self._job(f"Build {target} · {bench}", bench, steps)

    # -- everyday site tools ------------------------------------------------------------
    def clear_cache(self, bench: str, which_: str = "all") -> Job:
        """``site`` → clear-cache, ``website`` → clear-website-cache, ``all`` → both."""
        site = shlex.quote(bench)
        scripts = {
            "site": [f"bench --site {site} clear-cache"],
            "website": [f"bench --site {site} clear-website-cache"],
            "all": [f"bench --site {site} clear-cache", f"bench --site {site} clear-website-cache"],
        }[which_]
        label = {"site": "site cache", "website": "website cache", "all": "caches"}[which_]
        return self._job(
            f"Clear {label} · {bench}", bench, [self._bench_sh(bench, " && ".join(scripts), f"Clear {label}")]
        )

    def login_url_argv(self, bench: str) -> list[str]:
        """Prints a one-time Administrator login URL (``bench browse`` can't open the host browser)."""
        script = f"bench --site {shlex.quote(bench)} browse --user Administrator"
        return self._bench_sh(bench, script, "Login").argv or []

    @staticmethod
    def parse_login_url(output: str) -> str | None:
        match = re.search(r"Login URL:\s*(\S+)", output)
        return match.group(1) if match else None

    def set_admin_password(self, bench: str, password: str) -> Job:
        if not password:
            raise ValueError("The new password can't be empty.")
        script = f"bench --site {shlex.quote(bench)} set-admin-password {shlex.quote(password)}"
        job = self._job(
            f"Set Administrator password · {bench}",
            bench,
            [self._bench_sh(bench, script, "Set Administrator password")],
        )
        job.secrets.append(password)
        return job

    def maintenance(self, bench: str, on: bool) -> Job:
        state = "on" if on else "off"
        script = f"bench --site {shlex.quote(bench)} set-maintenance-mode {state}"
        # Web workers cache site_config.json in memory for ~60s (frappe.config, per process), so
        # without a restart the site keeps its old mode for up to a minute and looks inverted.
        restart = [self.fm(), "-n", "restart", bench, "--web", "--no-workers"]
        return self._job(
            f"Maintenance mode {state} · {bench}",
            bench,
            [
                self._bench_sh(bench, script, f"Maintenance mode {state}"),
                Step("Restart web so it applies now", restart),
            ],
        )

    def scheduler(self, bench: str, pause: bool) -> Job:
        state = "pause" if pause else "resume"
        script = f"bench --site {shlex.quote(bench)} scheduler {state}"
        return self._job(
            f"Scheduler {state} · {bench}", bench, [self._bench_sh(bench, script, f"Scheduler {state}")]
        )

    def run_command(self, bench: str, command: str) -> Job:
        """Any shell command inside the bench's frappe container, from /workspace/frappe-bench."""
        command = command.strip()
        if not command:
            raise ValueError("Type a command to run.")
        return self._job(f"$ {command[:60]} · {bench}", bench, [self._bench_sh(bench, command, command)])

    def open_code(self, bench: str) -> Job:
        return Job(f"Open {bench} in VS Code", None, [Step("fm code", [self.fm(), "-n", "code", bench])])

    def shell_command(self, bench: str, console: bool = False) -> list[str]:
        argv = [self.fm(), "shell", bench]
        return [*argv, "--bench-console"] if console else argv

    def restore(self, bench: Bench, backup: Backup, with_files: bool = True) -> Job:
        """Restore a backup from the site's own backups folder (paths mapped into the container)."""
        root_password = paths.fm_home() / "services" / "secrets" / "db_root_password.txt"
        try:
            password = root_password.read_text().strip()
        except OSError as exc:
            raise MissingTool(f"Can't read the MariaDB root password from {root_password}") from exc

        def inside(path) -> str:
            return f"sites/{bench.name}/private/backups/{path.name}"

        site = shlex.quote(bench.name)
        script = f"bench --site {site} restore {shlex.quote(inside(backup.database))}"
        script += f" --db-root-password {shlex.quote(password)}"
        if with_files and backup.public_files:
            script += f" --with-public-files {shlex.quote(inside(backup.public_files))}"
        if with_files and backup.private_files:
            script += f" --with-private-files {shlex.quote(inside(backup.private_files))}"
        steps = [
            self._bench_sh(bench.name, script, f"Restore {backup.stamp}"),
            self._bench_sh(bench.name, f"bench --site {shlex.quote(bench.name)} migrate", "bench migrate"),
        ]
        job = self._job(f"Restore {backup.stamp} · {bench.name}", bench.name, steps)
        job.secrets.append(password)
        return job

    def run_tests(self, bench: str, app: str, module: str = "") -> Job:
        _check_app_names([app])
        site = shlex.quote(bench)
        script = f"bench --site {site} run-tests --app {app}"
        if module:
            script += f" --module {shlex.quote(module)}"
        return self._job(
            f"Tests · {app} · {bench}",
            bench,
            [
                self._bench_sh(
                    bench, f"bench --site {site} set-config allow_tests true", "Allow tests on this site"
                ),
                self._bench_sh(bench, script, f"Run {app} tests"),
            ],
        )

    def pull_apps(self, bench: str, apps: list[str], migrate: bool = True, build: bool = True) -> Job:
        """``git pull --ff-only`` for each app, then optionally migrate and rebuild just those apps."""
        if not apps:
            raise ValueError("Pick at least one app.")
        steps = []
        for app in _check_app_names(apps):
            git = f"git -C apps/{app}"
            # Fall back to the remote bench created (`upstream`) when no tracking branch is set.
            script = (
                f"{git} pull --ff-only || "
                f'{git} pull --ff-only upstream "$({git} rev-parse --abbrev-ref HEAD)"'
            )
            steps.append(self._bench_sh(bench, script, f"Pull {app}"))
        if migrate:
            steps.append(self._bench_sh(bench, f"bench --site {shlex.quote(bench)} migrate", "bench migrate"))
        if build:
            steps.append(self._bench_sh(bench, f"bench build --apps {','.join(apps)}", "Build pulled apps"))
        return self._job(f"Pull {', '.join(apps)} · {bench}", bench, steps)

    def backup(self, bench: str) -> Job:
        script = f"bench --site {shlex.quote(bench)} backup --with-files"
        return self._job(f"Backup {bench}", bench, [self._bench_sh(bench, script, "bench backup")])

    def info_argv(self, bench: str) -> list[str]:
        return [self.fm(), "info", bench]

    def restart_parts(self, bench: str, part: str) -> Job:
        """Targeted ``fm restart``: web | workers | redis | nginx | containers | all."""
        flags = {
            "web": ["--web", "--no-workers"],
            "workers": ["--no-web", "--workers"],
            "redis": ["--no-web", "--no-workers", "--redis"],
            "nginx": ["--no-web", "--no-workers", "--nginx"],
            "containers": ["--container"],
            "all": ["--web", "--workers", "--redis", "--nginx"],
        }[part]
        argv = [self.fm(), "-n", "restart", bench, *flags]
        return self._job(f"Restart {part} · {bench}", bench, [Step(f"Restart {part}", argv)])

    def repair(self, bench: str) -> Job:
        """The usual "site won't load" fix: services up, proxy reloaded, containers recreated."""
        return self._job(
            f"Repair {bench}",
            bench,
            [
                *self._ensure_engine_steps(),
                Step("Start global services", [self.fm(), "-n", "services", "start", "all"]),
                Step("Restart proxy", [self.fm(), "-n", "services", "restart", "global-nginx-proxy"]),
                Step("Recreate bench containers", [self.fm(), "-n", "start", bench, "--force"]),
            ],
        )

    # -- docker engine & global services ------------------------------------------------
    def docker(self) -> str:
        path = which("docker", self.settings.docker_path)
        if not path:
            raise MissingTool(
                "The docker CLI was not found. Install Docker Desktop, OrbStack, Colima or Docker Engine."
            )
        return path

    def _engine(self) -> engine.Provider:
        # Context-only detection: never waits on the daemon, so it is safe on the GUI thread.
        return engine.provider(self.settings.docker_path, self.settings.engine_provider)

    def _ensure_engine_steps(self) -> list[Step]:
        docker = self.docker()
        start = engine.control_argv(self._engine(), "start", docker)[-1]
        # The "is it running?" check happens inside the step's process, not on the GUI thread.
        start_if_needed = [
            "/bin/sh",
            "-c",
            'if "$0" info >/dev/null 2>&1; then echo "Docker is already running."; else shift; exec "$@"; fi',
            docker,
            "start",
            *start,
        ]
        return [
            Step("Start Docker engine (if stopped)", start_if_needed),
            Step("Wait for Docker", engine.wait_ready_argv(docker)),
        ]

    def engine_action(self, action: str) -> Job:
        docker = self.docker()
        provider = self._engine()
        steps = [
            Step(f"{action.title()} {engine.LABELS[provider]}", argv)
            for argv in engine.control_argv(provider, action, docker)
        ]
        if action != "stop":
            steps.append(Step("Wait for Docker", engine.wait_ready_argv(docker)))
        return Job(f"{action.title()} Docker engine", SYSTEM, steps)

    def services(self, action: str, service: str = "all") -> Job:
        if action not in ("start", "stop", "restart"):
            raise ValueError(action)
        steps = [] if action == "stop" else self._ensure_engine_steps()
        steps.append(Step(f"{action.title()} {service}", [self.fm(), "-n", "services", action, service]))
        label = "global services" if service == "all" else service
        return Job(f"{action.title()} {label}", SYSTEM, steps)

    def container(self, action: str, container_id: str, name: str = "") -> Job:
        if action not in ("start", "stop", "restart"):
            raise ValueError(action)
        title = f"{action.title()} {name or container_id[:12]}"
        return Job(title, None, [Step(title, [self.docker(), action, container_id])])

    def container_logs(self, container_id: str) -> Job:
        return Job(
            "Container logs",
            None,
            [
                Step("Follow logs", [self.docker(), "logs", "--follow", "--tail", "500", container_id]),
            ],
        )

    def stop_conflicting(self, container_ids: list[str]) -> Job:
        return Job(
            "Free ports 80/443",
            SYSTEM,
            [
                Step("Stop containers holding 80/443", [self.docker(), "stop", *container_ids]),
                Step("Start proxy", [self.fm(), "-n", "services", "start", "global-nginx-proxy"]),
            ],
        )

    def start_everything(self, sites: list[str]) -> Job:
        steps = [
            *self._ensure_engine_steps(),
            Step("Start global services", [self.fm(), "-n", "services", "start", "all"]),
        ]
        steps += [Step(f"Start {site}", [self.fm(), "-n", "start", site]) for site in sites]
        return Job("Start everything", SYSTEM, steps)

    def stop_everything(self, sites: list[str], stop_engine: bool = False) -> Job:
        steps = [Step(f"Stop {site}", [self.fm(), "-n", "stop", site]) for site in sites]
        steps.append(Step("Stop global services", [self.fm(), "-n", "services", "stop", "all"]))
        if stop_engine:
            docker = self.docker()
            steps += [
                Step("Stop Docker engine", argv)
                for argv in engine.control_argv(self._engine(), "stop", docker)
            ]
        return Job("Stop everything", SYSTEM, steps)

    def reclaim_space(self) -> Job:
        """Dangling images + build cache only — never volumes (they hold your databases)."""
        docker = self.docker()
        return Job(
            "Reclaim disk space",
            SYSTEM,
            [
                Step("Remove dangling images", [docker, "image", "prune", "--force"]),
                Step("Clear build cache", [docker, "builder", "prune", "--force"]),
            ],
        )

    # -- apps on plain fm benches -------------------------------------------------------
    def _clone_url(self, app: AppRef) -> str:
        url = app.clone_url
        token = self.settings.github_token
        if token and url.startswith("https://github.com/"):
            url = url.replace("https://", f"https://x-access-token:{token}@", 1)
        return url

    def add_apps(self, bench: Bench, apps: list[AppRef]) -> Job:
        if bench.kind is BenchKind.DEPLOYER:
            return self.redeploy_with(bench, add=apps)

        apps_dir = bench.bench_root / "apps"

        def snapshot(ctx: Context) -> str:
            ctx["before"] = {p.name for p in apps_dir.iterdir()} if apps_dir.is_dir() else set()
            return f"{len(ctx['before'])} apps currently in bench"

        steps = [Step("Snapshot apps", action=snapshot)]
        for app in apps:
            branch = f"--branch {shlex.quote(app.ref)} " if app.ref else ""
            script = f"bench get-app {branch}{shlex.quote(self._clone_url(app))}"
            if self.settings.github_token:
                script += f" && {_SCRUB_TOKENS}"
            steps.append(self._bench_sh(bench.name, script, f"Get {app.display()}"))

        def install(ctx: Context) -> list[str] | None:
            after = {p.name for p in apps_dir.iterdir()} if apps_dir.is_dir() else set()
            new = sorted(after - ctx.get("before", set()))
            if not new:
                return None
            names = " ".join(shlex.quote(n) for n in new)
            script = f"bench --site {shlex.quote(bench.name)} install-app {names}"
            return self._bench_sh(bench.name, script, "Install").argv

        steps.append(Step("Install into site", build=install))
        return self._job(f"Add apps to {bench.name}", bench.name, steps)

    def remove_app(self, bench: Bench, app_name: str) -> Job:
        if bench.kind is BenchKind.DEPLOYER:
            return self.redeploy_with(bench, remove=[app_name])
        site, app = shlex.quote(bench.name), shlex.quote(app_name)
        script = f"bench --site {site} uninstall-app {app} --yes --no-backup && bench remove-app {app}"
        return self._job(
            f"Remove {app_name} from {bench.name}",
            bench.name,
            [self._bench_sh(bench.name, script, f"Uninstall {app_name}")],
        )

    # -- deployer -----------------------------------------------------------------------
    def deploy(self, bench: str, config: dict[str, Any] | None = None, title: str = "") -> Job:
        steps = []
        if config is not None:
            steps.append(self._write_config_step(bench, config))
        steps.append(
            Step(
                "Build release and switch",
                [self.fmd(), "deploy", "pull", "--config", str(deployer.config_path(bench))],
            )
        )
        return self._job(title or f"Deploy {bench}", bench, steps)

    def redeploy_with(
        self, bench: Bench, add: list[AppRef] | None = None, remove: list[str] | None = None
    ) -> Job:
        config = deployer.load(bench.name) or self.adopt_config(bench)
        # Edit the existing [[apps]] tables in place so per-app extras (hooks, symlink…) survive.
        tables = [t for t in config.get("apps", []) if isinstance(t, dict) and t.get("repo")]
        dropped = {app.name_guess for app in add or []} | set(remove or [])
        tables = [t for t in tables if AppRef.from_fmd_table(t).name_guess not in dropped]
        config["apps"] = tables + [app.to_fmd_table() for app in add or []]
        verb = "Add apps to" if add else "Remove apps from"
        return self.deploy(bench.name, config, title=f"{verb} {bench.name} (new release)")

    def adopt_config(self, bench: Bench) -> dict[str, Any]:
        """Derive an fmd config from what is installed in a bench right now."""
        apps = []
        for app in bench.apps:
            try:
                source = AppRef.parse(strip_credentials(app.remote)) if app.remote else None
            except ValueError:
                source = None
            source = source or AppRef(f"frappe/{app.name}")
            # Detached HEAD (pinned tag/commit): keep the exact commit, not the default branch.
            apps.append(AppRef(source.repo, app.branch or app.commit, source.subdir))
        return deployer.build_config(bench.name, apps, deployer.DeployOptions())

    def import_config(self, bench: Bench, imported: deployer.ImportedConfig) -> Job:
        """Replace a deployer site's config with an imported file and deploy it."""
        frappe = AppRef("frappe/frappe", imported.frappe_ref or self.settings.default_frappe_branch)
        config = deployer.merge_config(
            imported.config, bench.name, [frappe, *imported.apps], imported.options
        )
        job = self.deploy(bench.name, config, title=f"Deploy {imported.path.name} → {bench.name}")
        job.secrets += deployer.secrets_of(config)
        return job

    def convert_to_deployer(self, bench: Bench) -> Job:
        return self.deploy(
            bench.name, self.adopt_config(bench), title=f"Enable release deploys on {bench.name}"
        )

    def switch_release(self, bench: str, release: str, migrate: bool = True) -> Job:
        argv = [self.fmd(), "release", "switch", bench, release, "--maintenance-mode"]
        argv.append("--migrate" if migrate else "--no-migrate")
        return self._job(f"Switch {bench} → {release}", bench, [Step("Switch release", argv)])

    def cleanup_releases(self, bench: str, keep: int = 3) -> Job:
        argv = [self.fmd(), "cleanup", bench, "-r", str(keep), "-b", str(keep + 2), "-y"]
        return self._job(f"Clean up releases of {bench}", bench, [Step("Prune releases", argv)])

    # -- tooling ------------------------------------------------------------------------
    @staticmethod
    def install_tool(package: str) -> Job:
        uv = which("uv")
        if not uv:
            raise MissingTool("uv is required to install tools: https://docs.astral.sh/uv/")
        python = "3.13"
        return Job(
            f"Install {package}",
            None,
            [
                Step(
                    f"uv tool install {package}",
                    [uv, "tool", "install", "--upgrade", package, "--python", python],
                ),
            ],
        )
