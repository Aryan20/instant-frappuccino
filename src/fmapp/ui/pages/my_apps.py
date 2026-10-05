"""My Apps — your own repos and saved marketplace apps, reusable across sites."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView, QMessageBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from fmapp.ui import theme
from fmapp.ui.context import AppContext
from fmapp.ui.dialogs.simple import AddAppsDialog, CatalogAppDialog, PickSiteDialog
from fmapp.ui.widgets import button, label, page_header, run_dialog, tidy_view

ID = Qt.ItemDataRole.UserRole


class MyAppsPage(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        box = QVBoxLayout(self)
        box.setContentsMargins(*theme.PAGE_MARGINS)
        box.setSpacing(theme.SECTION_GAP)
        header, actions = page_header("My Apps", "Your own apps — any GitHub/GitLab repo, public or private")
        self.install_btn = button("Install on site…", on_click=self._install)
        self.edit_btn = button("Edit", on_click=self._edit)
        self.delete_btn = button("Remove", on_click=self._delete)
        for widget in (self.install_btn, self.edit_btn, self.delete_btn):
            actions.addWidget(widget)
        actions.addWidget(button("Add app", "primary", on_click=self._add))
        box.addWidget(header)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["App", "Repository", "Branch", "Notes"])
        self.tree.setRootIsDecorated(False)
        tidy_view(self.tree)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.itemDoubleClicked.connect(lambda *_: self._edit())
        self.tree.currentItemChanged.connect(lambda *_: self._sync_buttons())
        box.addWidget(self.tree, 1)
        self.empty = label(
            "Add your first app: point at a repo (e.g. myorg/my_app) and pick a branch. "
            "It then shows up in the New Site wizard and in Add apps.",
            "muted",
            wrap=True,
        )
        box.addWidget(self.empty)
        self._render()

    def _selected(self):
        item = self.tree.currentItem()
        if not item:
            return None
        return next((a for a in self.ctx.catalog.apps if a.id == item.data(0, ID)), None)

    def _sync_buttons(self) -> None:
        has = self._selected() is not None
        for widget in (self.install_btn, self.edit_btn, self.delete_btn):
            widget.setEnabled(has)

    def _render(self) -> None:
        self.tree.clear()
        for app in self.ctx.catalog.apps:
            lock = "🔒 " if app.private else ""
            source = " (marketplace)" if app.marketplace_name else ""
            item = QTreeWidgetItem(
                [
                    f"{lock}{app.title}{source}",
                    app.repo + (f"#{app.subdir}" if app.subdir else ""),
                    app.ref or "default",
                    app.description,
                ]
            )
            item.setData(0, ID, app.id)
            self.tree.addTopLevelItem(item)
        self.empty.setVisible(not self.ctx.catalog.apps)
        self._sync_buttons()

    def _add(self) -> None:
        if run_dialog(CatalogAppDialog(self.ctx, parent=self)):
            self._render()

    def _edit(self) -> None:
        app = self._selected()
        if app and run_dialog(CatalogAppDialog(self.ctx, app, parent=self)):
            self._render()

    def _delete(self) -> None:
        app = self._selected()
        if (
            app
            and QMessageBox.question(
                self,
                "Remove app",
                f"Remove {app.title} from My Apps? Sites that already have it are not affected.",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.ctx.catalog.remove(app.id)
            self._render()

    def _install(self) -> None:
        app = self._selected()
        if not app:
            return
        picker = PickSiteDialog(self.ctx, f"Install {app.title} on…", self)
        if run_dialog(picker) and (bench := self.ctx.benches.get(picker.site())):
            run_dialog(AddAppsDialog(self.ctx, bench, preselect=[app.to_ref()], parent=self))

    def showEvent(self, event) -> None:
        self._render()  # marketplace page may have saved apps meanwhile
        super().showEvent(event)
