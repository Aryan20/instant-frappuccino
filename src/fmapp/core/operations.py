"""Translate user intents into jobs: ordered steps of fm / fmd invocations.

A :class:`Job` is pure data, so every command the GUI will run can be unit-tested and
shown to the user ("Review" page) before anything executes. The UI layer only knows how
to execute steps; it never assembles CLI arguments itself.

Every job targets one :class:`~fmapp.core.hosts.Host`. For a server the same argv is
wrapped in SSH (``remote.wrap``) and tools are called by name so the server's own PATH
resolves them; anything that needs this machine (its Docker engine, VS Code) is refused
for servers instead of silently misbehaving.
"""

from __future__ import annotations

import base64
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from fmapp.core import deployer, engine, paths, remote
from fmapp.core.env import GIT_SSH_BATCH, git_env, which
from fmapp.core.hosts import LOCAL, Host
from fmapp.core.models import AppRef, Backup, Bench, BenchKind, SiteSpec
from fmapp.core.settings import Settings

Context = dict[str, Any]
BENCH_DIR = "/workspace/frappe-bench"
# Lock key for engine / global-service jobs so they never overlap each other.
SYSTEM = "@system"
# `fm shell -c` re-quotes its argument (`bash -c "<cmd>"`, then shlex.split), so a script's own
# quotes would be mangled. Shipping it base64-encoded into a temp file survives that, keeps
# stdin free for the script's commands, and still works if fm ever passes -c through as is.
_RUN_ENCODED = "f=$(mktemp) && echo {} | base64 -d > $f && bash $f; r=$?; rm -f $f; exit $r"


def _encoded(script: str) -> str:
    return _RUN_ENCODED.format(base64.b64encode(script.encode()).decode())


# Inside a bench container: never prompt, and use a shared agent on /fm-sockets if one runs
# (`fm shell -c` is non-interactive, so nothing from .bashrc has started or found an agent).
_CONTAINER_GIT_SSH = (
    f'export GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="${{GIT_SSH_COMMAND:-{GIT_SSH_BATCH}}}"; '
    '[ -n "$SSH_AUTH_SOCK" ] || [ ! -S /fm-sockets/ssh-agent.sock ] '
    "|| export SSH_AUTH_SOCK=/fm-sockets/ssh-agent.sock; "
)
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
    # In-process work (e.g. writing a config file). Returns text for the log.
    action: Callable[[Context], str | None] | None = None
    # Written to the process's stdin, then closed (e.g. a config uploaded over SSH).
    stdin: str | None = None
    # What previews and logs show instead of argv (e.g. a container script before encoding).
    shown: str = ""

    def display(self) -> str:
        return self.shown or display_argv(self.argv or [])


@dataclass
class Job:
    title: str
    bench: str | None
    steps: list[Step]
    env: dict[str, str] = field(default_factory=dict)
    secrets: list[str] = field(default_factory=list)
    context: Context = field(default_factory=dict)
    host: Host = LOCAL

    def preview(self) -> str:
        """Human-readable command list for confirmation dialogs."""
        lines = ["$ " + step.display() if step.argv else f"# {step.title}" for step in self.steps]
        text = "\n".join(lines)
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "••••••")
        return text


class MissingTool(RuntimeError):
    pass


class NotOnServer(ValueError):
    """The action only makes sense for this machine (its Docker engine, its editor)."""


def _check_app_names(apps: list[str]) -> list[str]:
    """App names are interpolated into shell snippets: allow Python identifiers only."""
    for app in apps:
        if not re.fullmatch(r"[A-Za-z0-9_]+", app):
            raise ValueError(f"Not a valid app name: {app!r}")
    return apps


class Operations:
    def __init__(self, settings: Settings, host: Host = LOCAL) -> None:
        self.settings = settings
        self.host = host

    # -- tools & job plumbing -----------------------------------------------------------
    def _tool(self, name: str, override: str, missing: str) -> str:
        if not self.host.is_local:
            return name  # resolved by the server's login shell
        path = which(name, override)
        if not path:
            raise MissingTool(missing)
        return path

    def fm(self) -> str:
        return self._tool(
            "fm", self.settings.fm_path, "Frappe Manager (fm) is not installed. Install it from Settings."
        )

    def fmd(self) -> str:
        return self._tool(
            "fmd", self.settings.fmd_path, "Frappe Deployer (fmd) is not installed. Install it from Settings."
        )

    def docker(self) -> str:
        return self._tool(
            "docker",
            self.settings.docker_path,
            "The docker CLI was not found. Install Docker Desktop, OrbStack, Colima or Docker Engine.",
        )

    def _local_only(self, what: str) -> None:
        if not self.host.is_local:
            raise NotOnServer(f"{what} only works for this machine, not for {self.host.name}.")

    @property
    def clone_token(self) -> str:
        """The GitHub token, when clones should use it.

        Never on servers (they use their own git credentials), and not when the user chose
        SSH keys. Without a token, fm, fmd and our ``bench get-app`` clone public repos over
        HTTPS and fall back to SSH for private ones.
        """
        s = self.settings
        return s.github_token if self.host.is_local and not s.git_over_ssh else ""

    def _job(self, title: str, bench: str | None, steps: list[Step], tty: bool = False) -> Job:
        """Wrap every command for the target host; servers get their name in the title."""
        for step in steps:
            if step.argv:
                step.argv = remote.wrap(self.host, step.argv, tty=tty)
                if step.shown and not self.host.is_local:
                    step.shown = f"[{self.host.destination}] {step.shown}"
        token = self.clone_token
        if not self.host.is_local:
            title = f"{title} · {self.host.name}"
        return Job(
            title,
            bench,
            steps,
            env={**git_env(), **({"GITHUB_TOKEN": token} if token else {})},
            secrets=[self.settings.github_token] if self.settings.github_token else [],
            host=self.host,
        )

    def _bench_sh(self, bench: str, script: str, title: str) -> Step:
        """Run a shell snippet inside the bench's frappe container."""
        script = f"cd {BENCH_DIR} || exit 1; {script}"
        shell = [self.fm(), "shell", bench, "--shell-path", "/bin/bash", "-c"]
        return Step(title, [*shell, _encoded(script)], shown=display_argv([*shell, script]))

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
        return self._job(
            f"Create {spec.name}",
            spec.name,
            [Step(f"Create bench {spec.name}", self._fm_create_argv(spec, apps))],
        )

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
            config = deployer.merge_config(spec.base_config, spec.name, apps, options)
        else:
            config = deployer.build_config(spec.name, apps, options)
        return self._deployable(config)

    def _create_deployer_site(self, spec: SiteSpec) -> Job:
        config = self.deployer_config(spec)
        base = replace(spec, environment="prod", developer_mode=False, apps=[])
        frappe = AppRef("frappe/frappe", spec.frappe_ref)
        job = self._job(
            f"Create {spec.name} (Deployer)",
            spec.name,
            [
                Step("Create production bench", self._fm_create_argv(base, [frappe])),
                *self._deploy_steps(spec.name, config, "Build first release and switch"),
            ],
        )
        job.secrets += deployer.secrets_of(config)
        return job

    def _deployable(self, config: dict[str, Any]) -> dict[str, Any]:
        """``${GITHUB_TOKEN}`` only resolves where the token is passed (this machine, if set).

        fmd leaves undefined variables as literal text, which it would then try as a token;
        servers use their own git credentials instead.
        """
        if config.get("github_token") == "${GITHUB_TOKEN}" and not self.clone_token:
            return {**config, "github_token": ""}
        return config

    def _deploy_steps(self, site: str, config: dict[str, Any], title: str) -> list[Step]:
        """Save the config (this machine's copy), put it where fmd reads it, then deploy."""
        host_id = self.host.id

        def save(_: Context) -> str:
            return f"Saved deploy config → {deployer.save(site, config, host_id)}"

        steps = [Step("Save deploy config", action=save)]
        if self.host.is_local:
            config_file = str(deployer.config_path(site))
        else:
            config_file = deployer.remote_path(site)
            upload = f"mkdir -p {deployer.REMOTE_DIR} && umask 077 && cat > {shlex.quote(config_file)}"
            steps.append(
                Step(
                    f"Upload config to {self.host.name}", ["bash", "-c", upload], stdin=deployer.dumps(config)
                )
            )
        steps.append(Step(title, [self.fmd(), "deploy", "pull", "--config", config_file]))
        return steps

    def start(self, bench: str) -> Job:
        return self._job(f"Start {bench}", bench, [Step("Start", [self.fm(), "-n", "start", bench])])

    def stop(self, bench: str) -> Job:
        return self._job(f"Stop {bench}", bench, [Step("Stop", [self.fm(), "-n", "stop", bench])])

    def restart(self, bench: str) -> Job:
        return self._job(f"Restart {bench}", bench, [Step("Restart", [self.fm(), "-n", "restart", bench])])

    def delete(self, bench: str, drop_db: bool = True) -> Job:
        argv = [self.fm(), "-n", "delete", bench, "--yes"]
        argv.append("--delete-db-from-global-db" if drop_db else "--no-delete-db-from-global-db")
        host_id = self.host.id

        def forget_config(_: Context) -> str | None:
            path = deployer.config_path(bench, host_id)
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
        # A tty makes a server stop `fm logs -f` when the stream is closed here.
        return self._job(f"Logs {bench}", None, [Step("Follow logs", argv)], tty=True)

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
        """Prints a one-time Administrator login URL (``bench browse`` can't open the browser)."""
        script = f"bench --site {shlex.quote(bench)} browse --user Administrator"
        return remote.wrap(self.host, self._bench_sh(bench, script, "Login").argv or [])

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
        self._local_only("Opening VS Code")
        return self._job(
            f"Open {bench} in VS Code", None, [Step("fm code", [self.fm(), "-n", "code", bench])]
        )

    def shell_command(self, bench: str, console: bool = False) -> list[str]:
        """An interactive ``fm shell`` (or bench console), over SSH for servers."""
        argv = [self.fm(), "shell", bench, *(["--bench-console"] if console else [])]
        return remote.wrap(self.host, argv, tty=True)

    def restore(self, bench: Bench, backup: Backup, with_files: bool = True) -> Job:
        """Restore a backup from the site's own backups folder (paths mapped into the container)."""

        def inside(name: str) -> str:
            return shlex.quote(f"sites/{bench.name}/private/backups/{name}")

        site = shlex.quote(bench.name)
        restore = f"bench --site {site} restore {inside(backup.database)}"
        if with_files and backup.public_files:
            restore += f" --with-public-files {inside(backup.public_files)}"
        if with_files and backup.private_files:
            restore += f" --with-private-files {inside(backup.private_files)}"
        migrate = self._bench_sh(bench.name, f"bench --site {site} migrate", "bench migrate")
        secret_file = "services/secrets/db_root_password.txt"
        if self.host.is_local:
            password_file = paths.fm_home() / secret_file
            try:
                password = password_file.read_text().strip()
            except OSError as exc:
                raise MissingTool(f"Can't read the MariaDB root password from {password_file}") from exc
            step = self._bench_sh(
                bench.name, f"{restore} --db-root-password {shlex.quote(password)}", f"Restore {backup.stamp}"
            )
            job = self._job(f"Restore {backup.stamp} · {bench.name}", bench.name, [step, migrate])
            job.secrets.append(password)
            return job
        # On a server the password is read there: it never travels over SSH or into the log.
        password_file = remote._home_path(f"{self.host.fm_home.rstrip('/')}/{secret_file}")
        fm_shell = shlex.join([self.fm(), "shell", bench.name, "--shell-path", "/bin/bash", "-c"])
        before, after = _RUN_ENCODED.split("{}")
        container = f"cd {BENCH_DIR} || exit 1; {restore}".replace("%", "%%") + " --db-root-password %q"
        script = (  # the container script is built and encoded on the server; %q quotes the password
            f"pw=$(cat {password_file}) && "
            f"s=$(printf {shlex.quote(container)} \"$pw\" | base64 | tr -d '\\n') && "
            f'{fm_shell} {shlex.quote(before)}"$s"{shlex.quote(after)}'
        )
        shown = f"{fm_shell} {shlex.quote(container.replace('%q', '<root password read on the server>'))}"
        step = Step(f"Restore {backup.stamp}", ["bash", "-c", script], shown=shown)
        return self._job(f"Restore {backup.stamp} · {bench.name}", bench.name, [step, migrate])

    def run_tests(self, bench: str, app: str, module: str = "") -> Job:
        _check_app_names([app])
        site = shlex.quote(bench)
        script = f"bench --site {site} run-tests --app {app}"
        if module:
            script += f" --module {shlex.quote(module)}"
        allow = f"bench --site {site} set-config allow_tests true"
        return self._job(
            f"Tests · {app} · {bench}",
            bench,
            [
                self._bench_sh(bench, allow, "Allow tests on this site"),
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
        return remote.wrap(self.host, [self.fm(), "info", bench])

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
    def _engine(self) -> engine.Provider:
        # Context-only detection: never waits on the daemon, so it is safe on the GUI thread.
        return engine.provider(self.settings.docker_path, self.settings.engine_provider)

    def _ensure_engine_steps(self) -> list[Step]:
        if not self.host.is_local:
            return []  # a server's Docker belongs to its admins; it is never started from here
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
        self._local_only("Controlling the Docker engine")
        docker = self.docker()
        provider = self._engine()
        steps = [
            Step(f"{action.title()} {engine.LABELS[provider]}", argv)
            for argv in engine.control_argv(provider, action, docker)
        ]
        if action != "stop":
            steps.append(Step("Wait for Docker", engine.wait_ready_argv(docker)))
        return self._job(f"{action.title()} Docker engine", SYSTEM, steps)

    def services(self, action: str, service: str = "all") -> Job:
        if action not in ("start", "stop", "restart"):
            raise ValueError(action)
        steps = [] if action == "stop" else self._ensure_engine_steps()
        steps.append(Step(f"{action.title()} {service}", [self.fm(), "-n", "services", action, service]))
        label = "global services" if service == "all" else service
        return self._job(f"{action.title()} {label}", SYSTEM, steps)

    def container(self, action: str, container_id: str, name: str = "") -> Job:
        if action not in ("start", "stop", "restart"):
            raise ValueError(action)
        title = f"{action.title()} {name or container_id[:12]}"
        return self._job(title, None, [Step(title, [self.docker(), action, container_id])])

    def container_logs(self, container_id: str) -> Job:
        argv = [self.docker(), "logs", "--follow", "--tail", "500", container_id]
        return self._job("Container logs", None, [Step("Follow logs", argv)], tty=True)

    def stop_conflicting(self, container_ids: list[str]) -> Job:
        return self._job(
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
        return self._job("Start everything", SYSTEM, steps)

    def stop_everything(self, sites: list[str], stop_engine: bool = False) -> Job:
        steps = [Step(f"Stop {site}", [self.fm(), "-n", "stop", site]) for site in sites]
        steps.append(Step("Stop global services", [self.fm(), "-n", "services", "stop", "all"]))
        if stop_engine and self.host.is_local:
            docker = self.docker()
            steps += [
                Step("Stop Docker engine", argv)
                for argv in engine.control_argv(self._engine(), "stop", docker)
            ]
        return self._job("Stop everything", SYSTEM, steps)

    def reclaim_space(self) -> Job:
        """Dangling images + build cache only — never volumes (they hold your databases)."""
        self._local_only("Reclaiming disk space")
        docker = self.docker()
        return self._job(
            "Reclaim disk space",
            SYSTEM,
            [
                Step("Remove dangling images", [docker, "image", "prune", "--force"]),
                Step("Clear build cache", [docker, "builder", "prune", "--force"]),
            ],
        )

    # -- apps on plain fm benches -------------------------------------------------------
    def _get_app(self, app: AppRef) -> str:
        """``bench get-app`` for one app, run inside the bench container."""
        branch = f"--branch {shlex.quote(app.ref)} " if app.ref else ""
        token = self.clone_token
        if token and app.clone_url.startswith("https://github.com/"):
            url = app.clone_url.replace("https://", f"https://x-access-token:{token}@", 1)
            return f"bench get-app {branch}{shlex.quote(url)} && {_SCRUB_TOKENS}"
        if app.is_ssh:
            return f"{_CONTAINER_GIT_SSH}bench get-app {branch}{shlex.quote(app.repo)}"
        # Public repos clone over HTTPS; private ones with the bench's SSH keys or agent.
        return (
            f"{_CONTAINER_GIT_SSH}url={shlex.quote(app.clone_url)}; "
            'git ls-remote --heads "$url" >/dev/null 2>&1 || '
            f"url={shlex.quote(app.ssh_url)}; "
            f'bench get-app {branch}"$url"'
        )

    def add_apps(self, bench: Bench, apps: list[AppRef]) -> Job:
        if bench.kind is BenchKind.DEPLOYER:
            return self.redeploy_with(bench, add=apps)
        site = shlex.quote(bench.name)
        steps = [self._bench_sh(bench.name, "ls apps > /tmp/.if-apps-before", "Note current apps")]
        for app in apps:
            steps.append(self._bench_sh(bench.name, self._get_app(app), f"Get {app.display()}"))
        # Install whatever get-app added (an app's folder name can differ from its repo name).
        install = (
            "new=$(ls apps | grep -vxF -f /tmp/.if-apps-before | tr '\\n' ' '); "
            f'if [ -n "$new" ]; then bench --site {site} install-app $new; '
            'else echo "No new apps to install."; fi'
        )
        steps.append(self._bench_sh(bench.name, install, "Install into site"))
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
        """Deploy ``config`` (or this machine's saved copy of the site's config) as a release."""
        config = config if config is not None else deployer.load(bench, self.host.id)
        if config is None:
            raise ValueError(f"No deploy config for {bench} yet.")
        config = self._deployable(config)
        job = self._job(
            title or f"Deploy {bench}", bench, self._deploy_steps(bench, config, "Build release and switch")
        )
        job.secrets += deployer.secrets_of(config)
        return job

    def redeploy_with(
        self, bench: Bench, add: list[AppRef] | None = None, remove: list[str] | None = None
    ) -> Job:
        config = deployer.load(bench.name, self.host.id) or self.adopt_config(bench)
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
        return self.deploy(bench.name, config, title=f"Deploy {imported.path.name} → {bench.name}")

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
        return Job(
            f"Install {package}",
            None,
            [
                Step(
                    f"uv tool install {package}",
                    [uv, "tool", "install", "--upgrade", package, "--python", "3.13"],
                )
            ],
        )
