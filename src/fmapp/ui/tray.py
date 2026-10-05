"""Menu-bar (macOS) / system-tray (Linux) icon with one-click actions."""

from __future__ import annotations

import sys
from collections.abc import Callable
from importlib import resources

from PySide6.QtGui import QAction, QIcon, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from fmapp import APP_NAME
from fmapp.core.models import BenchStatus
from fmapp.ui.context import AppContext
from fmapp.ui.site_tools import add_cache_actions, login_as_admin, open_in_browser


def tray_icon() -> QIcon:
    data = resources.files("fmapp.assets").joinpath("tray.svg").read_bytes()
    pixmap = QPixmap()
    pixmap.loadFromData(data, "SVG")
    icon = QIcon(pixmap)
    if sys.platform == "darwin":
        icon.setIsMask(True)  # template image: macOS tints it for light/dark menu bars
    return icon


class Tray(QSystemTrayIcon):
    def __init__(
        self,
        ctx: AppContext,
        show_window: Callable[[], None],
        quit_app: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(tray_icon(), parent)
        self.ctx = ctx
        self.show_window = show_window
        self.quit_app = quit_app
        self.setToolTip(APP_NAME)
        self.menu = QMenu()
        self.setContextMenu(self.menu)
        self.menu.aboutToShow.connect(self._build)
        self.activated.connect(self._activated)
        self._build()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Linux trays: left click opens the window; macOS always shows the menu.
        if reason == QSystemTrayIcon.ActivationReason.Trigger and sys.platform != "darwin":
            self.show_window()

    def _submit(self, build) -> None:
        self.ctx.submit(None, build)

    def _build(self) -> None:
        m = self.menu
        m.clear()
        system = self.ctx.system
        info = system.engine
        if info is None:
            state = "Checking Docker…"
        elif not info.running:
            state = f"● {info.label}: stopped"
        else:
            proxy = "proxy up" if system.proxy_running else "proxy DOWN"
            running = len(self.ctx.running_sites())
            state = f"● Docker running · {proxy} · {running} site(s) up"
        header = QAction(state, m)
        header.setEnabled(False)
        m.addAction(header)
        m.addSeparator()
        m.addAction(f"Open {APP_NAME}", self.show_window)
        m.addAction(
            "Start everything",
            lambda: self._submit(lambda ops: ops.start_everything(self.ctx.sites_to_start())),
        )
        m.addAction(
            "Stop everything",
            lambda: self._submit(
                lambda ops: ops.stop_everything(
                    self.ctx.running_sites(), self.ctx.settings.stop_engine_with_everything
                )
            ),
        )
        m.addSeparator()

        sites = m.addMenu("Sites")
        if not self.ctx.benches.benches:
            sites.addAction("No sites yet").setEnabled(False)
        for bench in self.ctx.benches.benches:
            running = bench.status in (BenchStatus.RUNNING, BenchStatus.PARTIAL)
            sub = sites.addMenu(f"{'●' if running else '○'}  {bench.name}")
            sub.addAction("Open in browser", lambda b=bench: open_in_browser(b.url)).setEnabled(running)
            sub.addAction(
                "Log in as Admin", lambda b=bench: login_as_admin(self.ctx, None, b.name)
            ).setEnabled(running)
            cache = sub.addMenu("Clear cache")
            cache.setEnabled(running)
            add_cache_actions(self.ctx, None, bench.name, cache)
            if running:
                sub.addAction("Restart", lambda b=bench: self._submit(lambda ops: ops.restart(b.name)))
                sub.addAction("Stop", lambda b=bench: self._submit(lambda ops: ops.stop(b.name)))
            else:
                sub.addAction("Start", lambda b=bench: self._submit(lambda ops: ops.start(b.name)))
            sub.addAction("Repair", lambda b=bench: self._submit(lambda ops: ops.repair(b.name)))
            sub.addAction("Manage…", lambda b=bench: (self.show_window(), self.ctx.open_site.emit(b.name)))

        services = m.addMenu("Services")
        services.addAction(
            "Restart proxy", lambda: self._submit(lambda ops: ops.services("restart", "global-nginx-proxy"))
        )
        services.addAction(
            "Restart database", lambda: self._submit(lambda ops: ops.services("restart", "global-db"))
        )
        services.addAction("Restart all services", lambda: self._submit(lambda ops: ops.services("restart")))
        engine = m.addMenu("Docker engine")
        if info and info.running:
            engine.addAction("Restart engine", lambda: self._submit(lambda ops: ops.engine_action("restart")))
            engine.addAction("Stop engine", lambda: self._submit(lambda ops: ops.engine_action("stop")))
        else:
            engine.addAction("Start engine", lambda: self._submit(lambda ops: ops.engine_action("start")))
        active = len(self.ctx.jobs.active())
        if active:
            m.addSeparator()
            m.addAction(
                f"{active} job(s) running…", lambda: (self.show_window(), self.ctx.navigate.emit("activity"))
            )
        m.addSeparator()
        m.addAction(f"Quit {APP_NAME}", self.quit_app)
