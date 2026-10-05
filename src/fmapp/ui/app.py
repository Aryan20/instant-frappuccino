"""QApplication bootstrap: single instance, tray, background mode and auto-start."""

from __future__ import annotations

import getpass
import sys
from importlib import resources

from PySide6.QtCore import QCoreApplication, Qt, QThreadPool
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from fmapp import APP_ID, APP_NAME, ORG_DOMAIN, __version__
from fmapp.core import autostart
from fmapp.ui import theme
from fmapp.ui.context import AppContext
from fmapp.ui.main_window import MainWindow
from fmapp.ui.tray import Tray

SERVER_NAME = f"{APP_ID}-{getpass.getuser()}"


def app_icon() -> QIcon:
    svg = resources.files("fmapp.assets").joinpath("icon.svg").read_bytes()
    pixmap = QPixmap()
    pixmap.loadFromData(svg, "SVG")
    return QIcon(pixmap)


def _already_running() -> bool:
    """Ask a running instance to show its window; True if one answered."""
    socket = QLocalSocket()
    socket.connectToServer(SERVER_NAME)
    if socket.waitForConnected(300):
        socket.write(b"show")
        socket.waitForBytesWritten(300)
        socket.disconnectFromServer()
        return True
    return False


def run(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    background = autostart.BACKGROUND_FLAG in argv
    QCoreApplication.setOrganizationDomain(ORG_DOMAIN)
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setApplicationVersion(__version__)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication([a for a in argv if a != autostart.BACKGROUND_FLAG])
    if _already_running():
        return 0
    app.setDesktopFileName(APP_ID)  # matches packaging/linux/instant-frappuccino.desktop (Wayland app id)
    app.setWindowIcon(app_icon())

    ctx = AppContext()
    theme.apply(app, ctx.settings.theme)
    window = MainWindow(ctx)

    server = QLocalServer(app)
    QLocalServer.removeServer(SERVER_NAME)  # stale socket from a crash
    server.listen(SERVER_NAME)

    def handoff() -> None:
        socket = server.nextPendingConnection()  # another launch: just show our window
        socket.disconnectFromServer()
        socket.deleteLater()
        window.show_and_raise()

    server.newConnection.connect(handoff)

    tray = None
    if ctx.settings.tray and QSystemTrayIcon.isSystemTrayAvailable():
        tray = Tray(ctx, window.show_and_raise, window.request_quit)
        tray.show()
        window.tray = tray
        app.setQuitOnLastWindowClosed(False)
    if not (background and tray):
        window.show()
    if sys.platform == "darwin":
        # Dock-icon click / `open` / Finder relaunch only re-activates the running app;
        # bring the window back if it was closed to the menu bar.
        app.applicationStateChanged.connect(
            lambda state: (
                window.show_and_raise()
                if state == Qt.ApplicationState.ApplicationActive and not window.isVisible()
                else None
            )
        )

    ctx.tools.refresh()
    ctx.benches.start()
    ctx.system.start()
    autostart.sync(ctx.settings.autostart)
    if ctx.settings.autostart != "off" or background:
        # Wait for the first bench scan (slow while Docker is still booting) so we know the sites.
        def start_once() -> None:
            if ctx.benches.loaded:
                ctx.benches.changed.disconnect(start_once)
                ctx.submit(window, lambda ops: ops.start_everything(ctx.sites_to_start()))

        ctx.benches.changed.connect(start_once)

    code = app.exec()
    server.close()
    QThreadPool.globalInstance().waitForDone(3000)
    return code
