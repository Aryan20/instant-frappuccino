"""Build assets: the whole bench or chosen apps (``bench build [--apps …]``)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QRadioButton,
    QVBoxLayout,
)

from fmapp.core.models import Bench, BenchKind
from fmapp.ui import theme
from fmapp.ui.context import AppContext
from fmapp.ui.widgets import button, fit_height, label, tidy_view


class BuildDialog(QDialog):
    def __init__(
        self, ctx: AppContext, bench: Bench, preselect: list[str] | None = None, parent=None
    ) -> None:
        super().__init__(parent)
        self.ctx, self.bench = ctx, bench
        self.setWindowTitle(f"Build {bench.name}")
        self.setMinimumWidth(560)
        box = QVBoxLayout(self)
        box.setContentsMargins(theme.XL, theme.XL, theme.XL, theme.LG)
        box.setSpacing(theme.MD)
        box.addWidget(label(f"Build assets · {bench.name}", "h2"))
        box.addWidget(
            label(
                "Compiles JS and CSS with `bench build`. Build only the apps you changed to save time.",
                "muted",
                wrap=True,
            )
        )

        self.whole = QRadioButton("Whole bench (all apps)")
        self.some = QRadioButton("Selected apps")
        group = QButtonGroup(self)
        group.addButton(self.whole)
        group.addButton(self.some)
        box.addWidget(self.whole)
        box.addWidget(self.some)

        self.apps = QListWidget()
        tidy_view(self.apps)
        chosen = set(preselect or [])
        for app in bench.apps:
            item = QListWidgetItem(f"{app.name}   {app.version}".rstrip())
            item.setData(Qt.ItemDataRole.UserRole, app.name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if app.name in chosen else Qt.CheckState.Unchecked)
            self.apps.addItem(item)
        fit_height(self.apps, self.apps.count(), max_rows=8)
        box.addWidget(self.apps)

        self.production = QCheckBox("Production mode (minified, even with developer mode on)")
        self.force = QCheckBox("Force — build Frappe's assets locally instead of downloading them")
        self.clear_cache = QCheckBox("Clear the site cache afterwards")
        self.clear_cache.setChecked(True)
        for widget in (self.production, self.force, self.clear_cache):
            box.addWidget(widget)
        if bench.kind is BenchKind.DEPLOYER:
            box.addWidget(
                label(
                    "This site uses releases: building here updates the active release only. "
                    "Deploy now builds a fresh release instead.",
                    "faint",
                    wrap=True,
                )
            )

        box.addWidget(label("Command", "h3"))
        self.preview = QPlainTextEdit()
        self.preview.setObjectName("LogView")
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(120)
        box.addWidget(self.preview)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.ok = button("Build", "primary", on_click=self._build)
        buttons.addButton(self.ok, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

        (self.some if chosen else self.whole).setChecked(True)
        for signal in (
            self.whole.toggled,
            self.production.toggled,
            self.force.toggled,
            self.clear_cache.toggled,
        ):
            signal.connect(self._sync)
        self.apps.itemChanged.connect(lambda _i: self._sync())
        self._sync()

    def selected_apps(self) -> list[str]:
        if self.whole.isChecked():
            return []
        return [
            self.apps.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.apps.count())
            if self.apps.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _job(self):
        return self.ctx.ops.build(
            self.bench.name,
            self.selected_apps(),
            self.production.isChecked(),
            self.force.isChecked(),
            self.clear_cache.isChecked(),
        )

    def _sync(self, *_args) -> None:
        self.apps.setEnabled(self.some.isChecked())
        nothing = self.some.isChecked() and not self.selected_apps()
        self.ok.setEnabled(not nothing)
        try:
            text = "Pick at least one app." if nothing else self._job().preview()
        except Exception as exc:  # missing fm, invalid name
            text = str(exc)
            self.ok.setEnabled(False)
        self.preview.setPlainText(text)

    def _build(self) -> None:
        if self.ctx.submit(self, lambda _ops: self._job(), show=True):
            self.accept()
