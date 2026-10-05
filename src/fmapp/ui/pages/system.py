"""System — the Docker Desktop replacement: engine, global services, containers, disk."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fmapp.core import containers as cmod
from fmapp.ui import theme
from fmapp.ui.context import AppContext
from fmapp.ui.dialogs.logs import show_logs
from fmapp.ui.widgets import Banner, Card, Pill, button, label, page_header, scroll_page, tidy_view

CID = Qt.ItemDataRole.UserRole


def container_menu(ctx: AppContext, parent: QWidget, container: cmod.Container, menu: QMenu) -> None:
    def run(action: str) -> None:
        ctx.submit(parent, lambda ops: ops.container(action, container.id, container.name))

    if container.running:
        menu.addAction("Restart", lambda: run("restart"))
        menu.addAction("Stop", lambda: run("stop"))
    else:
        menu.addAction("Start", lambda: run("start"))
    menu.addAction("Logs…", lambda: open_container_logs(ctx, parent, container))


def open_container_logs(ctx: AppContext, parent: QWidget, container: cmod.Container) -> None:
    try:
        job = ctx.ops.container_logs(container.id)
    except Exception as exc:
        QMessageBox.warning(parent, "Logs", str(exc))
        return
    show_logs(parent, f"{container.name} logs", job)


def fill_container_tree(tree: QTreeWidget, items: list[tuple[str, list[cmod.Container]]]) -> None:
    """Grouped container list; keeps expansion and selection across refreshes."""
    selected = tree.currentItem().data(0, CID) if tree.currentItem() else None
    collapsed = {
        tree.topLevelItem(i).text(0)
        for i in range(tree.topLevelItemCount())
        if not tree.topLevelItem(i).isExpanded()
    }
    tree.clear()
    for group, containers in items:
        if len(items) > 1:
            running = sum(c.running for c in containers)
            parent = QTreeWidgetItem(tree, [group, f"{running}/{len(containers)} running"])
            parent.setFirstColumnSpanned(False)
            font = parent.font(0)
            font.setBold(True)
            parent.setFont(0, font)
            parent.setExpanded(group not in collapsed)
        else:
            parent = tree.invisibleRootItem()
        for c in containers:
            child = QTreeWidgetItem(parent, [c.service or c.name, c.status, c.cpu, c.memory, c.image])
            child.setIcon(0, _dot(theme.STATUS_COLORS["running" if c.running else "stopped"][0]))
            child.setData(0, CID, c.id)
            child.setToolTip(0, c.name)
            if not c.running:
                for col in range(5):
                    child.setForeground(col, QColor(theme.current.faint))
            if c.id == selected:
                tree.setCurrentItem(child)


def _dot(color: str) -> QIcon:
    pixmap = QPixmap(QSize(20, 20))
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(5, 5, 10, 10)
    painter.end()
    return QIcon(pixmap)


def _stat_tile(title: str, value: str, note: str) -> QWidget:
    tile = QFrame()
    tile.setProperty("card", True)
    tile.setStyleSheet(f"QFrame {{ background: {theme.current.subtle}; border: none; }}")
    box = QVBoxLayout(tile)
    box.setContentsMargins(theme.LG, theme.MD, theme.LG, theme.MD)
    box.setSpacing(2)
    box.addWidget(label(title, "muted"))
    big = label(value)
    big.setStyleSheet("font-size: 18px; font-weight: 700; background: transparent;")
    box.addWidget(big)
    box.addWidget(label(note, "faint", wrap=True))
    return tile


def make_container_tree() -> QTreeWidget:
    tree = QTreeWidget()
    tree.setHeaderLabels(["Container", "Status", "CPU", "Memory", "Image"])
    tidy_view(tree)
    tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    header = tree.header()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
    return tree


class SystemPage(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self._disk_shown: list[cmod.DiskRow] = []
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        _scroll, body = scroll_page(outer)
        box = QVBoxLayout(body)
        box.setContentsMargins(*theme.PAGE_MARGINS)
        box.setSpacing(theme.SECTION_GAP)

        header, actions = page_header(
            "System", "Docker engine, global services and every container — no Docker Desktop window needed"
        )
        actions.addWidget(button("Stop everything", on_click=self._stop_all))
        actions.addWidget(button("Start everything", "primary", on_click=self._start_all))
        box.addWidget(header)
        self.subtitle = header.findChild(QLabel, "PageSubtitle")

        self.banner = Banner("warn")
        self.banner.hide()
        box.addWidget(self.banner)

        top = QHBoxLayout()
        top.setSpacing(theme.LG)
        # engine
        eng = Card(title="Docker engine")
        self.engine_pill = Pill("…", "unknown")
        eng.actions.addWidget(self.engine_pill)
        self.engine_meta = label("", "muted", wrap=True)
        eng.body.addWidget(self.engine_meta)
        erow = QHBoxLayout()
        erow.setSpacing(theme.SM)
        self.engine_start = button("Start", "primary", on_click=lambda: self._engine("start"))
        self.engine_restart = button("Restart", on_click=lambda: self._engine("restart"))
        self.engine_stop = button("Stop", on_click=lambda: self._engine("stop"))
        for widget in (self.engine_start, self.engine_restart, self.engine_stop):
            erow.addWidget(widget)
        erow.addStretch()
        eng.body.addLayout(erow)
        eng.body.addStretch()
        top.addWidget(eng, 1)

        # global services
        svc = Card(title="Global services")
        svc.actions.addWidget(button("Restart all", on_click=lambda: self._services("restart", "all")))
        self.svc_grid = QGridLayout()
        self.svc_grid.setColumnStretch(1, 1)
        self.svc_grid.setHorizontalSpacing(theme.LG)
        self.svc_grid.setVerticalSpacing(theme.MD)
        self.svc_rows: dict[str, tuple[Pill, object]] = {}
        for i, (name, title) in enumerate(cmod.GLOBAL_SERVICES.items()):
            self.svc_grid.addWidget(
                label(f"<b>{title.split(' (')[0]}</b><br><span style='opacity:.65'>{name}</span>"), i, 0
            )
            pill = Pill("…", "unknown")
            self.svc_grid.addWidget(pill, i, 1, alignment=Qt.AlignmentFlag.AlignLeft)
            btns = QHBoxLayout()
            btns.setContentsMargins(0, 0, 0, 0)
            btns.setSpacing(theme.SM)
            toggle = button("Start", on_click=lambda _=False, n=name: self._toggle_service(n))
            btns.addWidget(toggle)
            btns.addWidget(button("Restart", on_click=lambda _=False, n=name: self._services("restart", n)))
            btns.addWidget(button("Logs", on_click=lambda _=False, n=name: self._service_logs(n)))
            holder = QWidget()
            holder.setLayout(btns)
            self.svc_grid.addWidget(holder, i, 2)
            self.svc_rows[name] = (pill, toggle)
        svc.body.addLayout(self.svc_grid)
        svc.body.addWidget(
            label(
                "The proxy routes http://<site>.localhost to each bench; the database "
                "is shared by all benches.",
                "faint",
                wrap=True,
            )
        )
        top.addWidget(svc, 2)
        box.addLayout(top)

        # containers
        cont = Card(title="Containers")
        crow = cont.actions
        self.count = label("", "muted")
        crow.addWidget(self.count)
        crow.addSpacing(theme.SM)
        self.c_restart = button("Restart", on_click=lambda: self._container_action("restart"))
        self.c_stop = button("Stop", on_click=lambda: self._container_action("stop"))
        self.c_start = button("Start", on_click=lambda: self._container_action("start"))
        self.c_logs = button("Logs", on_click=self._container_logs)
        for widget in (self.c_start, self.c_restart, self.c_stop, self.c_logs):
            crow.addWidget(widget)
        self.tree = make_container_tree()
        self.tree.setMinimumHeight(380)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.tree.currentItemChanged.connect(lambda *_: self._sync_buttons())
        cont.body.addWidget(self.tree)
        box.addWidget(cont)

        # disk
        disk = Card(title="Disk usage", subtitle="Space used by Docker")
        disk.actions.addWidget(button("Refresh", on_click=lambda: ctx.system.refresh(with_disk=True)))
        self.reclaim_btn = button("Reclaim space…", on_click=self._reclaim)
        disk.actions.addWidget(self.reclaim_btn)
        self.disk_row = QHBoxLayout()
        self.disk_row.setSpacing(theme.MD)
        disk.body.addLayout(self.disk_row)
        self.disk_hint = label("Measuring…", "faint")
        self.disk_row.addWidget(self.disk_hint)
        box.addWidget(disk)
        box.addStretch()

        ctx.system.changed.connect(self.render)
        ctx.jobs.changed.connect(self.render)
        ctx.host_changed.connect(self._host_changed)
        self._host_changed()
        self._sync_buttons()

    def _host_changed(self) -> None:
        local = self.ctx.host.is_local
        self.subtitle.setText(
            "Docker engine, global services and every container — no Docker Desktop window needed"
            if local
            else f"Global services and containers on {self.ctx.host.name}"
        )
        self.reclaim_btn.setVisible(local)
        self._disk_shown = []
        self.render()

    # -- lifecycle ------------------------------------------------------------------------
    def showEvent(self, event) -> None:
        self.ctx.system.want_stats = True
        self.ctx.system.refresh(with_disk=not self.ctx.system.disk)
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self.ctx.system.want_stats = False
        super().hideEvent(event)

    # -- render ---------------------------------------------------------------------------
    def render(self) -> None:
        sysstore = self.ctx.system
        info = sysstore.engine
        local = self.ctx.host.is_local
        busy = "@system" in self.ctx.jobs.busy_benches(self.ctx.host.id)
        if sysstore.error:
            self.banner.show_message(sysstore.error)
        if info is None:
            if not sysstore.error:
                self.banner.hide()
            self.engine_pill.set("…", "unknown")
            self.engine_meta.setText("")
            fill_container_tree(self.tree, [])
            self.count.setText("")
            for widget in (self.engine_start, self.engine_restart, self.engine_stop):
                widget.hide()
            return
        if busy:
            self.engine_pill.set("working…", "queued")
        else:
            self.engine_pill.set(
                "running" if info.running else "stopped", "running" if info.running else "stopped"
            )
        bits = [info.label if local else f"Docker on {self.ctx.host.name}"]
        if info.version:
            bits.append(f"engine {info.version}")
        if info.context:
            bits.append(f"context “{info.context}”")
        if not local:
            note = "\nThe server's Docker is managed on the server itself."
        else:
            note = "" if info.controllable else "\nProvider unknown — choose it in Settings → Engine."
        self.engine_meta.setText(" · ".join(bits) + note)
        for widget in (self.engine_start, self.engine_restart, self.engine_stop):
            widget.setEnabled(info.controllable and not busy)
        self.engine_start.setVisible(local and not info.running)
        self.engine_restart.setVisible(local and info.running)
        self.engine_stop.setVisible(local and info.running)

        for name, (pill, toggle) in self.svc_rows.items():
            container = sysstore.service(name)
            if not info.running:
                pill.set("engine off", "stopped")
            elif container is None:
                pill.set("not created", "stopped")
            else:
                pill.set(
                    container.status if container.running else container.state,
                    "running" if container.running else "stopped",
                )
            toggle.setText("Stop" if container and container.running else "Start")
            toggle.setEnabled(info.running and not busy)

        if sysstore.error:
            pass  # shown above
        elif not local and not info.running:
            self.banner.show_message(f"Docker isn't running on {self.ctx.host.name}. Start it on the server.")
        elif local and sysstore.conflicts:
            names = ", ".join(c.name for c in sysstore.conflicts)
            self.banner.show_message(
                f"Ports 80/443 are held by {names} (not Frappe Manager). The fm proxy can't serve "
                "*.localhost sites until they're stopped.",
                "Stop them & start proxy",
                self._free_ports,
            )
        elif info.running and not sysstore.proxy_running:
            self.banner.show_message(
                "The global proxy is not running, so no site will load in the browser.",
                "Start services",
                lambda: self._services("start", "all"),
            )
        elif not info.running:
            self.banner.show_message(
                f"The Docker engine is stopped. Start it here — no need to open {info.label}.",
                "Start engine",
                lambda: self._engine("start"),
            )
        else:
            self.banner.hide()

        groups: list[tuple[str, list[cmod.Container]]] = []
        global_ = [c for c in sysstore.containers if c.is_global]
        if global_:
            groups.append(("Global services", global_))
        for bench in sorted({c.bench for c in sysstore.containers if c.bench}):
            groups.append((bench, sysstore.for_bench(bench)))
        fill_container_tree(self.tree, groups)
        running = sum(c.running for c in sysstore.containers)
        self.count.setText(f"{running} running · {len(sysstore.containers)} total")

        if sysstore.disk and sysstore.disk != self._disk_shown:
            self._disk_shown = sysstore.disk
            while self.disk_row.count():
                widget = self.disk_row.takeAt(0).widget()
                if widget:
                    widget.deleteLater()
            for d in sysstore.disk:
                self.disk_row.addWidget(
                    _stat_tile(d.kind, d.size, f"{d.count} items · {d.reclaimable} reclaimable")
                )
        self._sync_buttons()

    def _selected(self) -> cmod.Container | None:
        item = self.tree.currentItem()
        cid = item.data(0, CID) if item else None
        return next((c for c in self.ctx.system.containers if c.id == cid), None)

    def _sync_buttons(self) -> None:
        c = self._selected()
        self.c_start.setEnabled(bool(c and not c.running))
        self.c_stop.setEnabled(bool(c and c.running))
        self.c_restart.setEnabled(bool(c and c.running))
        self.c_logs.setEnabled(c is not None)

    # -- actions --------------------------------------------------------------------------
    def _engine(self, action: str) -> None:
        if (
            action == "stop"
            and QMessageBox.question(
                self, "Stop Docker", "Stop the Docker engine? Every site and service goes offline."
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.ctx.submit(self, lambda ops: ops.engine_action(action))

    def _services(self, action: str, name: str) -> None:
        self.ctx.submit(self, lambda ops: ops.services(action, name))

    def _toggle_service(self, name: str) -> None:
        container = self.ctx.system.service(name)
        self._services("stop" if container and container.running else "start", name)

    def _service_logs(self, name: str) -> None:
        container = self.ctx.system.service(name)
        if container:
            open_container_logs(self.ctx, self, container)

    def _container_action(self, action: str) -> None:
        c = self._selected()
        if c:
            self.ctx.submit(self, lambda ops: ops.container(action, c.id, c.name))

    def _container_logs(self) -> None:
        c = self._selected()
        if c:
            open_container_logs(self.ctx, self, c)

    def _menu(self, pos: QPoint) -> None:
        c = self._selected()
        if not c:
            return
        menu = QMenu(self)
        container_menu(self.ctx, self, c, menu)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _free_ports(self) -> None:
        ids = [c.id for c in self.ctx.system.conflicts]
        self.ctx.submit(self, lambda ops: ops.stop_conflicting(ids))

    def _start_all(self) -> None:
        sites = self.ctx.sites_to_start()
        self.ctx.submit(self, lambda ops: ops.start_everything(sites), show=True)

    def _stop_all(self) -> None:
        if (
            QMessageBox.question(self, "Stop everything", "Stop all sites and global services?")
            != QMessageBox.StandardButton.Yes
        ):
            return
        stop_engine = self.ctx.settings.stop_engine_with_everything
        sites = self.ctx.running_sites()
        self.ctx.submit(self, lambda ops: ops.stop_everything(sites, stop_engine))

    def _reclaim(self) -> None:
        if (
            QMessageBox.question(
                self,
                "Reclaim disk space",
                "Remove dangling images and the build cache? Volumes (your databases) and images in "
                "use are never touched.",
            )
            == QMessageBox.StandardButton.Yes
        ):
            run = self.ctx.submit(self, lambda ops: ops.reclaim_space())
            if run:
                run.finished.connect(lambda: self.ctx.system.refresh(with_disk=True))
