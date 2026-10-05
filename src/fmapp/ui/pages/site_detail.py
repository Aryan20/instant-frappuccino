"""A single site: overview, apps, releases (Deployer) and live logs."""

from __future__ import annotations

import subprocess

from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QMenu,
    QMessageBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fmapp.core import benches as benches_io
from fmapp.core import deployer, paths
from fmapp.core.env import tool_env
from fmapp.core.info import InfoRow, parse_info
from fmapp.core.models import Bench, BenchKind, BenchStatus
from fmapp.ui import theme
from fmapp.ui.async_ import run_async
from fmapp.ui.context import AppContext
from fmapp.ui.dialogs.build import BuildDialog
from fmapp.ui.dialogs.simple import AddAppsDialog, ConfirmRestoreDialog
from fmapp.ui.jobs import StreamProcess
from fmapp.ui.pages.sites import confirm_delete, site_actions
from fmapp.ui.pages.system import (
    container_menu,
    fill_container_tree,
    make_container_tree,
    open_container_logs,
)
from fmapp.ui.site_tools import (
    AdminPasswordDialog,
    RunCommandDialog,
    add_cache_actions,
    login_as_admin,
    open_in_browser,
    open_terminal,
)
from fmapp.ui.widgets import (
    Banner,
    Card,
    LogView,
    Pill,
    button,
    form_layout,
    hline,
    label,
    pick_fmd_config,
    run_dialog,
    scroll_page,
    tidy_view,
)

LOG_SERVICES = ("", "frappe", "nginx", "socketio", "schedule", "redis-cache", "redis-queue")


class SiteDetailPage(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.name = ""
        box = QVBoxLayout(self)
        box.setContentsMargins(*theme.PAGE_MARGINS)
        box.setSpacing(0)

        back = button("‹  All sites", "ghost", on_click=lambda: ctx.navigate.emit("sites"))
        box.addWidget(back, alignment=Qt.AlignmentFlag.AlignLeft)
        box.addSpacing(theme.SM)

        title_row = QHBoxLayout()
        title_row.setSpacing(theme.SM + 2)
        self.title = label("", "h1")
        self.status = Pill()
        self.kind = Pill()
        title_row.addWidget(self.title)
        title_row.addWidget(self.status)
        title_row.addWidget(self.kind)
        title_row.addStretch()
        self.open_btn = button("Open site", on_click=lambda: open_in_browser(self.bench.url))
        self.admin_btn = button(
            "Log in as Admin",
            on_click=lambda: login_as_admin(self.ctx, self, self.name, self.admin_btn),
            tooltip="Opens the desk logged in as Administrator — no password needed",
        )
        self.toggle_btn = button("Start", "primary", on_click=self._toggle)
        self.more_btn = button("More  ▾", on_click=self._more)
        for widget in (self.open_btn, self.admin_btn, self.toggle_btn, self.more_btn):
            title_row.addWidget(widget)
        box.addLayout(title_row)
        box.addSpacing(theme.XS)
        self.subtitle = label("", "muted")
        self.subtitle.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self.subtitle.setOpenExternalLinks(True)
        box.addWidget(self.subtitle)
        box.addSpacing(theme.SECTION_GAP)

        self.banner = Banner("warn")
        self.banner.hide()
        box.addWidget(self.banner)
        self.banner.installEventFilter(self)  # adds breathing room only while visible
        self._banner_space = QWidget()
        self._banner_space.setFixedHeight(theme.LG)
        self._banner_space.hide()
        box.addWidget(self._banner_space)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._overview_tab(), "Overview")
        self.tabs.addTab(self._apps_tab(), "Apps")
        self.tabs.addTab(self._containers_tab(), "Containers")
        self.tabs.addTab(self._backups_tab(), "Backups")
        self.tabs.addTab(self._releases_tab(), "Releases")
        self.tabs.addTab(self._logs_tab(), "Logs")
        box.addWidget(self.tabs, 1)

        ctx.benches.changed.connect(self.render)
        # (site, flag) -> value the user asked for, while its job is still running
        self._pending_flags: dict[tuple[str, str], bool] = {}
        ctx.system.changed.connect(self._render_containers)
        self._info_cache: dict[str, list[InfoRow]] = {}
        ctx.jobs.changed.connect(self._update_busy)

    def eventFilter(self, obj, event) -> bool:
        if obj is self.banner and event.type() in (QEvent.Type.Show, QEvent.Type.Hide):
            self._banner_space.setVisible(event.type() == QEvent.Type.Show)
        return super().eventFilter(obj, event)

    # -- data -----------------------------------------------------------------------------
    @property
    def bench(self) -> Bench:
        return self.ctx.benches.get(self.name) or Bench(self.name, paths.benches_dir() / self.name)

    def show_site(self, name: str) -> None:
        if name != self.name:
            self.logs.stop()
            self.log_view.clear()
            self.tabs.setCurrentIndex(0)
        self.name = name
        self._render_info(self._info_cache.get(name))
        self.render()

    # -- tabs -----------------------------------------------------------------------------
    def _overview_tab(self) -> QWidget:
        scroll, page = scroll_page()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, theme.SECTION_GAP, theme.XS, theme.XS)
        outer.setSpacing(theme.LG)
        box = QHBoxLayout()
        box.setSpacing(theme.LG)
        outer.addLayout(box)
        left = QVBoxLayout()
        left.setSpacing(theme.LG)
        info = Card(title="Configuration")
        self.info_form = form_layout()
        info.body.addLayout(self.info_form)
        left.addWidget(info)

        creds = Card(
            title="Site info & credentials",
            subtitle="Logins for Administrator, the database, Mailpit and Adminer",
        )
        crow = creds.actions
        self.info_btn = button(
            "Load fm info",
            on_click=self._load_info,
            tooltip="Runs `fm info` — Frappe, database and Mailpit/Adminer logins",
        )
        crow.addWidget(self.info_btn)
        self.creds_hint = label(
            "Loaded on demand from `fm info`, so passwords never sit on screen until you ask.",
            "faint",
            wrap=True,
        )
        creds.body.addWidget(self.creds_hint)
        self.creds_grid = QGridLayout()
        self.creds_grid.setColumnStretch(1, 1)
        self.creds_grid.setHorizontalSpacing(theme.LG)
        self.creds_grid.setVerticalSpacing(theme.SM)
        creds.body.addLayout(self.creds_grid)
        left.addStretch()
        box.addLayout(left, 3)

        toggles = Card(title="Settings")
        self.dev_mode = QCheckBox("Developer mode")
        self.dev_mode.clicked.connect(
            lambda on: self._switch_flag(
                "developer_mode",
                on,
                lambda ops: ops.update(self.name, developer_mode="enable" if on else "disable"),
            )
        )
        self.admin_tools = QCheckBox("Admin tools (Mailpit, Adminer)")
        self.admin_tools.clicked.connect(
            lambda on: self._switch_flag(
                "admin_tools",
                on,
                lambda ops: ops.update(self.name, admin_tools="enable" if on else "disable"),
            )
        )
        toggles.body.addWidget(self.dev_mode)
        toggles.body.addWidget(self.admin_tools)
        env_row = QHBoxLayout()
        env_row.addWidget(label("Environment"))
        self.env_combo = QComboBox()
        self.env_combo.addItems(["dev", "prod"])
        self.env_combo.activated.connect(lambda _i: self._update(environment=self.env_combo.currentText()))
        env_row.addWidget(self.env_combo)
        env_row.addStretch()
        toggles.body.addLayout(env_row)
        self.maintenance_box = QCheckBox("Maintenance mode (visitors see a maintenance page)")
        self.maintenance_box.clicked.connect(
            lambda on: self._switch_flag("maintenance", on, lambda ops: ops.maintenance(self.name, on))
        )
        self.scheduler_box = QCheckBox("Pause scheduler (no background jobs run)")
        self.scheduler_box.clicked.connect(
            lambda on: self._switch_flag("scheduler", on, lambda ops: ops.scheduler(self.name, on))
        )
        toggles.body.addWidget(self.maintenance_box)
        toggles.body.addWidget(self.scheduler_box)
        toggles.body.addWidget(
            button("Administrator password…", on_click=self._admin_password),
            alignment=Qt.AlignmentFlag.AlignLeft,
        )

        toggles.body.addSpacing(theme.SM)
        toggles.body.addWidget(hline())
        toggles.body.addSpacing(theme.XS)
        toggles.body.addWidget(label("Restart", "h3"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.SM)
        grid.setVerticalSpacing(theme.SM)
        for i, (part, title, tip) in enumerate(
            (
                ("web", "Web", "Frappe server + socketio"),
                ("workers", "Workers", "Scheduler and background workers"),
                ("redis", "Redis", "redis-cache and redis-queue"),
                ("nginx", "Nginx", "The bench's own nginx"),
                ("containers", "All containers", "Stop and start every container of this bench"),
                ("all", "Everything", "Web, workers, redis and nginx"),
            )
        ):
            grid.addWidget(
                button(
                    title,
                    tooltip=tip,
                    on_click=lambda _=False, p=part: self._run(lambda ops: ops.restart_parts(self.name, p)),
                ),
                i // 3,
                i % 3,
            )
        toggles.body.addLayout(grid)

        toggles.body.addSpacing(theme.SM)
        toggles.body.addWidget(label("Maintenance", "h3"))
        clear_cache = button("Clear cache  ▾")
        clear_cache.clicked.connect(self._cache_menu)
        self._clear_cache_btn = clear_cache
        maintenance = (
            button(
                "Repair",
                on_click=self._repair,
                tooltip="Site won't load? Starts the engine and global services, reloads "
                "the proxy and recreates this bench's containers.",
            ),
            button("Migrate", on_click=lambda: self._run(lambda ops: ops.migrate(self.name))),
            button(
                "Build…", on_click=lambda: self._build(), tooltip="bench build — whole bench or chosen apps"
            ),
            clear_cache,
            button("Backup now", on_click=lambda: self._run(lambda ops: ops.backup(self.name))),
            button("Run command…", on_click=lambda: run_dialog(RunCommandDialog(self.ctx, self.name, self))),
        )
        toggles.body.addLayout(_grid(maintenance))
        toggles.body.addSpacing(theme.SM)
        toggles.body.addWidget(label("Tools", "h3"))
        tools = (
            button(
                "VS Code",
                on_click=lambda: self._run(lambda ops: ops.open_code(self.name)),
                tooltip="fm code — opens the bench inside the container in VS Code",
            ),
            button(
                "Terminal",
                on_click=lambda: open_terminal(self.ctx, self, self.name),
                tooltip="A shell inside the bench container",
            ),
            button(
                "Console",
                on_click=lambda: open_terminal(self.ctx, self, self.name, console=True),
                tooltip="bench console (IPython with Frappe loaded)",
            ),
        )
        toggles.body.addLayout(_grid(tools))
        toggles.body.addStretch()
        box.addWidget(toggles, 2)
        outer.addWidget(creds)
        danger = Card(
            title="Delete site",
            subtitle="Removes the bench, its containers and code. You can keep or drop its database.",
        )
        danger.actions.addWidget(button("Delete site…", "danger", on_click=self._delete))
        outer.addWidget(danger)
        outer.addStretch()
        return scroll

    def _containers_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, theme.SECTION_GAP, 0, 0)
        box.setSpacing(theme.MD)
        row = QHBoxLayout()
        row.addWidget(
            label("Every container of this bench. Right-click for restart, stop and logs.", "muted"), 1
        )
        for title, action in (("Start", "start"), ("Restart", "restart"), ("Stop", "stop")):
            row.addWidget(button(title, on_click=lambda _=False, a=action: self._container_action(a)))
        row.addWidget(button("Logs", on_click=self._container_logs))
        box.addLayout(row)
        self.containers_tree = make_container_tree()
        self.containers_tree.customContextMenuRequested.connect(self._container_menu)
        box.addWidget(self.containers_tree, 1)
        return page

    def _apps_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, theme.SECTION_GAP, 0, 0)
        box.setSpacing(theme.MD)
        row = QHBoxLayout()
        self.apps_note = label("", "muted", wrap=True)
        row.addWidget(self.apps_note, 1)
        self.build_apps_btn = button(
            "Build selected",
            on_click=lambda: self._build(self._selected_apps()),
            tooltip="Rebuild assets for the selected apps (⌘/Ctrl-click to pick several)",
        )
        row.addWidget(self.build_apps_btn)
        self.pull_btn = button(
            "Pull latest",
            on_click=self._pull,
            tooltip="git pull the selected apps, then migrate and rebuild them",
        )
        row.addWidget(self.pull_btn)
        self.tests_btn = button("Run tests", on_click=self._tests, tooltip="bench run-tests for the app")
        row.addWidget(self.tests_btn)
        self.remove_app_btn = button("Remove app", on_click=self._remove_app)
        row.addWidget(self.remove_app_btn)
        row.addWidget(button("Add apps…", "primary", on_click=self._add_apps))
        box.addLayout(row)
        self.apps_tree = QTreeWidget()
        self.apps_tree.setHeaderLabels(["App", "Version", "Branch", "Commit", "Source"])
        self.apps_tree.setRootIsDecorated(False)
        self.apps_tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.apps_tree.itemSelectionChanged.connect(self._sync_app_buttons)
        tidy_view(self.apps_tree)
        header = self.apps_tree.header()
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for col in range(4):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        box.addWidget(self.apps_tree, 1)
        return page

    def _backups_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, theme.SECTION_GAP, 0, 0)
        box.setSpacing(theme.MD)
        row = QHBoxLayout()
        row.setSpacing(theme.SM)
        self.backups_note = label("", "muted", wrap=True)
        row.addWidget(self.backups_note, 1)
        row.addWidget(button("Show in folder", on_click=self._open_backups))
        self.restore_btn = button("Restore…", on_click=self._restore)
        row.addWidget(self.restore_btn)
        row.addWidget(button("Back up now", "primary", on_click=self._backup_now))
        box.addLayout(row)
        self.backups_tree = QTreeWidget()
        self.backups_tree.setHeaderLabels(["Taken", "Database", "Files", "Size"])
        self.backups_tree.setRootIsDecorated(False)
        tidy_view(self.backups_tree)
        header = self.backups_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.backups_tree.itemSelectionChanged.connect(
            lambda: self.restore_btn.setEnabled(bool(self.backups_tree.selectedItems()))
        )
        self.backups_tree.itemDoubleClicked.connect(lambda *_: self._restore())
        box.addWidget(self.backups_tree, 1)
        return page

    def _render_backups(self, bench: Bench) -> None:
        self._backups = benches_io.list_backups(bench.backups_dir)
        keep = self.backups_tree.currentItem().text(0) if self.backups_tree.currentItem() else None
        self.backups_tree.clear()
        for backup in self._backups:
            when = backup.created.strftime("%Y-%m-%d  %H:%M:%S") if backup.created else backup.stamp
            parts = (("public", backup.public_files), ("private", backup.private_files))
            files = ", ".join(name for name, path in parts if path)
            item = QTreeWidgetItem(
                self.backups_tree, [when, backup.database.name, files or "—", _human(backup.size)]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, backup.stamp)
            if when == keep:
                self.backups_tree.setCurrentItem(item)
        self.backups_note.setText(
            f"{len(self._backups)} backup{'s' if len(self._backups) != 1 else ''} stored inside the bench"
            if self._backups
            else "No backups yet. Back up now saves the database (and files) inside the bench."
        )
        self.restore_btn.setEnabled(bool(self.backups_tree.selectedItems()))

    def _open_backups(self) -> None:
        folder = self.bench.backups_dir
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder if folder.exists() else self.bench.site_dir)))

    def _backup_now(self) -> None:
        run = self.ctx.submit(self, lambda ops: ops.backup(self.name))
        if run:
            run.finished.connect(self.render)

    def _restore(self) -> None:
        item = self.backups_tree.currentItem()
        stamp = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        backup = next((b for b in self._backups if b.stamp == stamp), None)
        if not backup:
            return
        dialog = ConfirmRestoreDialog(self.name, backup, self)
        if run_dialog(dialog):
            files = dialog.with_files.isChecked()
            self.ctx.submit(self, lambda ops: ops.restore(self.bench, backup, files), show=True)

    def _releases_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, theme.SECTION_GAP, 0, 0)
        box.setSpacing(theme.MD)

        self.convert_card = Card()
        self.convert_card.body.addWidget(label("Release-based deploys", "h2"))
        self.convert_card.body.addWidget(
            label(
                "Hand this site to Frappe Deployer: every deploy builds a fresh, immutable release "
                "from pinned app branches and switches atomically, with backups and one-click "
                "rollback — Frappe Cloud's deploy model, locally. Your current apps and branches "
                "become the deploy config.",
                wrap=True,
            )
        )
        self.convert_card.body.addWidget(
            button("Enable release deploys", "primary", on_click=self._convert),
            alignment=Qt.AlignmentFlag.AlignLeft,
        )
        box.addWidget(self.convert_card)

        self.releases_box = QWidget()
        rbox = QVBoxLayout(self.releases_box)
        rbox.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        self.release_note = label("", "muted", wrap=True)
        actions.addWidget(self.release_note, 1)
        actions.addWidget(
            button(
                "Clean up old",
                on_click=lambda: self._run(
                    lambda ops: ops.cleanup_releases(self.name),
                    confirm="Delete all but the 3 newest releases?",
                ),
            )
        )
        self.switch_btn = button("Switch to selected", on_click=self._switch)
        actions.addWidget(self.switch_btn)
        actions.addWidget(
            button(
                "Import fmd config…",
                on_click=self._import_config,
                tooltip="Replace this site's deploy config with a site.toml and deploy",
            )
        )
        actions.addWidget(button("Deploy now", "primary", on_click=self._deploy))
        rbox.addLayout(actions)
        self.releases_tree = QTreeWidget()
        self.releases_tree.setHeaderLabels(["Release", "Created", "Status"])
        self.releases_tree.setRootIsDecorated(False)
        self.releases_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        rbox.addWidget(self.releases_tree, 1)
        self.deploy_apps = label("", "muted", wrap=True)
        self.deploy_apps.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        rbox.addWidget(self.deploy_apps)
        box.addWidget(self.releases_box, 1)
        return page

    def _logs_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, theme.SECTION_GAP, 0, 0)
        box.setSpacing(theme.MD)
        row = QHBoxLayout()
        row.addWidget(label("Service"))
        self.log_service = QComboBox()
        for service in LOG_SERVICES:
            self.log_service.addItem(service or "bench (server logs)", service)
        row.addWidget(self.log_service)
        row.addStretch()
        self.log_btn = button("Follow logs", "primary", on_click=self._toggle_logs)
        row.addWidget(button("Clear", on_click=lambda: self.log_view.clear()))
        row.addWidget(self.log_btn)
        box.addLayout(row)
        self.log_view = LogView()
        box.addWidget(self.log_view, 1)
        self.logs = StreamProcess(self)
        self.logs.output.connect(self.log_view.append_text)
        self.logs.stopped.connect(lambda: self.log_btn.setText("Follow logs"))
        return page

    # -- render ---------------------------------------------------------------------------
    def render(self) -> None:
        if not self.name:
            return
        if not self.isVisible():
            self._stale = True  # re-rendered by showEvent
            return
        self._stale = False
        bench = self.ctx.benches.get(self.name)
        if bench is None:
            if self.ctx.benches.loaded:
                self.ctx.navigate.emit("sites")  # deleted
            return
        running = bench.status in (BenchStatus.RUNNING, BenchStatus.PARTIAL)
        self.title.setText(bench.name)
        self.status.set(bench.status.value)
        is_fmd = bench.kind is BenchKind.DEPLOYER
        self.kind.set("Deployer" if is_fmd else "Frappe Manager", bench.kind.value)
        self.subtitle.setText(f'<a href="{bench.url}">{bench.url}</a> · {bench.path}')
        self.open_btn.setEnabled(running)
        self.admin_btn.setEnabled(running)
        self._render_backups(bench)
        self.toggle_btn.setText("Stop" if running else "Start")
        self.toggle_btn.setProperty("kind", "" if running else "primary")
        self.toggle_btn.style().unpolish(self.toggle_btn)
        self.toggle_btn.style().polish(self.toggle_btn)
        if bench.error:
            self.banner.show_message(bench.error)
        else:
            self.banner.hide()

        # overview
        while self.info_form.rowCount():
            self.info_form.removeRow(0)
        cfg = bench.config
        rows = [
            ("Frappe", bench.frappe_version or "—"),
            ("Environment", bench.environment),
            ("Python", str(cfg.get("python_version", "auto"))),
            ("Node", str(cfg.get("node_version", "auto"))),
            ("Database", str(cfg.get("db_name", "—"))),
            ("Upload limit", str(cfg.get("upload_limit", "—"))),
            ("Alias domains", ", ".join(bench.alias_domains) or "—"),
            ("Admin login", "Administrator — password under Site info & credentials"),
        ]
        if bench.admin_tools:
            rows.append(
                (
                    "Admin tools",
                    f'<a href="{bench.url}/mailpit/">Mailpit</a> · '
                    f'<a href="{bench.url}/adminer/">Adminer</a>',
                )
            )
        for key, value in rows:
            value_label = label(value)
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            value_label.setOpenExternalLinks(True)
            self.info_form.addRow(label(key, "muted"), value_label)
        self._render_flags(bench)
        self.env_combo.setCurrentText(bench.environment)

        # apps (keep the selection across the periodic refresh)
        keep = set(self._selected_apps())
        self.apps_tree.blockSignals(True)
        self.apps_tree.clear()
        for app in bench.apps:
            item = QTreeWidgetItem(
                self.apps_tree,
                [app.name, app.version, app.branch or "(detached)", app.short_commit, app.remote],
            )
            item.setSelected(app.name in keep)
        self.apps_tree.blockSignals(False)
        self._sync_app_buttons()
        self.apps_note.setText(
            "Changes create a new release (built in the background, then switched atomically)."
            if is_fmd
            else "Apps installed in this bench. New apps are fetched and installed into the site."
        )

        # releases
        self.convert_card.setVisible(not is_fmd)
        self.releases_box.setVisible(is_fmd)
        if is_fmd:
            current = self.releases_tree.currentItem()
            keep_release = current.data(0, Qt.ItemDataRole.UserRole) if current else None
            self.releases_tree.clear()
            for release in bench.releases:
                created = release.created.strftime("%Y-%m-%d %H:%M:%S") if release.created else ""
                item = QTreeWidgetItem(
                    self.releases_tree, [release.name, created, "● active" if release.active else ""]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, release.name)
                if release.name == keep_release:
                    self.releases_tree.setCurrentItem(item)
            config = deployer.load(bench.name)
            apps = deployer.apps_of(config)
            self.deploy_apps.setText(
                "Deploy config: "
                + ", ".join(a.display() for a in apps)
                + f"\n{deployer.config_path(bench.name)}"
                if apps
                else "No deploy config yet — Deploy now will derive one from the installed apps."
            )
            self.release_note.setText(f"{len(bench.releases)} releases on disk")
        self._update_busy()

    def _update_busy(self) -> None:
        busy = self.name in self.ctx.jobs.busy_benches()
        self.toggle_btn.setEnabled(not busy)
        if busy:
            self.status.set("working…", "queued")

    # -- actions --------------------------------------------------------------------------
    def _run(self, build, confirm: str = "") -> None:
        if confirm and QMessageBox.question(self, "Confirm", confirm) != QMessageBox.StandardButton.Yes:
            return
        self.ctx.submit(self, build)

    def _update(self, **flags: str) -> None:
        self._run(lambda ops: ops.update(self.name, **flags))

    def _toggle(self) -> None:
        bench = self.bench
        if bench.status in (BenchStatus.RUNNING, BenchStatus.PARTIAL):
            self._run(lambda ops: ops.stop(bench.name))
        else:
            self._run(lambda ops: ops.start(bench.name))

    def _more(self) -> None:
        menu = QMenu(self)
        site_actions(self.ctx, self, self.bench, menu)
        menu.exec(self.more_btn.mapToGlobal(self.more_btn.rect().bottomLeft()))

    def _delete(self) -> None:
        confirm_delete(self.ctx, self, self.bench)

    def _add_apps(self) -> None:
        run_dialog(AddAppsDialog(self.ctx, self.bench, parent=self))

    def _selected_apps(self) -> list[str]:
        return [item.text(0) for item in self.apps_tree.selectedItems()]

    def _sync_app_buttons(self) -> None:
        count = len(self.apps_tree.selectedItems())
        self.build_apps_btn.setEnabled(count > 0)
        self.build_apps_btn.setText(f"Build {count} apps" if count > 1 else "Build selected")
        self.remove_app_btn.setEnabled(count == 1)
        self.pull_btn.setEnabled(count > 0)
        self.tests_btn.setEnabled(count == 1)

    def _pull(self) -> None:
        apps = self._selected_apps()
        if not apps:
            return
        box = QMessageBox(
            QMessageBox.Icon.Question,
            "Pull latest",
            f"git pull --ff-only {', '.join(apps)} on their current branches?",
            parent=self,
        )
        migrate = QCheckBox("Then run bench migrate")
        migrate.setChecked(True)
        build = QCheckBox("Then rebuild these apps' assets")
        build.setChecked(True)
        holder = QWidget()
        holder_box = QVBoxLayout(holder)
        holder_box.setContentsMargins(0, 0, 0, 0)
        holder_box.addWidget(migrate)
        holder_box.addWidget(build)
        box.setCheckBox(None)
        box.layout().addWidget(holder, box.layout().rowCount(), 0, 1, box.layout().columnCount())
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if box.exec() == QMessageBox.StandardButton.Ok:
            m, b = migrate.isChecked(), build.isChecked()
            self.ctx.submit(self, lambda ops: ops.pull_apps(self.name, apps, m, b), show=True)

    def _tests(self) -> None:
        apps = self._selected_apps()
        if len(apps) != 1:
            return
        module, ok = QInputDialog.getText(
            self,
            f"Run {apps[0]} tests",
            "Module to test (optional, e.g. erpnext.stock.doctype.item.test_item).\n"
            "Leave empty to run every test in the app. Enables allow_tests on this site.",
        )
        if ok:
            self.ctx.submit(self, lambda ops: ops.run_tests(self.name, apps[0], module.strip()), show=True)

    def _flag_boxes(self, bench: Bench) -> dict[str, tuple[QCheckBox, bool, str]]:
        return {
            "developer_mode": (self.dev_mode, bench.developer_mode, "Developer mode"),
            "admin_tools": (self.admin_tools, bench.admin_tools, "Admin tools (Mailpit, Adminer)"),
            "maintenance": (
                self.maintenance_box,
                bench.maintenance_mode,
                "Maintenance mode (visitors see a maintenance page)",
            ),
            "scheduler": (
                self.scheduler_box,
                bench.scheduler_paused,
                "Pause scheduler (no background jobs run)",
            ),
        }

    def _render_flags(self, bench: Bench) -> None:
        """Show on-disk state, except while a change the user made is still being applied."""
        for key, (box, actual, text) in self._flag_boxes(bench).items():
            pending = self._pending_flags.get((bench.name, key))
            box.blockSignals(True)
            box.setChecked(actual if pending is None else pending)
            box.blockSignals(False)
            box.setEnabled(pending is None)
            box.setText(text if pending is None else f"{text} — applying…")

    def _switch_flag(self, key: str, value: bool, build) -> None:
        site = self.name
        run = self.ctx.submit(self, build)
        if run is None:
            self._render_flags(self.bench)  # submit refused: snap back to the real state
            return
        self._pending_flags[(site, key)] = value

        def settle() -> None:
            self._pending_flags.pop((site, key), None)
            self.ctx.benches.refresh()  # re-reads site_config.json → final state

        run.finished.connect(settle)
        self._render_flags(self.bench)

    def _admin_password(self) -> None:
        run_dialog(AdminPasswordDialog(self.ctx, self.name, self))

    def _cache_menu(self) -> None:
        menu = QMenu(self)
        add_cache_actions(self.ctx, self, self.name, menu)
        menu.exec(self._clear_cache_btn.mapToGlobal(self._clear_cache_btn.rect().bottomLeft()))

    def _build(self, apps: list[str] | None = None) -> None:
        run_dialog(BuildDialog(self.ctx, self.bench, preselect=apps, parent=self))

    def _remove_app(self) -> None:
        item = self.apps_tree.currentItem()
        if not item:
            return
        app = item.text(0)
        if app == "frappe":
            QMessageBox.information(self, "Can't remove Frappe", "Frappe is the framework every app needs.")
            return
        question = f"Uninstall {app} from {self.name}? Its data (DocTypes, records) is removed from the site."
        self._run(lambda ops: ops.remove_app(self.bench, app), confirm=question)

    def _convert(self) -> None:
        if not self.ctx.tools.ok("fmd"):
            self.ctx.navigate.emit("settings")
            return
        self._run(
            lambda ops: ops.convert_to_deployer(self.bench),
            confirm="Convert this site to release-based deploys? fmd restructures the bench "
            "workspace and builds a first release from the installed apps.",
        )

    def _import_config(self) -> None:
        path = pick_fmd_config(self)
        if not path:
            return
        try:
            imported = deployer.load_file(path)
        except deployer.ConfigError as exc:
            QMessageBox.warning(self, "Can't use this config", str(exc))
            return
        rename = ""
        if imported.site != self.name:
            rename = f"\n\nIts site_name ({imported.site}) will be set to {self.name}."
        question = (
            f"Deploy {imported.path.name} to {self.name}?\n\n{imported.summary()}.{rename}\n\n"
            "This replaces the site's deploy config and builds a new release."
        )
        self._run(lambda ops: ops.import_config(self.bench, imported), confirm=question)

    def _deploy(self) -> None:
        bench = self.bench
        config = deployer.load(bench.name)
        self._run(
            lambda ops: ops.deploy(bench.name, None if config else ops.adopt_config(bench)),
            confirm="Build a new release from the deploy config and switch to it?",
        )

    def _switch(self) -> None:
        item = self.releases_tree.currentItem()
        if not item:
            return
        release = item.data(0, Qt.ItemDataRole.UserRole)
        self._run(
            lambda ops: ops.switch_release(self.name, release),
            confirm=f"Switch {self.name} to {release}? Migrations run under maintenance mode.",
        )

    # -- containers -----------------------------------------------------------------------
    def _render_containers(self) -> None:
        if self.name and self.isVisible():
            fill_container_tree(self.containers_tree, [("", self.ctx.system.for_bench(self.name))])

    def _selected_container(self):
        item = self.containers_tree.currentItem()
        cid = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        return next((c for c in self.ctx.system.containers if c.id == cid), None)

    def _container_action(self, action: str) -> None:
        c = self._selected_container()
        if c:
            self.ctx.submit(self, lambda ops: ops.container(action, c.id, c.name))

    def _container_logs(self) -> None:
        c = self._selected_container()
        if c:
            open_container_logs(self.ctx, self, c)

    def _container_menu(self, pos) -> None:
        c = self._selected_container()
        if c:
            menu = QMenu(self)
            container_menu(self.ctx, self, c, menu)
            menu.exec(self.containers_tree.viewport().mapToGlobal(pos))

    def _repair(self) -> None:
        self._run(
            lambda ops: ops.repair(self.name),
            confirm=(
                f"Repair {self.name}? This makes sure Docker and the global services are up, reloads the "
                "proxy and recreates the bench's containers (code and data are kept)."
            ),
        )

    # -- fm info / credentials ----------------------------------------------------------
    def _load_info(self) -> None:
        name = self.name
        try:
            argv = self.ctx.ops.info_argv(name)
        except Exception as exc:
            QMessageBox.warning(self, "fm info", str(exc))
            return
        self.info_btn.setEnabled(False)
        self.info_btn.setText("Loading…")

        def work() -> list[InfoRow]:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=120,
                env=tool_env(self.ctx.settings.secret_env()),
                check=False,
            )
            rows = parse_info(proc.stdout + proc.stderr)
            if not rows:
                raise RuntimeError((proc.stdout + proc.stderr).strip()[-400:] or "fm info printed nothing")
            return rows

        def done(rows: list[InfoRow]) -> None:
            self._info_cache[name] = rows
            self.info_btn.setEnabled(True)
            self.info_btn.setText("Refresh")
            if self.name == name:
                self._render_info(rows)

        def failed(message: str) -> None:
            self.info_btn.setEnabled(True)
            self.info_btn.setText("Load fm info")
            self.creds_hint.setText(f"⚠ {message}")
            self.creds_hint.show()

        run_async(work, done, failed)

    def _render_info(self, rows: list[InfoRow] | None) -> None:
        while self.creds_grid.count():
            widget = self.creds_grid.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        self.creds_hint.setVisible(rows is None)
        self.info_btn.setText("Refresh" if rows else "Load fm info")
        for i, row in enumerate(rows or []):
            self.creds_grid.addWidget(label(row.key, "muted"), i, 0, alignment=Qt.AlignmentFlag.AlignRight)
            if row.is_url:
                value = label(f'<a href="{row.value}">{row.value}</a>')
                value.setOpenExternalLinks(True)
            else:
                value = label("••••••••" if row.secret else row.value, wrap=True)
                value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.creds_grid.addWidget(value, i, 1)
            if row.secret or row.key.endswith("username") or row.key.endswith("User"):
                actions = QHBoxLayout()
                actions.setContentsMargins(0, 0, 0, 0)
                if row.secret:
                    reveal = button("Show")
                    reveal.setCheckable(True)
                    reveal.toggled.connect(
                        lambda on, v=value, r=row, b=reveal: (
                            v.setText(r.value if on else "••••••••"),
                            b.setText("Hide" if on else "Show"),
                        )
                    )
                    actions.addWidget(reveal)
                copy = button(
                    "Copy", on_click=lambda _=False, v=row.value: QApplication.clipboard().setText(v)
                )
                actions.addWidget(copy)
                holder = QWidget()
                holder.setLayout(actions)
                self.creds_grid.addWidget(holder, i, 2)

    def _toggle_logs(self) -> None:
        if self.logs.running:
            self.logs.stop()
            self.log_btn.setText("Follow logs")
            return
        service = self.log_service.currentData()
        try:
            job = self.ctx.ops.logs(self.name, service)
        except Exception as exc:
            QMessageBox.warning(self, "Logs", str(exc))
            return
        self.log_view.append_text(f"── following {service or 'bench'} logs ──\n")
        self.logs.start(job)
        self.log_btn.setText("Stop following")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if getattr(self, "_stale", True):
            self.render()
            self._render_containers()

    def hideEvent(self, event) -> None:
        self.logs.stop()
        super().hideEvent(event)


def _grid(buttons: tuple) -> QGridLayout:
    grid = QGridLayout()
    grid.setHorizontalSpacing(theme.SM)
    grid.setVerticalSpacing(theme.SM)
    for i, widget in enumerate(buttons):
        grid.addWidget(widget, i // 3, i % 3)
    return grid


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
