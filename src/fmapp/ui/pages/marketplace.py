"""Browse the Frappe Cloud Marketplace and install apps locally."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from fmapp.core import marketplace
from fmapp.core.catalog import CatalogApp
from fmapp.core.models import AppRef
from fmapp.ui import theme
from fmapp.ui.async_ import run_async
from fmapp.ui.context import AppContext
from fmapp.ui.dialogs.simple import AddAppsDialog, PickSiteDialog
from fmapp.ui.widgets import (
    AppIcon,
    Card,
    Pill,
    button,
    empty_state,
    form_layout,
    label,
    page_header,
    run_dialog,
    splitter,
)

NAME = Qt.ItemDataRole.UserRole
CONFIDENCE = {
    "known": "Verified source.",
    "page": "Found on the app's marketplace page.",
    "guess": "Best guess — the marketplace doesn't publish sources. Check before installing.",
}


class MarketplacePage(QWidget):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.current: marketplace.MarketplaceApp | None = None
        box = QVBoxLayout(self)
        box.setContentsMargins(*theme.PAGE_MARGINS)
        box.setSpacing(theme.SECTION_GAP)
        header, actions = page_header(
            "Marketplace", "Apps from the Frappe Cloud Marketplace, installed locally"
        )
        actions.addWidget(button("Reload", on_click=lambda: ctx.market.load(force=True)))
        box.addWidget(header)

        left = QWidget()
        lbox = QVBoxLayout(left)
        lbox.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit(placeholderText="Search apps…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        lbox.addWidget(self.search)
        self.list = QListWidget()
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.list.currentItemChanged.connect(lambda cur, _prev: self._select(cur))
        lbox.addWidget(self.list, 1)
        self.count = label("", "muted")
        lbox.addWidget(self.count)

        self.detail = Card()
        self.detail_empty = empty_state(
            "Pick an app",
            "Choose an app on the left to see its details, pick a branch, and install it on a site "
            "— or start a new site with it.",
        )
        self.detail.body.addWidget(self.detail_empty)
        self.detail_body = QWidget()
        body = QVBoxLayout(self.detail_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(theme.CARD_GAP)
        top = QHBoxLayout()
        top.setSpacing(theme.LG)
        self.icon = AppIcon(64)
        top.addWidget(self.icon, alignment=Qt.AlignmentFlag.AlignTop)
        title_col = QVBoxLayout()
        self.title = label("Select an app", "h1")
        self.tagline = label("", "muted", wrap=True)
        title_col.addWidget(self.title)
        title_col.addWidget(self.tagline)
        top.addLayout(title_col, 1)
        body.addLayout(top)

        self.meta = label("", "muted", wrap=True)
        body.addWidget(self.meta)
        self.versions = QHBoxLayout()
        self.versions.setSpacing(6)
        self.versions.setAlignment(Qt.AlignmentFlag.AlignLeft)
        body.addLayout(self.versions)

        form = form_layout()
        self.repo = QLineEdit()
        self.repo.textEdited.connect(lambda _t: self._load_branches())
        self.repo_hint = label("", "muted", wrap=True)
        self.branch = QComboBox()
        self.branch.setEditable(True)
        self.branch.lineEdit().setPlaceholderText("default branch")
        form.addRow("Source", self.repo)
        form.addRow("", self.repo_hint)
        form.addRow("Branch", self.branch)
        body.addLayout(form)

        actions_row = QHBoxLayout()
        actions_row.setSpacing(theme.SM)
        self.install_btn = button("Install on site…", "primary", on_click=self._install)
        self.create_btn = button("New site with this app", on_click=self._new_site)
        self.save_btn = button("Save to My Apps", on_click=self._save)
        self.page_btn = button("View on Frappe Cloud", on_click=self._open_page)
        for widget in (self.install_btn, self.create_btn, self.save_btn, self.page_btn):
            actions_row.addWidget(widget)
        actions_row.addStretch()
        body.addLayout(actions_row)
        body.addStretch()
        self.detail.body.addWidget(self.detail_body, 1)
        box.addWidget(splitter(left, self.detail, sizes=(340, 680)), 1)
        self._set_enabled(False)

        ctx.market.changed.connect(self._fill)
        ctx.market.details_loaded.connect(self._details_loaded)

    def showEvent(self, event) -> None:
        self.ctx.market.load()
        super().showEvent(event)

    # -- list -----------------------------------------------------------------------------
    def _fill(self) -> None:
        store = self.ctx.market
        self.list.clear()
        for app in store.apps:
            star = "★  " if app.name in marketplace.FEATURED else ""
            item = QListWidgetItem(f"{star}{app.title}")
            item.setData(NAME, app.name)
            self.list.addItem(item)
        self.count.setText(f"⚠ {store.error}" if store.error else f"{len(store.apps)} apps")
        self._filter(self.search.text())

    def _filter(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(bool(needle) and needle not in f"{item.text()} {item.data(NAME)}".lower())

    # -- detail ---------------------------------------------------------------------------
    def _set_enabled(self, on: bool) -> None:
        self.detail_body.setVisible(on)
        self.detail_empty.setVisible(not on)
        for widget in (
            self.install_btn,
            self.create_btn,
            self.save_btn,
            self.page_btn,
            self.repo,
            self.branch,
        ):
            widget.setEnabled(on)

    def _select(self, item: QListWidgetItem | None) -> None:
        if not item:
            return
        app = self.ctx.market.get(item.data(NAME))
        if not app:
            return
        self.current = app
        self.title.setText(app.title)
        self.tagline.setText("Loading details…")
        self.meta.setText(app.name)
        self.icon.set_url("", app.title)
        self._clear_versions()
        self.repo.setText(marketplace.KNOWN_REPOS.get(app.name, ""))
        self.repo_hint.setText("")
        self.branch.clear()
        self._set_enabled(True)
        if app.name in self.ctx.market.details:
            self._details_loaded(app.name)
        else:
            self.ctx.market.request_details(app)

    def _clear_versions(self) -> None:
        while self.versions.count():
            widget = self.versions.takeAt(0).widget()
            if widget:
                widget.deleteLater()

    def _details_loaded(self, name: str) -> None:
        if not self.current or self.current.name != name:
            return
        d = self.ctx.market.details[name]
        self.tagline.setText(d.tagline or "")
        bits = [
            b
            for b in (
                f"by {d.publisher}" if d.publisher else "",
                f"{d.installs} installs" if d.installs else "",
                " · ".join(d.categories),
            )
            if b
        ]
        self.meta.setText("   ·   ".join(bits))
        self.icon.set_url(d.icon_url, self.current.title)
        self._clear_versions()
        for version in d.supported_versions:
            self.versions.addWidget(Pill(version, "fm"))
        self.repo.setText(d.repo)
        hint = CONFIDENCE.get(d.repo_confidence, "")
        others = [link for link in d.github_links if link.lower() != d.repo.lower()]
        if others:
            hint += f"  Also linked: {', '.join(others[:3])}"
        self.repo_hint.setText(hint)
        self._load_branches(preferred=d.branches())

    def _load_branches(self, preferred: list[str] | None = None) -> None:
        try:
            org_repo = AppRef.parse(self.repo.text()).org_repo
        except ValueError:
            return
        if not org_repo:
            return
        wanted = self.ctx.settings.default_frappe_branch
        token = self.ctx.settings.github_token
        repo_at_request = self.repo.text()

        def fill(branches: list[str]) -> None:
            if self.repo.text() != repo_at_request:
                return
            self.branch.clear()
            self.branch.addItems(branches)
            match = wanted if wanted in branches else next((b for b in preferred or [] if b in branches), "")
            self.branch.setCurrentText(match)

        ssh = self.ctx.settings.git_over_ssh
        run_async(lambda: marketplace.list_branches(org_repo, token, ssh), fill)

    def _ref(self) -> AppRef | None:
        try:
            parsed = AppRef.parse(self.repo.text())
        except ValueError:
            return None
        return AppRef(parsed.repo, self.branch.currentText().strip(), parsed.subdir)

    # -- actions --------------------------------------------------------------------------
    def _install(self) -> None:
        ref = self._ref()
        if not ref:
            return
        picker = PickSiteDialog(self.ctx, f"Install {self.current.title} on…", self)
        if not run_dialog(picker):
            return
        bench = self.ctx.benches.get(picker.site())
        if bench:
            run_dialog(AddAppsDialog(self.ctx, bench, preselect=[ref], parent=self))

    def _new_site(self) -> None:
        ref = self._ref()
        if ref:
            self.ctx.new_site.emit([ref])

    def _save(self) -> None:
        ref = self._ref()
        if not ref or not self.current:
            return
        existing = self.ctx.catalog.find_marketplace(self.current.name)
        app = existing or CatalogApp(
            title=self.current.title, repo=ref.repo, marketplace_name=self.current.name
        )
        app.repo, app.ref, app.subdir = ref.repo, ref.ref, ref.subdir
        details = self.ctx.market.details.get(self.current.name)
        if details and details.tagline:
            app.description = details.tagline
        self.ctx.catalog.upsert(app)
        self.save_btn.setText("Saved ✓")

    def _open_page(self) -> None:
        if self.current:
            QDesktopServices.openUrl(QUrl(self.current.page_url))
