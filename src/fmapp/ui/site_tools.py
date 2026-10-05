"""Everyday site actions shared by the site page, the Sites list menu and the tray."""

from __future__ import annotations

import subprocess
import sys

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from fmapp.core.env import tool_env
from fmapp.core.operations import MissingTool, Operations
from fmapp.core.terminal import terminal_argv
from fmapp.ui import theme
from fmapp.ui.async_ import run_async
from fmapp.ui.context import AppContext
from fmapp.ui.widgets import button, form_layout, label, run_dialog

# Handy starting points for "Run command…"; {site} is replaced with the site name.
COMMAND_TEMPLATES = (
    ("List installed apps", "bench --site {site} list-apps"),
    ("Show pending background jobs", "bench --site {site} show-pending-jobs"),
    ("Scheduler / worker health", "bench --site {site} doctor"),
    ("Set a site config value", "bench --site {site} set-config KEY VALUE"),
    ("Run a Python function", "bench --site {site} execute app.module.function"),
    ("Reload a DocType", "bench --site {site} reload-doctype DocType"),
    ("Export fixtures", "bench --site {site} export-fixtures"),
    ("Add a System Manager", "bench --site {site} add-system-manager user@example.com"),
    ("Rebuild global search", "bench --site {site} rebuild-global-search"),
    ("Bench version", "bench version"),
)


def open_in_browser(url: str) -> bool:
    """Open a URL in the default browser; fall back to the OS opener if Qt's hand-off fails."""
    if QDesktopServices.openUrl(QUrl(url)):
        return True
    opener = ["/usr/bin/open"] if sys.platform == "darwin" else ["xdg-open"]
    try:
        return subprocess.run([*opener, url], capture_output=True, timeout=15, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def login_as_admin(
    ctx: AppContext, parent: QWidget | None, site: str, trigger: QPushButton | None = None
) -> None:
    """Mint a one-time Administrator session and open the desk with it — no password needed."""
    try:
        argv = ctx.ops.login_url_argv(site)
    except MissingTool as exc:
        QMessageBox.warning(parent, "Log in as Admin", str(exc))
        return
    label_before = trigger.text() if trigger else ""
    if trigger:
        trigger.setEnabled(False)
        trigger.setText("Logging in…")
    ctx.notify.emit(f"Logging in to {site} as Administrator…", 0)

    def restore() -> None:
        if trigger:
            trigger.setText(label_before)
            trigger.setEnabled(True)

    def work() -> tuple[str | None, str]:
        # Run outside the job log on purpose: the URL carries a live session id.
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=120,
            env=tool_env(),
            stdin=subprocess.DEVNULL,
            check=False,
        )
        out = proc.stdout + proc.stderr
        return Operations.parse_login_url(out), out

    def done(result: tuple[str | None, str]) -> None:
        restore()
        url, out = result
        if not url:
            ctx.notify.emit("", 0)
            tail = "\n".join(out.strip().splitlines()[-6:]) or "No output."
            QMessageBox.warning(
                parent, "Couldn't log in", f"bench browse didn't return a login link:\n\n{tail}"
            )
            return
        if open_in_browser(url):
            ctx.notify.emit(f"Opened {site} as Administrator in your browser", 6000)
            return
        ctx.notify.emit("", 0)
        box = QMessageBox(
            QMessageBox.Icon.Information,
            "Open this link",
            "Your browser didn't open automatically. This one-time link logs you in as Administrator:",
            parent=parent,
        )
        box.setDetailedText(url)
        copy = box.addButton("Copy link", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is copy:
            QApplication.clipboard().setText(url)

    def failed(message: str) -> None:
        restore()
        ctx.notify.emit("", 0)
        QMessageBox.warning(parent, "Couldn't log in", message)

    run_async(work, done, failed)


def open_terminal(ctx: AppContext, parent: QWidget | None, site: str, console: bool = False) -> None:
    try:
        command = ctx.ops.shell_command(site, console)
    except MissingTool as exc:
        QMessageBox.warning(parent, "Terminal", str(exc))
        return
    what = "bench console" if console else "shell"
    argv = terminal_argv(command, name=f"{site}-{'console' if console else 'shell'}")
    problem = ""
    if not argv:
        problem = "No terminal app was found."
    else:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
            if proc.returncode != 0:
                problem = (proc.stderr or proc.stdout).strip() or f"exit status {proc.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            problem = str(exc)
    if problem:
        QMessageBox.warning(
            parent,
            "Couldn't open a terminal",
            f"{problem}\n\nRun this yourself:\n\n{' '.join(command)}",
        )
    else:
        ctx.notify.emit(f"Opened a {what} for {site} in your terminal", 6000)


class RunCommandDialog(QDialog):
    def __init__(self, ctx: AppContext, site: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx, self.site = ctx, site
        self.setWindowTitle(f"Run command · {site}")
        self.setMinimumWidth(640)
        box = QVBoxLayout(self)
        box.setContentsMargins(theme.XL, theme.XL, theme.XL, theme.LG)
        box.setSpacing(theme.MD)
        box.addWidget(label(f"Run a command in {site}", "h2"))
        box.addWidget(
            label(
                "Runs inside the bench container from /workspace/frappe-bench. Output streams to Activity.",
                "muted",
                wrap=True,
            )
        )
        form = form_layout()
        self.template = QComboBox()
        self.template.addItem("Recent & templates…", "")
        for command in ctx.settings.command_history:
            self.template.addItem(f"↺  {command}", command)
        for title, command in COMMAND_TEMPLATES:
            self.template.addItem(title, command.format(site=site))
        self.template.activated.connect(self._pick)
        form.addRow("Start from", self.template)
        self.command = QLineEdit(f"bench --site {site} ")
        self.command.setProperty("role", "mono")
        form.addRow("Command", self.command)
        box.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        run = button("Run", "primary", on_click=self._run)
        run.setDefault(True)
        buttons.addButton(run, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        self.command.setFocus()
        self.command.end(False)

    def _pick(self, index: int) -> None:
        command = self.template.itemData(index)
        if command:
            self.command.setText(command)
            # Select the placeholder part (e.g. KEY VALUE) so typing replaces it.
            for placeholder in ("KEY VALUE", "app.module.function", "DocType", "user@example.com"):
                start = command.find(placeholder)
                if start >= 0:
                    self.command.setSelection(start, len(placeholder))
                    break
            self.command.setFocus()

    def _run(self) -> None:
        command = self.command.text().strip()
        if self.ctx.submit(self, lambda ops: ops.run_command(self.site, command), show=True):
            self.ctx.settings.remember_command(command)
            self.ctx.settings.save()
            self.accept()


class AdminPasswordDialog(QDialog):
    def __init__(self, ctx: AppContext, site: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx, self.site = ctx, site
        self.setWindowTitle(f"Administrator password · {site}")
        self.setMinimumWidth(460)
        box = QVBoxLayout(self)
        box.setContentsMargins(theme.XL, theme.XL, theme.XL, theme.LG)
        box.setSpacing(theme.MD)
        box.addWidget(label("Set the Administrator password", "h2"))
        form = form_layout()
        self.password = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        self.confirm = QLineEdit(echoMode=QLineEdit.EchoMode.Password)
        form.addRow("New password", self.password)
        form.addRow("Confirm", self.confirm)
        box.addLayout(form)
        self.error = label("", wrap=True)
        self.error.setProperty("role", "error")
        box.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton(
            button("Set password", "primary", on_click=self._save), QDialogButtonBox.ButtonRole.AcceptRole
        )
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

    def _save(self) -> None:
        if self.password.text() != self.confirm.text():
            self.error.setText("The passwords don't match.")
            return
        password = self.password.text()
        if self.ctx.submit(self, lambda ops: ops.set_admin_password(self.site, password)):
            self.accept()


def add_tool_actions(ctx: AppContext, parent: QWidget, site: str, menu: QMenu, running: bool) -> None:
    """Everyday tools for a running site, appended to a context / overflow menu."""
    menu.addAction("Log in as Administrator", lambda: login_as_admin(ctx, parent, site)).setEnabled(running)
    menu.addAction("Open in VS Code", lambda: ctx.submit(parent, lambda ops: ops.open_code(site)))
    menu.addAction("Terminal (bench shell)", lambda: open_terminal(ctx, parent, site)).setEnabled(running)
    menu.addAction("Bench console", lambda: open_terminal(ctx, parent, site, console=True)).setEnabled(
        running
    )
    menu.addAction("Run command…", lambda: run_dialog(RunCommandDialog(ctx, site, parent))).setEnabled(
        running
    )
    cache = menu.addMenu("Clear cache")
    cache.setEnabled(running)
    add_cache_actions(ctx, parent, site, cache)


def add_cache_actions(ctx: AppContext, parent: QWidget | None, site: str, menu: QMenu) -> None:
    menu.addAction("Site + website cache", lambda: ctx.submit(parent, lambda ops: ops.clear_cache(site)))
    menu.addAction("Site cache only", lambda: ctx.submit(parent, lambda ops: ops.clear_cache(site, "site")))
    menu.addAction(
        "Website cache only", lambda: ctx.submit(parent, lambda ops: ops.clear_cache(site, "website"))
    )
