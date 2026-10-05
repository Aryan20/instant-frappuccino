"""The "New Site" wizard: basics → apps → review."""

from __future__ import annotations

import re

import tomli_w
from PySide6.QtCore import Qt
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from fmapp.core import deployer
from fmapp.core.models import AppRef, BenchKind, SiteSpec
from fmapp.core.operations import MissingTool
from fmapp.core.settings import FRAPPE_BRANCHES
from fmapp.core.textutil import mask
from fmapp.ui import theme
from fmapp.ui.context import AppContext
from fmapp.ui.dialogs.app_selector import AppSelector
from fmapp.ui.widgets import (
    Banner,
    ChoiceCard,
    align_forms,
    button,
    form_layout,
    label,
    pick_fmd_config,
    scroll_page,
)

_SITE_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
STEPS = ("Basics", "Apps", "Review")


class NewSiteDialog(QDialog):
    def __init__(self, ctx: AppContext, preselect: list[AppRef] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.kind = BenchKind.FM
        self.imported: deployer.ImportedConfig | None = None
        self.setWindowTitle("New Site")
        self.setAcceptDrops(True)  # drop a site.toml anywhere on the wizard
        self.resize(1000, 760)

        root = QVBoxLayout(self)
        root.setContentsMargins(theme.XXL, theme.XL, theme.XXL, theme.XL)
        root.setSpacing(theme.LG)
        self.crumbs = label("", "muted")
        root.addWidget(self.crumbs)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.stack.addWidget(self._basics_page())
        self.selector = AppSelector(ctx, self.frappe_ref)
        apps_page = QWidget()
        apps_box = QVBoxLayout(apps_page)
        apps_box.setContentsMargins(0, 0, 0, 0)
        apps_box.setSpacing(theme.LG)
        apps_box.addWidget(label("Apps", "h1"))
        apps_box.addWidget(self.selector, 1)
        self.stack.addWidget(apps_page)
        self.stack.addWidget(self._review_page())
        if preselect:
            self.selector.preselect(preselect)

        nav = QHBoxLayout()
        nav.setSpacing(theme.SM)
        self.error = label("", wrap=True)
        self.error.setProperty("role", "error")
        nav.addWidget(self.error, 1)
        self.back_btn = button("Back", on_click=lambda: self._go(self.stack.currentIndex() - 1))
        self.next_btn = button("Continue", "primary", on_click=self._next)
        nav.addWidget(button("Cancel", on_click=self.reject))
        nav.addWidget(self.back_btn)
        nav.addWidget(self.next_btn)
        root.addLayout(nav)
        self._select_kind(BenchKind.FM)
        self._go(0)

    # -- pages ----------------------------------------------------------------------------
    def _basics_page(self) -> QWidget:
        scroll, page = scroll_page()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, theme.MD, 0)  # room for the scrollbar
        box.setSpacing(theme.SECTION_GAP)
        box.addWidget(label("Create a new site", "h1"))

        form = form_layout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.name = QLineEdit(placeholderText="mysite.localhost")
        self.name.textChanged.connect(lambda: self.error.setText(""))
        form.addRow("Site name", self.name)
        self.frappe = QComboBox()
        self.frappe.setEditable(True)
        self.frappe.addItems(FRAPPE_BRANCHES)
        self.frappe.setCurrentText(self.ctx.settings.default_frappe_branch)
        self.frappe.currentTextChanged.connect(lambda _: self.selector.refresh_branches())
        form.addRow("Frappe version", self.frappe)
        box.addLayout(form)

        box.addWidget(label("How should this site be managed?", "h2"))
        cards = QHBoxLayout()
        cards.setSpacing(theme.MD)
        self.fm_card = ChoiceCard(
            "Frappe Manager",
            "Development bench. Code lives in the bench, edit apps live, developer mode on. "
            "Best for building apps.",
        )
        self.fmd_card = ChoiceCard(
            "Frappe Deployer",
            "Production-style, like Frappe Cloud. Every deploy builds an immutable release; "
            "switching is atomic and you can roll back in one click.",
        )
        self.fm_card.clicked.connect(lambda: self._select_kind(BenchKind.FM))
        self.fmd_card.clicked.connect(lambda: self._select_kind(BenchKind.DEPLOYER))
        cards.addWidget(self.fm_card)
        cards.addWidget(self.fmd_card)
        box.addLayout(cards)
        if not self.ctx.tools.ok("fmd"):
            self.fmd_card.note.setText("⚠ fmd isn't installed — install it from Settings first.")
            self.fmd_card.note.show()

        # Instant setup: start from an existing fmd site.toml.
        self.import_bar = Banner("info")
        self.import_clear = button("Clear", on_click=self.clear_import)
        self.import_bar.layout().addWidget(self.import_clear)
        box.addWidget(self.import_bar)
        self._render_import()

        self.fm_opts = QGroupBox("Bench options")
        fm_form = form_layout(self.fm_opts)
        fm_form.setContentsMargins(0, theme.SM, 0, 0)
        self.environment = QComboBox()
        self.environment.addItems(["dev", "prod"])
        fm_form.addRow("Environment", self.environment)
        self.dev_mode = QCheckBox("Enable developer mode")
        self.dev_mode.setChecked(True)
        fm_form.addRow("", self.dev_mode)
        box.addWidget(self.fm_opts)

        self.fmd_opts = QGroupBox("Release options")
        fmd_form = form_layout(self.fmd_opts)
        fmd_form.setContentsMargins(0, theme.SM, 0, 0)
        self.maintenance = QCheckBox("Maintenance mode while migrating")
        self.maintenance.setChecked(True)
        self.backups = QCheckBox("Back up the database before every switch")
        self.backups.setChecked(True)
        self.rollback = QCheckBox("Roll back automatically if migration fails")
        self.rollback.setChecked(True)
        for widget in (self.maintenance, self.backups, self.rollback):
            fmd_form.addRow("", widget)
        box.addWidget(self.fmd_opts)

        advanced = QGroupBox("Advanced")
        adv = form_layout(advanced)
        adv.setContentsMargins(0, theme.SM, 0, 0)
        self.admin_pass = QLineEdit(self.ctx.settings.default_admin_password)
        self.admin_pass.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        adv.addRow("Administrator password", self.admin_pass)
        self.python = QLineEdit(placeholderText="auto (e.g. 3.11)")
        self.node = QLineEdit(placeholderText="auto (e.g. 20)")
        runtimes = QHBoxLayout()
        runtimes.setSpacing(theme.SM)
        runtimes.addWidget(label("Python"))
        runtimes.addWidget(self.python)
        runtimes.addWidget(label("Node"))
        runtimes.addWidget(self.node)
        adv.addRow("Runtimes", runtimes)
        self.aliases = QLineEdit(placeholderText="www.example.test, api.example.test")
        adv.addRow("Alias domains", self.aliases)
        box.addWidget(advanced)
        align_forms(form, fm_form, fmd_form, adv)
        box.addStretch()
        return scroll

    def _review_page(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(theme.MD)
        box.addWidget(label("Review", "h1"))
        self.summary = label("", wrap=True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        box.addWidget(self.summary)
        box.addWidget(label("These commands will run:", "muted"))
        self.preview = QPlainTextEdit()
        self.preview.setObjectName("LogView")
        self.preview.setReadOnly(True)
        box.addWidget(self.preview, 1)
        box.addWidget(
            label(
                "Creating a site pulls images and clones apps; the first one can take 10+ minutes. "
                "You can close this window — progress lives in Activity.",
                "muted",
                wrap=True,
            )
        )
        return page

    # -- behaviour ------------------------------------------------------------------------
    def frappe_ref(self) -> str:
        return self.frappe.currentText().strip() or "version-15"

    # -- fmd config import ----------------------------------------------------------------
    def choose_config(self) -> None:
        path = pick_fmd_config(self)
        if path:
            self.import_config(path)

    def import_config(self, path: str) -> bool:
        try:
            imported = deployer.load_file(path)
        except deployer.ConfigError as exc:
            self.error.setText(str(exc))
            return False
        self.imported = imported
        self.error.setText("")
        self._select_kind(BenchKind.DEPLOYER)
        self.name.setText(imported.site)
        if imported.frappe_ref:
            self.frappe.setCurrentText(imported.frappe_ref)
        self.python.setText(imported.options.python_version)
        self.node.setText(imported.options.node_version)
        self.maintenance.setChecked(imported.options.maintenance_mode)
        self.backups.setChecked(imported.options.backups)
        self.rollback.setChecked(imported.options.rollback)
        self.selector.clear()
        self.selector.preselect(imported.apps)
        self._render_import()
        return True

    def clear_import(self) -> None:
        self.imported = None
        self.selector.clear()
        self._render_import()

    def _render_import(self) -> None:
        loaded = self.imported is not None
        self.import_clear.setVisible(loaded)
        if loaded:
            self.import_bar.show_message(
                f"<b>Using {self.imported.path.name}</b> — {self.imported.summary()}. "
                "Settings this wizard doesn't show are kept as they are.",
                "Choose another…",
                self.choose_config,
            )
        else:
            self.import_bar.show_message(
                "<b>Have an fmd config?</b> Import a site.toml to set up everything in one go — "
                "or drop the file onto this window.",
                "Import fmd config…",
                self.choose_config,
            )
        self.import_bar.setVisible(self.kind is BenchKind.DEPLOYER)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile() and urls[0].toLocalFile().endswith(".toml"):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        path = event.mimeData().urls()[0].toLocalFile()
        if self.import_config(path):
            self._go(0)
        event.acceptProposedAction()

    def _select_kind(self, kind: BenchKind) -> None:
        self.kind = kind
        self.fm_card.set_selected(kind is BenchKind.FM)
        self.fmd_card.set_selected(kind is BenchKind.DEPLOYER)
        self.fm_opts.setVisible(kind is BenchKind.FM)
        self.fmd_opts.setVisible(kind is BenchKind.DEPLOYER)
        self.import_bar.setVisible(kind is BenchKind.DEPLOYER)

    def spec(self) -> SiteSpec:
        return SiteSpec(
            name=self.name.text().strip().lower(),
            kind=self.kind,
            frappe_ref=self.frappe_ref(),
            apps=[a for a in self.selector.selected() if a.name_guess != "frappe"],
            environment=self.environment.currentText(),
            developer_mode=self.dev_mode.isChecked(),
            admin_password=self.admin_pass.text() or "admin",
            python_version=self.python.text().strip(),
            node_version=self.node.text().strip(),
            alias_domains=[d.strip() for d in self.aliases.text().split(",") if d.strip()],
            maintenance_mode=self.maintenance.isChecked(),
            backups=self.backups.isChecked(),
            rollback=self.rollback.isChecked(),
            base_config=self.imported.config if self.imported and self.kind is BenchKind.DEPLOYER else None,
        )

    def _validate_basics(self) -> str:
        name = self.name.text().strip().lower()
        if not name:
            return "Give the site a name, e.g. mysite.localhost"
        if "." not in name:
            self.name.setText(f"{name}.localhost")
            name = f"{name}.localhost"
        if not _SITE_NAME.match(name):
            return "Use a domain-style name: lowercase letters, digits, dashes and dots."
        if self.ctx.benches.get(name):
            return f"A site named {name} already exists."
        if self.kind is BenchKind.DEPLOYER and not self.ctx.tools.ok("fmd"):
            return "Frappe Deployer (fmd) is not installed. Install it from Settings."
        return ""

    def _go(self, index: int) -> None:
        index = max(0, min(index, len(STEPS) - 1))
        self.stack.setCurrentIndex(index)
        self.crumbs.setText("  ›  ".join(f"<b>{s}</b>" if i == index else s for i, s in enumerate(STEPS)))
        self.back_btn.setEnabled(index > 0)
        self.next_btn.setText("Create site" if index == len(STEPS) - 1 else "Continue")
        if index == len(STEPS) - 1:
            self._fill_review()

    def _fill_review(self) -> None:
        spec = self.spec()
        kind = "Frappe Deployer (release-based)" if spec.kind is BenchKind.DEPLOYER else "Frappe Manager"
        apps = ", ".join(a.display() for a in spec.apps) or "none besides Frappe"
        source = ""
        if spec.base_config is not None and self.imported:
            source = (
                f"<br>From <code>{self.imported.path}</code> — {', '.join(self.imported.notes) or 'as is'}"
            )
        self.summary.setText(
            f"<b>{spec.name}</b> · {kind} · Frappe <code>{spec.frappe_ref}</code><br>Apps: {apps}{source}"
        )
        try:
            job = self.ctx.ops.create_site(spec)
            text = job.preview()
            if spec.kind is BenchKind.DEPLOYER:
                config = tomli_w.dumps(self.ctx.ops.deployer_config(spec))
                text += f"\n\n# {deployer.config_path(spec.name)}\n{mask(config, job.secrets)}"
            self.preview.setPlainText(text)
            self.next_btn.setEnabled(True)
        except MissingTool as exc:
            self.preview.setPlainText(str(exc))
            self.next_btn.setEnabled(False)

    def _next(self) -> None:
        index = self.stack.currentIndex()
        if index == 0:
            problem = self._validate_basics()
            if problem:
                self.error.setText(problem)
                return
        if index == 0 and self.imported and self.kind is BenchKind.DEPLOYER:
            self._go(len(STEPS) - 1)  # instant setup: the config already lists the apps
            return
        if index < len(STEPS) - 1:
            self._go(index + 1)
            return
        spec = self.spec()
        if self.ctx.submit(self, lambda ops: ops.create_site(spec), show=True):
            self.accept()
