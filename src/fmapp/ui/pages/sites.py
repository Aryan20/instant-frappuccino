"""Sites list — the dashboard."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fmapp.core import remote
from fmapp.core.models import Bench, BenchKind, BenchStatus
from fmapp.ui import theme
from fmapp.ui.context import AppContext
from fmapp.ui.dialogs.simple import ConfirmDeleteDialog
from fmapp.ui.site_tools import add_tool_actions, launch_terminal, open_in_browser
from fmapp.ui.widgets import (
    Banner,
    Pill,
    button,
    fit_height,
    label,
    page_header,
    pick_fmd_config,
    run_dialog,
    tidy_view,
)

NAME = Qt.ItemDataRole.UserRole
ROW_HEIGHT = 44


def site_actions(ctx: AppContext, parent: QWidget, bench: Bench, menu: QMenu) -> None:
    """Populate a context/overflow menu with the standard per-site actions."""
    running = bench.status in (BenchStatus.RUNNING, BenchStatus.PARTIAL)
    menu.addAction("Open site", lambda: open_in_browser(bench.url)).setEnabled(running)
    menu.addAction("Open desk", lambda: open_in_browser(f"{bench.url}/app")).setEnabled(running)
    menu.addSeparator()
    add_tool_actions(ctx, parent, bench.name, menu, running)
    menu.addSeparator()
    if running:
        menu.addAction("Stop", lambda: ctx.submit(parent, lambda ops: ops.stop(bench.name)))
        menu.addAction("Restart", lambda: ctx.submit(parent, lambda ops: ops.restart(bench.name)))
    else:
        menu.addAction("Start", lambda: ctx.submit(parent, lambda ops: ops.start(bench.name)))
    if ctx.host.is_local:
        menu.addAction(
            "Show in folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(bench.path)))
        )
    menu.addSeparator()
    menu.addAction("Delete…", lambda: confirm_delete(ctx, parent, bench))


def confirm_delete(ctx: AppContext, parent: QWidget, bench: Bench) -> bool:
    dialog = ConfirmDeleteDialog(bench, parent)
    if run_dialog(dialog):
        drop = dialog.drop_db.isChecked()
        return ctx.submit(parent, lambda ops: ops.delete(bench.name, drop), show=True) is not None
    return False


class SitesPage(QWidget):
    COLUMNS = ("Site", "Status", "Type", "Env", "Frappe", "Apps")

    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        box = QVBoxLayout(self)
        box.setContentsMargins(*theme.PAGE_MARGINS)
        box.setSpacing(theme.SECTION_GAP)

        header, actions = page_header("Sites", "Frappe Manager benches on this machine")
        self.search = QLineEdit(placeholderText="Filter…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(220)
        self.search.textChanged.connect(self._render)
        actions.addWidget(self.search)
        actions.addWidget(button("Refresh", on_click=ctx.benches.refresh))
        actions.addWidget(
            button(
                "Import fmd config…",
                on_click=self._import_config,
                tooltip="Create a Frappe Deployer site from an existing site.toml",
            )
        )
        actions.addWidget(button("New site", "primary", on_click=lambda: ctx.new_site.emit([])))
        box.addWidget(header)
        self.subtitle = header.findChild(QLabel, "PageSubtitle")

        self.banner = Banner("warn")
        self.banner.hide()
        box.addWidget(self.banner)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.COLUMNS))
        self.tree.setHeaderLabels(list(self.COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        tidy_view(self.tree)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.itemDoubleClicked.connect(lambda item, _c: ctx.open_site.emit(item.data(0, NAME)))
        header_view = self.tree.header()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col, width in zip(range(1, len(self.COLUMNS)), (124, 112, 72, 96, 64), strict=True):
            header_view.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            header_view.resizeSection(col, width)
        header_view.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header_view.setMinimumSectionSize(72)
        box.addWidget(self.tree)

        self.empty = QWidget()
        empty_box = QVBoxLayout(self.empty)
        empty_box.addStretch()
        empty_box.addWidget(label("No sites yet", "h2"), alignment=Qt.AlignmentFlag.AlignHCenter)
        empty_box.addWidget(
            label("Create a Frappe site with apps from the marketplace or your own repos.", "muted"),
            alignment=Qt.AlignmentFlag.AlignHCenter,
        )
        empty_box.addWidget(
            button("Create your first site", "primary", on_click=lambda: ctx.new_site.emit([])),
            alignment=Qt.AlignmentFlag.AlignHCenter,
        )
        empty_box.addStretch()
        box.addWidget(self.empty, 1)
        self.empty.hide()

        self.hint = label("Double-click a site to manage it · right-click for quick actions", "faint")
        box.addWidget(self.hint)
        box.addStretch(1)

        ctx.benches.changed.connect(self._render)
        ctx.benches.changed.connect(self._update_banner)
        ctx.tools.changed.connect(self._update_banner)
        ctx.system.changed.connect(self._update_banner)
        ctx.jobs.changed.connect(self._render)
        ctx.host_changed.connect(self._host_changed)
        self._host_changed()

    def _host_changed(self) -> None:
        host = self.ctx.host
        where = "this machine" if host.is_local else f"{host.name} ({host.destination})"
        self.subtitle.setText(f"Frappe Manager benches on {where}")
        self._update_banner()

    def _import_config(self) -> None:
        path = pick_fmd_config(self)
        if path:
            self.ctx.new_site_from_config.emit(path)

    def _update_banner(self) -> None:
        if not self.ctx.host.is_local:
            self._update_server_banner()
            return
        tools, system = self.ctx.tools, self.ctx.system
        info = system.engine
        if tools.tools and not tools.ok("fm"):
            self.banner.show_message(
                "Frappe Manager (fm) was not found. Install it to create and manage sites.",
                "Install…",
                lambda: self.ctx.navigate.emit("settings"),
            )
        elif info is not None and not info.running:
            self.banner.show_message(
                f"Docker ({info.label}) is stopped. Start it from here — its window won't open.",
                "Start Docker",
                lambda: self.ctx.submit(self, lambda ops: ops.start_everything(self.ctx.sites_to_start())),
            )
        elif system.conflicts:
            names = ", ".join(c.name for c in system.conflicts)
            ids = [c.id for c in system.conflicts]
            self.banner.show_message(
                f"Ports 80/443 are taken by {names}, so *.localhost sites can't load.",
                "Free ports",
                lambda: self.ctx.submit(self, lambda ops: ops.stop_conflicting(ids)),
            )
        elif info is not None and not system.proxy_running:
            self.banner.show_message(
                "The global proxy is down, so sites won't open in the browser.",
                "Start services",
                lambda: self.ctx.submit(self, lambda ops: ops.services("start")),
            )
        else:
            self.banner.hide()

    def _update_server_banner(self) -> None:
        host, system = self.ctx.host, self.ctx.system
        error = self.ctx.benches.error or system.error
        if error:
            self.banner.show_message(
                error,
                "Connect in Terminal",
                lambda: launch_terminal(self, remote.login_argv(host), name=f"ssh-{host.name}"),
            )
        elif system.loaded and not system.has_tool("fm"):
            self.banner.show_message(f"Frappe Manager (fm) isn't installed on {host.name}.")
        elif system.engine is not None and not system.engine.running:
            self.banner.show_message(
                f"Docker isn't running on {host.name}. "
                "Start it on the server (e.g. sudo systemctl start docker)."
            )
        elif system.engine is not None and not system.proxy_running:
            self.banner.show_message(
                f"The global proxy on {host.name} is down, so its sites won't load.",
                "Start services",
                lambda: self.ctx.submit(self, lambda ops: ops.services("start")),
            )
        else:
            self.banner.hide()

    def _render(self) -> None:
        store = self.ctx.benches
        needle = self.search.text().strip().lower()
        selected = self.tree.currentItem().data(0, NAME) if self.tree.currentItem() else None
        busy = self.ctx.jobs.busy_benches(self.ctx.host.id)
        self.tree.clear()
        for bench in store.benches:
            if needle and needle not in bench.name.lower():
                continue
            item = QTreeWidgetItem(
                [
                    bench.name,
                    "",
                    "",
                    bench.environment,
                    bench.frappe_version or "—",
                    str(len(bench.apps)),
                ]
            )
            item.setData(0, NAME, bench.name)
            item.setSizeHint(0, QSize(0, ROW_HEIGHT))
            font = item.font(0)
            font.setWeight(QFont.Weight.DemiBold)
            item.setFont(0, font)
            if bench.error:
                item.setToolTip(0, bench.error)
            self.tree.addTopLevelItem(item)
            status = "working…" if bench.name in busy else bench.status.value
            self.tree.setItemWidget(
                item, 1, _cell(Pill(status, "queued" if bench.name in busy else bench.status.value))
            )
            kind = "Deployer" if bench.kind is BenchKind.DEPLOYER else "FM"
            self.tree.setItemWidget(item, 2, _cell(Pill(kind, bench.kind.value)))
            if bench.name == selected:
                self.tree.setCurrentItem(item)
        has_any = bool(store.benches)
        fit_height(self.tree, self.tree.topLevelItemCount(), max_rows=12, row_height=ROW_HEIGHT)
        self.tree.setVisible(has_any or not store.loaded)
        self.hint.setVisible(has_any)
        self.empty.setVisible(store.loaded and not has_any)

    def _menu(self, pos: QPoint) -> None:
        item = self.tree.itemAt(pos)
        if not item:
            return
        bench = self.ctx.benches.get(item.data(0, NAME))
        if not bench:
            return
        menu = QMenu(self)
        menu.addAction("Manage…", lambda: self.ctx.open_site.emit(bench.name))
        menu.addSeparator()
        site_actions(self.ctx, self, bench, menu)
        menu.exec(self.tree.viewport().mapToGlobal(pos))


def _cell(widget: QWidget) -> QWidget:
    holder = QWidget()
    row = QVBoxLayout(holder)
    row.setContentsMargins(6, 0, 6, 0)
    row.addWidget(widget, alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return holder
