"""Shared application state handed to every page."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from fmapp.core import benches as bench_io
from fmapp.core import containers as containers_mod
from fmapp.core import engine as engine_mod
from fmapp.core import env, marketplace
from fmapp.core.catalog import Catalog
from fmapp.core.models import Bench
from fmapp.core.operations import Job, MissingTool, Operations
from fmapp.core.settings import Settings
from fmapp.ui.async_ import run_async
from fmapp.ui.jobs import JobManager, JobRun


class BenchStore(QObject):
    changed = Signal()

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.benches: list[Bench] = []
        self.loaded = False
        self._refreshing = False
        self._again = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)

    def start(self) -> None:
        self.timer.start(max(3, self.ctx.settings.refresh_seconds) * 1000)
        self.refresh()

    def get(self, name: str) -> Bench | None:
        return next((b for b in self.benches if b.name == name), None)

    def refresh(self) -> None:
        if self._refreshing:
            self._again = True
            return
        self._refreshing = True
        docker_path = self.ctx.settings.docker_path

        def work() -> list[Bench]:
            return bench_io.discover(statuses=bench_io.compose_statuses(docker_path))

        run_async(work, self._loaded, self._failed)

    def _loaded(self, benches: list[Bench]) -> None:
        self.benches = benches
        self.loaded = True
        self._settle()

    def _failed(self, _message: str) -> None:
        self._settle()

    def _settle(self) -> None:
        self._refreshing = False
        self.changed.emit()
        if self._again:
            self._again = False
            self.refresh()


class ToolStore(QObject):
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


class SystemStore(QObject):
    """Live runtime state: engine, containers (+ stats on demand), port conflicts, disk."""

    changed = Signal()

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.engine: engine_mod.EngineInfo | None = None
        self.containers: list[containers_mod.Container] = []
        self.conflicts: list[containers_mod.Container] = []
        self.disk: list[containers_mod.DiskRow] = []
        self.want_stats = False  # set while a page that shows CPU/memory is visible
        self._busy = False
        self._disk_requested = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)

    def start(self) -> None:
        self.timer.start(max(3, self.ctx.settings.refresh_seconds) * 1000)
        self.refresh()

    @property
    def engine_running(self) -> bool:
        return bool(self.engine and self.engine.running)

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
        if self._busy:
            return  # the in-flight refresh re-runs if disk usage was asked for meanwhile
        self._busy = True
        with_disk, self._disk_requested = self._disk_requested, False
        s = self.ctx.settings
        stats = self.want_stats

        def work():
            info = engine_mod.detect(s.docker_path, s.engine_provider)
            if not info.running:
                return info, [], [], None
            found = containers_mod.list_containers(s.docker_path, include_foreign=True)
            ours = [c for c in found if c.bench != ""]
            conflicts = containers_mod.port_conflicts(found)
            if stats:
                containers_mod.attach_stats(ours, s.docker_path)
            disk = containers_mod.disk_usage(s.docker_path) if with_disk else None
            return info, ours, conflicts, disk

        def done(result) -> None:
            self._busy = False
            self.engine, self.containers, self.conflicts, disk = result
            if disk is not None:
                self.disk = disk
            self.changed.emit()
            if self._disk_requested:
                self.refresh()

        def failed(_message: str) -> None:
            self._busy = False
            if self._disk_requested:
                self.refresh()

        run_async(work, done, failed)


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
    notify = Signal(str, int)  # status-bar message, timeout ms (0 = until replaced)

    def __init__(self) -> None:
        super().__init__()
        self.settings = Settings.load()
        self.settings.apply_env()
        self.catalog = Catalog()
        self.jobs = JobManager(self)
        self.benches = BenchStore(self)
        self.tools = ToolStore(self)
        self.market = MarketStore()
        self.system = SystemStore(self)
        self.jobs.finished.connect(lambda _run: (self.benches.refresh(), self.system.refresh()))

    def sites_to_start(self) -> list[str]:
        """Sites chosen in Settings, else every healthy bench."""
        known = {b.name for b in self.benches.benches if not b.error}
        chosen = [s for s in self.settings.autostart_sites if s in known]
        return chosen or sorted(known)

    def running_sites(self) -> list[str]:
        return [b.name for b in self.benches.benches if b.status.value in ("running", "partial")]

    @property
    def ops(self) -> Operations:
        return Operations(self.settings)

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
        except ValueError as exc:
            QMessageBox.warning(parent, "Invalid input", str(exc))
            return None
        run = self.jobs.submit(job)
        if show:
            self.open_run.emit(run)
        return run
