"""Main window: sidebar navigation + page stack."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from fmapp import APP_NAME, __version__
from fmapp.core.models import AppRef
from fmapp.ui import theme
from fmapp.ui.context import AppContext
from fmapp.ui.dialogs.new_site import NewSiteDialog
from fmapp.ui.jobs import JobRun
from fmapp.ui.pages.activity import ActivityPage
from fmapp.ui.pages.marketplace import MarketplacePage
from fmapp.ui.pages.my_apps import MyAppsPage
from fmapp.ui.pages.settings import SettingsPage
from fmapp.ui.pages.site_detail import SiteDetailPage
from fmapp.ui.pages.sites import SitesPage
from fmapp.ui.pages.system import SystemPage
from fmapp.ui.widgets import StatusRow, label, run_dialog

NAV = (
    ("sites", "Sites"),
    ("system", "System"),
    ("marketplace", "Marketplace"),
    ("my_apps", "My Apps"),
    ("activity", "Activity"),
    ("settings", "Settings"),
)


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.tray: QSystemTrayIcon | None = None  # set by app.py when a tray is available
        self._quitting = False
        self._told_about_tray = False
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(QSize(1000, 640))
        self.resize(1240, 800)
        self.setUnifiedTitleAndToolBarOnMac(True)

        central = QWidget()
        row = QHBoxLayout(central)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.setCentralWidget(central)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(theme.SIDEBAR_WIDTH)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(theme.MD, theme.XL, theme.MD, theme.LG)
        side.setSpacing(0)
        brand = label(APP_NAME)
        brand.setObjectName("Brand")
        sub = label("Frappe sites, locally")
        sub.setObjectName("BrandSub")
        for widget in (brand, sub):
            widget.setContentsMargins(10, 0, 0, 0)
            side.addWidget(widget)
        side.addSpacing(theme.XL)
        self.nav = QListWidget()
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for key, title in NAV:
            item = QListWidgetItem(title)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self._nav_changed)
        side.addWidget(self.nav, 1)

        section = label("STATUS")
        section.setObjectName("SidebarSection")
        section.setContentsMargins(10, 0, 0, theme.XS)
        side.addWidget(section)
        self.health: dict[str, StatusRow] = {}
        for key, title in (
            ("docker", "Docker"),
            ("proxy", "Proxy"),
            ("fm", "Frappe Manager"),
            ("fmd", "Frappe Deployer"),
        ):
            status = StatusRow(title)
            self.health[key] = status
            side.addWidget(status)
        side.addSpacing(theme.MD)
        version = label(f"v{__version__}", "faint")
        version.setContentsMargins(10, 0, 0, 0)
        side.addWidget(version)
        row.addWidget(sidebar)

        self.stack = QStackedWidget()
        self.stack.setObjectName("Content")
        row.addWidget(self.stack, 1)
        self.pages = {
            "sites": SitesPage(ctx),
            "site": SiteDetailPage(ctx),
            "system": SystemPage(ctx),
            "marketplace": MarketplacePage(ctx),
            "my_apps": MyAppsPage(ctx),
            "activity": ActivityPage(ctx),
            "settings": SettingsPage(ctx),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)

        ctx.navigate.connect(self.go)
        ctx.notify.connect(lambda text, ms: self.statusBar().showMessage(text, ms))
        ctx.open_site.connect(self.open_site)
        ctx.open_run.connect(self.open_run)
        ctx.new_site.connect(self.new_site)
        ctx.new_site_from_config.connect(self.new_site_from_config)
        ctx.tools.changed.connect(self._render_health)
        ctx.system.changed.connect(self._render_health)
        ctx.jobs.changed.connect(self._render_badge)
        ctx.jobs.finished.connect(self._job_finished)
        self._shortcuts()
        self.nav.setCurrentRow(0)
        self.statusBar().setSizeGripEnabled(False)

    def _shortcuts(self) -> None:
        new = QAction("New Site", self)
        new.setShortcut(QKeySequence.StandardKey.New)
        new.triggered.connect(lambda: self.new_site([]))
        refresh = QAction("Refresh", self)
        refresh.setShortcut(QKeySequence.StandardKey.Refresh)
        refresh.triggered.connect(self.ctx.benches.refresh)
        prefs = QAction("Settings", self)
        prefs.setShortcut(QKeySequence.StandardKey.Preferences)
        prefs.setMenuRole(QAction.MenuRole.PreferencesRole)
        prefs.triggered.connect(lambda: self.go("settings"))
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        quit_action.triggered.connect(self.request_quit)
        menu = self.menuBar().addMenu("&File")
        for action in (new, refresh, prefs, quit_action):
            menu.addAction(action)
            self.addAction(action)
        for i, (key, _title) in enumerate(NAV, start=1):
            jump = QAction(self)
            jump.setShortcut(QKeySequence(f"Ctrl+{i}"))
            jump.triggered.connect(lambda _=False, k=key: self.go(k))
            self.addAction(jump)

    # -- navigation -----------------------------------------------------------------------
    def go(self, key: str) -> None:
        keys = [k for k, _ in NAV]
        if key in keys:
            self.nav.blockSignals(True)
            self.nav.setCurrentRow(keys.index(key))
            self.nav.blockSignals(False)
        self.stack.setCurrentWidget(self.pages[key])

    def _nav_changed(self, row: int) -> None:
        if 0 <= row < len(NAV):
            self.stack.setCurrentWidget(self.pages[NAV[row][0]])

    def open_site(self, name: str) -> None:
        self.go("sites")
        page: SiteDetailPage = self.pages["site"]  # type: ignore[assignment]
        page.show_site(name)
        self.stack.setCurrentWidget(page)

    def open_run(self, run: JobRun) -> None:
        self.go("activity")
        self.pages["activity"].show_run(run)  # type: ignore[attr-defined]

    def new_site(self, preselect: list[AppRef]) -> None:
        run_dialog(NewSiteDialog(self.ctx, preselect, self))

    def new_site_from_config(self, path: str) -> None:
        dialog = NewSiteDialog(self.ctx, [], self)
        dialog.import_config(path)  # problems show inline in the wizard
        run_dialog(dialog)

    # -- status ---------------------------------------------------------------------------
    def _render_health(self) -> None:
        for key in ("fm", "fmd"):
            status = self.ctx.tools.tools.get(key)
            if status:
                version = status.version.split()[-1] if status.ok and status.version else ""
                self.health[key].set(
                    version if status.ok else "missing", "running" if status.ok else "broken"
                )
        system = self.ctx.system
        if system.engine is not None:
            up = system.engine_running
            self.health["docker"].set("running" if up else "stopped", "running" if up else "broken")
            proxy = system.proxy_running
            self.health["proxy"].set("up" if proxy else "down", "running" if proxy else "broken")

    def _render_badge(self) -> None:
        active = len([r for r in self.ctx.jobs.active()])
        row = [k for k, _ in NAV].index("activity")
        self.nav.item(row).setText(f"Activity  ({active})" if active else "Activity")

    def _job_finished(self, run: JobRun) -> None:
        self.statusBar().showMessage(f"{run.job.title}: {run.state.value}", 8000)
        if not self.isActiveWindow():
            QApplication.alert(self)  # bounce the dock icon / flash the taskbar entry

    def show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def request_quit(self) -> None:
        active = self.ctx.jobs.active()
        if active:
            self.show_and_raise()
            answer = QMessageBox.question(
                self,
                "Jobs still running",
                f"{len(active)} job(s) are still running. Quit and stop them?",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            for run in active:
                run.cancel()
        self._quitting = True
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:
        tray = self.tray
        if not self._quitting and tray and tray.isVisible() and self.ctx.settings.close_to_tray:
            # Keep running in the menu bar / tray; sites and jobs stay under our control.
            event.ignore()
            self.hide()
            if not self._told_about_tray:
                self._told_about_tray = True
                tray.showMessage(
                    f"{APP_NAME} is still running",
                    "Sites keep running. Use the menu-bar icon to reopen or quit.",
                )
            return
        if not self._quitting:
            event.ignore()
            self.request_quit()
            return
        event.accept()
