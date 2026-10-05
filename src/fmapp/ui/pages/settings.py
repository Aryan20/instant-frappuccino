"""Settings: tool health/installation, GitHub access, defaults, appearance."""

from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from fmapp.core import autostart, paths
from fmapp.core.engine import LABELS, Provider
from fmapp.core.operations import Operations
from fmapp.core.settings import FRAPPE_BRANCHES
from fmapp.ui import theme
from fmapp.ui.context import AppContext
from fmapp.ui.widgets import (
    Card,
    Pill,
    align_forms,
    button,
    fit_height,
    form_layout,
    label,
    page_header,
    scroll_page,
    tidy_view,
)

TOOLS = (
    ("docker", "Docker", "Runs every bench. Install Docker Desktop, OrbStack or Docker Engine.", ""),
    ("fm", "Frappe Manager", "Creates and manages benches.", "frappe-manager"),
    ("fmd", "Frappe Deployer", "Release-based, zero-downtime deploys on top of fm.", "frappe-deployer"),
    ("uv", "uv", "Installs fm and fmd. https://docs.astral.sh/uv/", ""),
    ("git", "git", "Used by bench to clone apps.", ""),
)


class SettingsPage(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        _scroll, body = scroll_page(outer)
        columns = QHBoxLayout(body)
        columns.setContentsMargins(*theme.PAGE_MARGINS)
        column = QWidget()
        column.setMaximumWidth(920)  # forms read badly stretched across a wide window
        columns.addWidget(column, 1)
        columns.addStretch(0)
        box = QVBoxLayout(column)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(theme.SECTION_GAP)
        header, actions = page_header("Settings")
        actions.addWidget(button("Save", "primary", on_click=self._save))
        box.addWidget(header)

        # tools
        tools = Card(title="Tools")
        self.grid = QGridLayout()
        self.grid.setColumnStretch(2, 1)
        self.grid.setHorizontalSpacing(theme.LG)
        self.grid.setVerticalSpacing(theme.MD)
        self.rows: dict[str, tuple] = {}
        for i, (key, title, help_text, package) in enumerate(TOOLS):
            name = label(f"<b>{title}</b>")
            pill = Pill("checking…", "unknown")
            detail = label(help_text, "muted", wrap=True)
            self.grid.addWidget(name, i, 0)
            self.grid.addWidget(pill, i, 1, alignment=Qt.AlignmentFlag.AlignLeft)
            self.grid.addWidget(detail, i, 2)
            action = None
            if package:
                action = button("Install", on_click=lambda _=False, p=package: self._install(p))
                self.grid.addWidget(action, i, 3)
            self.rows[key] = (pill, detail, action, help_text)
        tools.body.addLayout(self.grid)
        tools.actions.addWidget(button("Re-check", on_click=ctx.tools.refresh))
        box.addWidget(tools)

        s = ctx.settings
        github = Card(title="GitHub")
        github.body.addWidget(
            label(
                "A personal access token lets fm/fmd clone private app repos and raises the GitHub API "
                "limit used for branch lookups. It's stored in your user config dir (mode 600) and "
                "passed to tools via GITHUB_TOKEN — never on the command line.",
                "muted",
                wrap=True,
            )
        )
        form = form_layout()
        self.token = QLineEdit(s.github_token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("ghp_… or github_pat_…")
        form.addRow("Token", self.token)
        github.body.addLayout(form)
        box.addWidget(github)

        startup = Card(title="Engine & startup")
        startup.body.addWidget(
            label(
                "The app drives the Docker engine itself, so Docker Desktop's window never has to open.",
                "muted",
                wrap=True,
            )
        )
        sform = form_layout()
        self.provider = QComboBox()
        for key, title in (
            ("auto", "Detect automatically"),
            *[(p.value, name) for p, name in LABELS.items() if p is not Provider.UNKNOWN],
        ):
            self.provider.addItem(title, key)
        self.provider.setCurrentIndex(max(0, self.provider.findData(s.engine_provider)))
        sform.addRow("Docker provided by", self.provider)
        self.autostart = QComboBox()
        for key, title in (
            ("off", "Off — I'll press Start everything"),
            ("launch", "When Instant Frappuccino opens"),
            ("login", "When I log in (runs in the background)"),
        ):
            self.autostart.addItem(title, key)
        self.autostart.setCurrentIndex(max(0, self.autostart.findData(s.autostart)))
        sform.addRow("Bring everything up", self.autostart)
        self.sites_list = QListWidget()
        tidy_view(self.sites_list)
        sform.addRow("Sites to start", self.sites_list)
        sform.addRow(
            "",
            label(
                "None ticked = start every site. Engine and global services always start.", "muted", wrap=True
            ),
        )
        self.tray = QCheckBox("Show menu-bar / tray icon with quick actions")
        self.tray.setChecked(s.tray)
        self.close_to_tray = QCheckBox("Closing the window keeps Instant Frappuccino running in the tray")
        self.close_to_tray.setChecked(s.close_to_tray)
        self.stop_engine = QCheckBox("“Stop everything” also stops the Docker engine")
        self.stop_engine.setChecked(s.stop_engine_with_everything)
        for widget in (self.tray, self.close_to_tray, self.stop_engine):
            sform.addRow("", widget)
        startup.body.addLayout(sform)
        box.addWidget(startup)
        ctx.benches.changed.connect(self._render_sites)

        defaults = Card(title="Defaults")
        dform = form_layout()
        self.branch = QComboBox()
        self.branch.setEditable(True)
        self.branch.addItems(FRAPPE_BRANCHES)
        self.branch.setCurrentText(s.default_frappe_branch)
        dform.addRow("Frappe version", self.branch)
        self.admin = QLineEdit(s.default_admin_password)
        self.admin.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        dform.addRow("Administrator password", self.admin)
        self.refresh = QSpinBox()
        self.refresh.setRange(3, 300)
        self.refresh.setSuffix(" s")
        self.refresh.setValue(s.refresh_seconds)
        dform.addRow("Status refresh", self.refresh)
        defaults.body.addLayout(dform)
        box.addWidget(defaults)

        advanced = Card(title="Paths")
        aform = form_layout()
        self.fm_home = QLineEdit(s.fm_home)
        self.fm_home.setPlaceholderText(str(Path.home() / "frappe"))
        aform.addRow("FM home", self.fm_home)
        self.fm_path = QLineEdit(s.fm_path, placeholderText="auto-detect")
        self.fmd_path = QLineEdit(s.fmd_path, placeholderText="auto-detect")
        self.docker_path = QLineEdit(s.docker_path, placeholderText="auto-detect")
        aform.addRow("fm binary", self.fm_path)
        aform.addRow("fmd binary", self.fmd_path)
        aform.addRow("docker binary", self.docker_path)
        aform.addRow("App config", label(str(paths.config_dir()), "mono"))
        advanced.body.addLayout(aform)
        box.addWidget(advanced)

        look = Card(title="Appearance")
        lform = form_layout()
        self.theme = QComboBox()
        self.theme.addItems(["system", "light", "dark"])
        self.theme.setCurrentText(s.theme)
        lform.addRow("Theme", self.theme)
        look.body.addLayout(lform)
        box.addWidget(look)
        box.addStretch()

        self.saved = label("", "muted")
        box.addWidget(self.saved)
        align_forms(form, sform, dform, aform, lform)
        ctx.tools.changed.connect(self._render_tools)
        self._render_tools()

    def _render_sites(self) -> None:
        chosen = set(self.ctx.settings.autostart_sites)
        if self.sites_list.count():  # keep the user's unsaved ticks
            chosen = {
                self.sites_list.item(i).text()
                for i in range(self.sites_list.count())
                if self.sites_list.item(i).checkState() == Qt.CheckState.Checked
            }
        self.sites_list.clear()
        for bench in self.ctx.benches.benches:
            item = QListWidgetItem(bench.name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if bench.name in chosen else Qt.CheckState.Unchecked)
            self.sites_list.addItem(item)
        fit_height(self.sites_list, self.sites_list.count(), max_rows=6)

    def _render_tools(self) -> None:
        for key, (pill, detail, action, help_text) in self.rows.items():
            status = self.ctx.tools.tools.get(key)
            if status is None:
                continue
            if status.ok:
                version = re.search(r"\d+\.\d+[\w.+-]*", status.version)
                pill.set(version.group(0) if version else "ok", "running")
                detail.setText(f"{help_text}  <span style='opacity:.7'>{status.path}</span>")
            else:
                pill.set("missing" if not status.path else "error", "broken")
                detail.setText(f"{help_text}  {status.detail}")
            if action:
                action.setText("Update" if status.ok else "Install")
                action.setEnabled(self.ctx.tools.ok("uv"))

    def _install(self, package: str) -> None:
        run = self.ctx.submit(self, lambda _ops: Operations.install_tool(package), show=True)
        if run:
            run.finished.connect(self.ctx.tools.refresh)

    def _save(self) -> None:
        s = self.ctx.settings
        s.github_token = self.token.text().strip()
        s.default_frappe_branch = self.branch.currentText().strip() or "version-15"
        s.default_admin_password = self.admin.text() or "admin"
        s.refresh_seconds = self.refresh.value()
        s.fm_home = self.fm_home.text().strip()
        s.fm_path = self.fm_path.text().strip()
        s.fmd_path = self.fmd_path.text().strip()
        s.docker_path = self.docker_path.text().strip()
        s.theme = self.theme.currentText()
        theme.apply(QApplication.instance(), s.theme)
        s.engine_provider = self.provider.currentData()
        s.autostart = self.autostart.currentData()
        s.autostart_sites = [
            self.sites_list.item(i).text()
            for i in range(self.sites_list.count())
            if self.sites_list.item(i).checkState() == Qt.CheckState.Checked
        ]
        s.tray = self.tray.isChecked()
        s.close_to_tray = self.close_to_tray.isChecked()
        s.stop_engine_with_everything = self.stop_engine.isChecked()
        s.save()
        try:
            autostart.sync(s.autostart)
        except OSError as exc:
            QMessageBox.warning(self, "Login item", f"Couldn't update the login item: {exc}")
        self.ctx.benches.timer.setInterval(s.refresh_seconds * 1000)
        self.ctx.system.timer.setInterval(s.refresh_seconds * 1000)
        self.ctx.tools.refresh()
        self.ctx.benches.refresh()
        self.saved.setText("Saved.")
