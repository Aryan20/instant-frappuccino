"""Pick apps from the marketplace, My Apps, or any git repo — and pin their branches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from fmapp.core import marketplace
from fmapp.core.models import AppRef
from fmapp.ui import theme
from fmapp.ui.async_ import run_async
from fmapp.ui.context import AppContext
from fmapp.ui.widgets import button, form_layout, label

KEY = Qt.ItemDataRole.UserRole


@dataclass
class _Row:
    key: str
    title: str
    repo: str
    ref: str = ""
    subdir: str = ""


class AppSelector(QWidget):
    changed = Signal()

    def __init__(
        self,
        ctx: AppContext,
        frappe_ref: Callable[[], str],
        exclude: set[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.frappe_ref = frappe_ref
        self.exclude = exclude or set()
        self.rows: list[_Row] = []
        self._branches: dict[str, list[str]] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.XL)

        # -- sources ------------------------------------------------------------------------
        self.tabs = QTabWidget()
        self.tabs.setMinimumWidth(320)
        layout.addWidget(self.tabs, 5)

        market = QWidget()
        mbox = QVBoxLayout(market)
        self.search = QLineEdit(placeholderText="Search 360+ marketplace apps…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter_market)
        mbox.addWidget(self.search)
        self.market_list = QListWidget()
        self.market_list.itemChanged.connect(self._market_toggled)
        mbox.addWidget(self.market_list)
        self.market_status = label("Loading marketplace…", "muted")
        mbox.addWidget(self.market_status)
        self.tabs.addTab(market, "Marketplace")

        mine = QWidget()
        ybox = QVBoxLayout(mine)
        self.my_list = QListWidget()
        self.my_list.itemChanged.connect(self._mine_toggled)
        ybox.addWidget(self.my_list)
        self.my_empty = label("No saved apps yet. Add them from the My Apps page.", "muted", wrap=True)
        ybox.addWidget(self.my_empty)
        self.tabs.addTab(mine, "My Apps")

        custom = QWidget()
        form = form_layout(custom)
        self.custom_repo = QLineEdit(placeholderText="org/repo or https://… git URL")
        self.custom_ref = QLineEdit(placeholderText="branch / tag (blank = default)")
        self.custom_subdir = QLineEdit(placeholderText="apps/my_app (monorepos only)")
        form.addRow("Repository", self.custom_repo)
        form.addRow("Branch", self.custom_ref)
        form.addRow("Subdirectory", self.custom_subdir)
        self.custom_error = label("", wrap=True)
        self.custom_error.setProperty("role", "error")
        form.addRow(self.custom_error)
        form.addRow(button("Add app", on_click=self._add_custom))
        form.addRow(label("Private GitHub repos use the token from Settings.", "muted", wrap=True))
        self.tabs.addTab(custom, "Custom repo")

        # -- selection ----------------------------------------------------------------------
        right = QVBoxLayout()
        right.addWidget(label("Selected apps", "h2"))
        right.addWidget(
            label("Frappe is always included. Adjust source or branch as needed.", "muted", wrap=True)
        )
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["App", "Repository", "Branch"])
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(2, 150)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(self._cell_edited)
        right.addWidget(self.table, 1)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(button("Remove selected", on_click=self._remove_selected))
        right.addLayout(actions)
        layout.addLayout(right, 6)

        ctx.market.changed.connect(self._fill_market)
        ctx.market.details_loaded.connect(self._details_loaded)
        ctx.market.load()
        self._fill_market()
        self._fill_mine()

    # -- public ---------------------------------------------------------------------------
    def selected(self) -> list[AppRef]:
        return [AppRef(r.repo, r.ref, r.subdir) for r in self.rows if r.repo]

    def clear(self) -> None:
        for row in list(self.rows):
            self._remove_key(row.key)

    def preselect(self, refs: list[AppRef]) -> None:
        for ref in refs:
            self._add_row(_Row(f"ref:{ref.repo}", ref.name_guess, ref.repo, ref.ref, ref.subdir))

    def refresh_branches(self) -> None:
        """Frappe version changed: re-pick defaults where the user hasn't chosen."""
        for row in self.rows:
            self._suggest_branch(row)

    # -- marketplace ----------------------------------------------------------------------
    def _fill_market(self) -> None:
        store = self.ctx.market
        self.market_list.blockSignals(True)
        self.market_list.clear()
        selected = {r.key for r in self.rows}
        for app in store.apps:
            if app.name in self.exclude:
                continue
            featured = " ★" if app.name in marketplace.FEATURED else ""
            item = QListWidgetItem(f"{app.title}{featured}")
            item.setData(KEY, app.name)
            item.setToolTip(app.name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if f"mp:{app.name}" in selected else Qt.CheckState.Unchecked
            )
            self.market_list.addItem(item)
        self.market_list.blockSignals(False)
        if store.error and not store.apps:
            self.market_status.setText(f"⚠ {store.error}")
        else:
            self.market_status.setText(
                f"{len(store.apps)} apps from Frappe Cloud Marketplace"
                if store.apps
                else "Loading marketplace…"
            )
        self._filter_market(self.search.text())

    def _filter_market(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.market_list.count()):
            item = self.market_list.item(i)
            hay = f"{item.text()} {item.data(KEY)}".lower()
            item.setHidden(bool(needle) and needle not in hay)

    def _market_toggled(self, item: QListWidgetItem) -> None:
        name = item.data(KEY)
        key = f"mp:{name}"
        if item.checkState() == Qt.CheckState.Checked:
            app = self.ctx.market.get(name)
            details = self.ctx.market.details.get(name)
            repo = (details.repo if details else "") or marketplace.KNOWN_REPOS.get(name, "")
            self._add_row(_Row(key, app.title if app else name, repo))
            if not details and app:
                self.ctx.market.request_details(app)
        else:
            self._remove_key(key)

    def _details_loaded(self, name: str) -> None:
        details = self.ctx.market.details[name]
        for row in self.rows:
            if row.key == f"mp:{name}" and not row.repo:
                row.repo = details.repo
                self._render()
                self._suggest_branch(row)

    # -- my apps --------------------------------------------------------------------------
    def _fill_mine(self) -> None:
        self.my_list.blockSignals(True)
        self.my_list.clear()
        for app in self.ctx.catalog.apps:
            item = QListWidgetItem(f"{app.title}   —   {app.repo}")
            item.setData(KEY, app.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.my_list.addItem(item)
        self.my_list.blockSignals(False)
        self.my_empty.setVisible(not self.ctx.catalog.apps)

    def _mine_toggled(self, item: QListWidgetItem) -> None:
        app_id = item.data(KEY)
        key = f"my:{app_id}"
        if item.checkState() == Qt.CheckState.Checked:
            app = next(a for a in self.ctx.catalog.apps if a.id == app_id)
            self._add_row(_Row(key, app.title, app.repo, app.ref, app.subdir))
        else:
            self._remove_key(key)

    # -- custom ---------------------------------------------------------------------------
    def _add_custom(self) -> None:
        text = self.custom_repo.text().strip()
        try:
            ref = AppRef.parse(text)
        except ValueError as exc:
            self.custom_error.setText(str(exc))
            return
        self.custom_error.setText("")
        row = _Row(
            f"custom:{text}",
            ref.name_guess,
            ref.repo,
            self.custom_ref.text().strip() or ref.ref,
            self.custom_subdir.text().strip() or ref.subdir,
        )
        self._add_row(row)
        self.custom_repo.clear()
        self.custom_ref.clear()
        self.custom_subdir.clear()

    # -- table ----------------------------------------------------------------------------
    def _add_row(self, row: _Row) -> None:
        if any(r.key == row.key for r in self.rows):
            return
        self.rows.append(row)
        self._render()
        if not row.ref:
            self._suggest_branch(row)
        self.changed.emit()

    def _remove_key(self, key: str) -> None:
        self.rows = [r for r in self.rows if r.key != key]
        self._render()
        self._uncheck(key)
        self.changed.emit()

    def _uncheck(self, key: str) -> None:
        kind, _, value = key.partition(":")
        widget = {"mp": self.market_list, "my": self.my_list}.get(kind)
        if not widget:
            return
        widget.blockSignals(True)
        for i in range(widget.count()):
            if widget.item(i).data(KEY) == value:
                widget.item(i).setCheckState(Qt.CheckState.Unchecked)
        widget.blockSignals(False)

    def _remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for index in rows:
            self._remove_key(self.rows[index].key)

    def _render(self) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.rows))
        for i, row in enumerate(self.rows):
            title = QTableWidgetItem(row.title)
            title.setFlags(title.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(i, 0, title)
            repo = QTableWidgetItem(row.repo or "resolving…")
            repo.setToolTip("Double-click to change the source repository")
            self.table.setItem(i, 1, repo)
            combo = QComboBox()
            combo.setEditable(True)
            combo.lineEdit().setPlaceholderText("default branch")
            for branch in self._branches.get(row.repo, []):
                combo.addItem(branch)
            combo.setCurrentText(row.ref)
            combo.currentTextChanged.connect(lambda text, r=row: setattr(r, "ref", text.strip()))
            self.table.setCellWidget(i, 2, combo)
        self.table.blockSignals(False)

    def _cell_edited(self, item: QTableWidgetItem) -> None:
        if item.column() != 1:
            return
        row = self.rows[item.row()]
        text = item.text().strip()
        try:
            AppRef.parse(text)
        except ValueError:
            item.setText(row.repo)
            return
        row.repo = text
        self._suggest_branch(row)
        self.changed.emit()

    def _suggest_branch(self, row: _Row) -> None:
        """Default to the matching Frappe branch when the repo has one, else repo default."""
        if not row.repo:
            return
        app = AppRef(row.repo)
        wanted = self.frappe_ref()

        def apply(branches: list[str]) -> None:
            self._branches[row.repo] = branches
            if (
                branches
                and row in self.rows
                and (not row.ref or row.ref.startswith("version-") or row.ref == "develop")
            ):
                row.ref = wanted if wanted in branches else ""
            self._render()

        cached = self._branches.get(row.repo)
        if cached is not None:
            apply(cached)
            return
        s = self.ctx.settings
        token, ssh = s.github_token, s.git_over_ssh
        run_async(lambda: marketplace.branches_for(app, token, ssh), apply)
