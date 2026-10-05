"""Shared application state handed to every page."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from fmapp.core import containers as containers_mod
from fmapp.core import engine as engine_mod
from fmapp.core import env, hosts, marketplace
from fmapp.core.catalog import Catalog
from fmapp.core.hosts import Host
from fmapp.core.models import Bench
from fmapp.core.operations import Job, MissingTool, NotOnServer, Operations
from fmapp.core.settings import Settings
from fmapp.core.source import Source
from fmapp.ui.async_ import run_async
from fmapp.ui.jobs import JobManager, JobRun


class _HostPoller(QObject):
    """Polls the active host in the background; results for a host we've left are dropped."""

    changed = Signal()

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.loaded = False
        self.error = ""  # e.g. "Couldn't reach staging: Permission denied (publickey)"
        self._busy = False
        self._again = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)

    def start(self) -> None:
        self.restart_timer()
        self.refresh()

    def restart_timer(self) -> None:
        seconds = max(3, self.ctx.settings.refresh_seconds)
        if not self.ctx.host.is_local:
            seconds = max(20, seconds)  # each poll of a server is an SSH round trip
        self.timer.start(seconds * 1000)

    def reset(self) -> None:
        """Forget the previous host's state (called when switching hosts)."""
        self.loaded, self.error = False, ""
        self._clear()
        self.changed.emit()
        self.restart_timer()
        self.refresh()

    def refresh(self) -> None:
        if self._busy:
            self._again = True
            return
        self._busy = True
        host_id = self.ctx.host.id
        source = self.ctx.source
        work = self._work(source)

        def done(result) -> None:
            if host_id == self.ctx.host.id:
                self.error, self.loaded = "", True
                self._apply(result)
            self._settle()

        def failed(message: str) -> None:
            if host_id == self.ctx.host.id:
                self.error, self.loaded = message, True
            self._settle()

        run_async(work, done, failed)

    def _settle(self) -> None:
        self._busy = False
        self.changed.emit()
        if self._again:
            self._again = False
            self.refresh()

    # subclasses
    def _work(self, source: Source) -> Callable[[], object]:
        raise NotImplementedError

    def _apply(self, result) -> None:
        raise NotImplementedError

    def _clear(self) -> None:
        raise NotImplementedError


class BenchStore(_HostPoller):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.benches: list[Bench] = []

    def get(self, name: str) -> Bench | None:
        return next((b for b in self.benches if b.name == name), None)

    def _work(self, source: Source):
        return source.benches

    def _apply(self, benches: list[Bench]) -> None:
        self.benches = benches

    def _clear(self) -> None:
        self.benches = []


class ToolStore(QObject):
    """This machine's tools (fm, fmd, docker, uv, git) for Settings and the wizards."""

    changed = Signal()

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.tools: dict[str, env.ToolStatus] = {}

    def ok(self, name: str) -> bool:
        tool = self.tools.get(name)
        return bool(tool and tool.ok)

    def refresh(self) -> None:
        s = self.ctx.settings
        run_async(lambda: env.probe_all(s.fm_path, s.fmd_path, s.docker_path), self._loaded)

    def _loaded(self, tools: dict[str, env.ToolStatus]) -> None:
        self.tools = tools
        self.changed.emit()


class SystemStore(_HostPoller):
    """Live runtime state of the active host: engine, containers (+ stats on demand), disk."""

    def __init__(self, ctx: AppContext) -> None:
        super().__init__(ctx)
        self.want_stats = False  # set while a page that shows CPU/memory is visible
        self._disk_requested = False
        self._clear()

    def _clear(self) -> None:
        self.engine: engine_mod.EngineInfo | None = None
        self.containers: list[containers_mod.Container] = []
        self.conflicts: list[containers_mod.Container] = []
        self.disk: list[containers_mod.DiskRow] = []
        self.host_tools: dict[str, str | None] = {}  # fm / fmd / docker paths on the host

    @property
    def engine_running(self) -> bool:
        return bool(self.engine and self.engine.running)

    def has_tool(self, name: str) -> bool:
        if self.ctx.host.is_local:
            return self.ctx.tools.ok(name)
        return bool(self.host_tools.get(name))

    def service(self, name: str) -> containers_mod.Container | None:
        return next((c for c in self.containers if c.is_global and c.service == name), None)

    def for_bench(self, bench: str) -> list[containers_mod.Container]:
        return [c for c in self.containers if c.bench == bench]

    @property
    def proxy_running(self) -> bool:
        proxy = self.service("global-nginx-proxy")
        return bool(proxy and proxy.running)

    def refresh(self, with_disk: bool = False) -> None:
        self._disk_requested = self._disk_requested or with_disk
        super().refresh()  # an in-flight refresh re-runs, so a queued disk request is honoured

    def _work(self, source: Source):
        stats, disk = self.want_stats, self._disk_requested
        self._disk_requested = False
        return lambda: source.system(stats=stats, disk=disk)

    def _apply(self, state) -> None:
        self.engine = state.engine
        self.containers = [c for c in state.containers if c.bench != ""]
        self.conflicts = containers_mod.port_conflicts(state.containers)
        self.host_tools = state.tools
        if state.disk is not None:
            self.disk = state.disk


class MarketStore(QObject):
    changed = Signal()
    details_loaded = Signal(str)  # app name

    def __init__(self) -> None:
        super().__init__()
        self.apps: list[marketplace.MarketplaceApp] = []
        self.details: dict[str, marketplace.AppDetails] = {}
        self.error = ""
        self._loading = False
        self._pending: set[str] = set()

    def load(self, force: bool = False) -> None:
        if self._loading or (self.apps and not force):
            return
        self._loading = True

        def done(apps: list[marketplace.MarketplaceApp]) -> None:
            self._loading = False
            self.apps, self.error = apps, "" if apps else "Marketplace is unreachable."
            self.changed.emit()

        def failed(message: str) -> None:
            self._loading = False
            self.error = message
            self.changed.emit()

        run_async(lambda: marketplace.fetch_index(force), done, failed)

    def get(self, name: str) -> marketplace.MarketplaceApp | None:
        return next((a for a in self.apps if a.name == name), None)

    def request_details(self, app: marketplace.MarketplaceApp) -> None:
        if app.name in self.details or app.name in self._pending:
            return
        self._pending.add(app.name)

        def done(details: marketplace.AppDetails) -> None:
            self._pending.discard(app.name)
            self.details[app.name] = details
            self.details_loaded.emit(app.name)

        def failed(_message: str) -> None:
            self._pending.discard(app.name)
            fallback = marketplace.AppDetails(name=app.name)
            marketplace.resolve_repo(fallback)
            self.details[app.name] = fallback
            self.details_loaded.emit(app.name)

        run_async(lambda: marketplace.fetch_details(app), done, failed)


class AppContext(QObject):
    """Settings + stores + navigation requests. Pages emit, MainWindow routes."""

    open_site = Signal(str)
    open_run = Signal(object)  # JobRun
    new_site = Signal(list)  # preselected AppRefs
    new_site_from_config = Signal(str)  # path to an fmd site.toml
    navigate = Signal(str)  # page key
    host_changed = Signal()
    hosts_changed = Signal()  # a server was added, edited or removed
    notify = Signal(str, int)  # status-bar message, timeout ms (0 = until replaced)

    def __init__(self) -> None:
        super().__init__()
        self.settings = Settings.load()
        self.settings.apply_env()
        self.host = hosts.get(self.settings, self.settings.active_host)
        self.catalog = Catalog()
        self.jobs = JobManager(self)
        self.benches = BenchStore(self)
        self.tools = ToolStore(self)
        self.market = MarketStore()
        self.system = SystemStore(self)
        self.jobs.finished.connect(lambda _run: (self.benches.refresh(), self.system.refresh()))

    def hosts(self) -> list[Host]:
        return hosts.all_hosts(self.settings)

    def set_host(self, host_id: str) -> None:
        host = hosts.get(self.settings, host_id)
        if host.id == self.host.id:
            return
        self.host = host
        self.settings.active_host = host.id
        self.settings.save()
        self.benches.reset()
        self.system.reset()
        self.host_changed.emit()

    def save_host(self, host: Host) -> None:
        saved = [h for h in self.settings.hosts if h.get("id") != host.id]
        index = next((i for i, h in enumerate(self.settings.hosts) if h.get("id") == host.id), len(saved))
        saved.insert(index, host.to_dict())
        self.settings.hosts = saved
        self.settings.save()
        if host.id == self.host.id:  # editing the active server: reconnect with its new details
            self.host = host
            self.benches.reset()
            self.system.reset()
            self.host_changed.emit()
        self.hosts_changed.emit()

    def remove_host(self, host_id: str) -> None:
        if host_id == self.host.id:
            self.set_host(hosts.LOCAL_ID)
        self.settings.hosts = [h for h in self.settings.hosts if h.get("id") != host_id]
        self.settings.save()
        self.hosts_changed.emit()

    @property
    def source(self) -> Source:
        return Source(self.settings, self.host)

    def sites_to_start(self, benches: list[Bench] | None = None) -> list[str]:
        """Sites chosen in Settings, else every healthy bench (of ``benches`` or this host)."""
        known = {b.name for b in (self.benches.benches if benches is None else benches) if not b.error}
        chosen = [s for s in self.settings.autostart_sites if s in known]
        return chosen or sorted(known)

    def running_sites(self) -> list[str]:
        return [b.name for b in self.benches.benches if b.status.value in ("running", "partial")]

    @property
    def ops(self) -> Operations:
        return Operations(self.settings, self.host)

    def submit(
        self, parent: QWidget, build: Callable[[Operations], Job], show: bool = False
    ) -> JobRun | None:
        """Build a job (reporting missing tools nicely) and queue it."""
        try:
            job = build(self.ops)
        except MissingTool as exc:
            box = QMessageBox(QMessageBox.Icon.Warning, "Tool missing", str(exc), parent=parent)
            settings_btn = box.addButton("Open Settings", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is settings_btn:
                self.navigate.emit("settings")
            return None
        except NotOnServer as exc:
            QMessageBox.information(parent, "Not available on servers", str(exc))
            return None
        except ValueError as exc:
            QMessageBox.warning(parent, "Invalid input", str(exc))
            return None
        if job.host.production and not self.confirm_production(parent, job):
            return None
        run = self.jobs.submit(job)
        if show:
            self.open_run.emit(run)
        return run

    @staticmethod
    def confirm_production(parent: QWidget | None, job: Job) -> bool:
        """Every change on a production server shows its exact commands and asks first."""
        box = QMessageBox(
            QMessageBox.Icon.Warning,
            f"Run on {job.host.name} (production)?",
            f"<b>{job.title}</b><br>This changes a production server; live users may be affected.",
            parent=parent,
        )
        box.setDetailedText(job.preview())
        run = box.addButton("Run on production", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is run
