"""Small dialogs: add apps to a site, edit a My Apps entry, confirm deletion, pick a site."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
)

from fmapp.core import marketplace
from fmapp.core.catalog import CatalogApp
from fmapp.core.models import AppRef, Bench, BenchKind
from fmapp.ui.async_ import run_async
from fmapp.ui.context import AppContext
from fmapp.ui.dialogs.app_selector import AppSelector
from fmapp.ui.widgets import button, form_layout, label


class AddAppsDialog(QDialog):
    def __init__(
        self, ctx: AppContext, bench: Bench, preselect: list[AppRef] | None = None, parent=None
    ) -> None:
        super().__init__(parent)
        self.ctx, self.bench = ctx, bench
        self.setWindowTitle(f"Add apps to {bench.name}")
        self.resize(940, 580)
        box = QVBoxLayout(self)
        box.addWidget(label(f"Add apps to {bench.name}", "h1"))
        if bench.kind is BenchKind.DEPLOYER:
            note = "This site uses Frappe Deployer: adding apps builds and switches to a new release."
        else:
            note = "Apps are cloned with `bench get-app` and installed into the site."
        box.addWidget(label(note, "muted", wrap=True))
        frappe = next((a for a in bench.apps if a.name == "frappe"), None)
        frappe_ref = frappe.branch if frappe and frappe.branch else ctx.settings.default_frappe_branch
        self.selector = AppSelector(ctx, lambda: frappe_ref, exclude={a.name for a in bench.apps})
        if preselect:
            self.selector.preselect(preselect)
        box.addWidget(self.selector, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        install = button("Install", "primary", on_click=self._install)
        buttons.addButton(install, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

    def _install(self) -> None:
        apps = self.selector.selected()
        if not apps:
            return
        if self.ctx.submit(self, lambda ops: ops.add_apps(self.bench, apps), show=True):
            self.accept()


class CatalogAppDialog(QDialog):
    """Create or edit an entry in My Apps."""

    def __init__(self, ctx: AppContext, app: CatalogApp | None = None, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.app = app or CatalogApp(title="", repo="")
        self.setWindowTitle("Edit app" if app else "Add app")
        self.setMinimumWidth(520)
        form = form_layout(self)
        self.title = QLineEdit(self.app.title, placeholderText="My App")
        self.repo = QLineEdit(self.app.repo, placeholderText="org/repo or https://gitlab.com/org/repo")
        self.ref = QComboBox()
        self.ref.setEditable(True)
        self.ref.setCurrentText(self.app.ref)
        self.ref.lineEdit().setPlaceholderText("default branch")
        fetch = button("Fetch branches", on_click=self._fetch)
        ref_row = QHBoxLayout()
        ref_row.addWidget(self.ref, 1)
        ref_row.addWidget(fetch)
        self.subdir = QLineEdit(self.app.subdir, placeholderText="only for monorepos")
        self.private = QCheckBox("Private repository (uses your GitHub token)")
        self.private.setChecked(self.app.private)
        self.description = QPlainTextEdit(self.app.description)
        self.description.setFixedHeight(70)
        form.addRow("Title", self.title)
        form.addRow("Repository", self.repo)
        form.addRow("Branch", ref_row)
        form.addRow("Subdirectory", self.subdir)
        form.addRow("", self.private)
        form.addRow("Notes", self.description)
        self.error = label("", wrap=True)
        self.error.setProperty("role", "error")
        form.addRow(self.error)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _fetch(self) -> None:
        try:
            org_repo = AppRef.parse(self.repo.text()).org_repo
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        if not org_repo:
            self.error.setText("Branch lookup works for GitHub repositories only.")
            return
        current = self.ref.currentText()

        def fill(branches: list[str]) -> None:
            self.ref.clear()
            self.ref.addItems(branches)
            self.ref.setCurrentText(current)
            self.error.setText("" if branches else "No branches found (private repo without token?)")

        token = self.ctx.settings.github_token
        run_async(lambda: marketplace.list_branches(org_repo, token), fill)

    def _save(self) -> None:
        repo = self.repo.text().strip()
        try:
            parsed = AppRef.parse(repo)
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.app.title = self.title.text().strip() or parsed.name_guess
        self.app.repo = parsed.repo
        self.app.ref = self.ref.currentText().strip() or parsed.ref
        self.app.subdir = self.subdir.text().strip() or parsed.subdir
        self.app.private = self.private.isChecked()
        self.app.description = self.description.toPlainText().strip()
        self.ctx.catalog.upsert(self.app)
        self.accept()


class ConfirmDeleteDialog(QDialog):
    def __init__(self, bench: Bench, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Delete {bench.name}")
        self.setMinimumWidth(460)
        box = QVBoxLayout(self)
        box.addWidget(label(f"Delete {bench.name}?", "h2"))
        box.addWidget(
            label("This removes the bench directory, containers and code. It cannot be undone.", wrap=True)
        )
        self.drop_db = QCheckBox("Also delete its database from the global MariaDB")
        self.drop_db.setChecked(True)
        box.addWidget(self.drop_db)
        box.addWidget(label(f"Type <b>{bench.name}</b> to confirm:"))
        self.confirm = QLineEdit()
        box.addWidget(self.confirm)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = button("Delete site", "danger", on_click=self.accept)
        self.ok.setEnabled(False)
        buttons.addButton(self.ok, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        self.confirm.textChanged.connect(lambda t: self.ok.setEnabled(t.strip() == bench.name))


class PickSiteDialog(QDialog):
    def __init__(self, ctx: AppContext, title: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(380)
        box = QVBoxLayout(self)
        box.addWidget(label(title, "h2"))
        self.combo = QComboBox()
        for bench in ctx.benches.benches:
            if not bench.error:
                self.combo.addItem(bench.name)
        box.addWidget(self.combo)
        if not self.combo.count():
            box.addWidget(label("No sites yet — create one first.", "muted"))
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(self.combo.count() > 0)
        box.addWidget(buttons)

    def site(self) -> str:
        return self.combo.currentText()


class ConfirmRestoreDialog(QDialog):
    def __init__(self, site: str, backup, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Restore {site}")
        self.setMinimumWidth(480)
        box = QVBoxLayout(self)
        box.addWidget(label(f"Restore {site} from {backup.stamp}?", "h2"))
        box.addWidget(
            label(
                "The site's current database is replaced by this backup, then migrations run. "
                "Anything changed since the backup is lost — take a fresh backup first if unsure.",
                wrap=True,
            )
        )
        self.with_files = QCheckBox("Also restore uploaded files from this backup")
        self.with_files.setChecked(bool(backup.public_files or backup.private_files))
        self.with_files.setEnabled(bool(backup.public_files or backup.private_files))
        box.addWidget(self.with_files)
        box.addWidget(label(f"Type <b>{site}</b> to confirm:"))
        self.confirm = QLineEdit()
        box.addWidget(self.confirm)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = button("Restore", "danger", on_click=self.accept)
        self.ok.setEnabled(False)
        buttons.addButton(self.ok, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        self.confirm.textChanged.connect(lambda t: self.ok.setEnabled(t.strip() == site))
