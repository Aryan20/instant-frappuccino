"""Small reusable widgets."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from fmapp.core import marketplace
from fmapp.ui import theme
from fmapp.ui.async_ import run_async


def label(text: str = "", role: str = "", wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if role:
        widget.setProperty("role", role)
    widget.setWordWrap(wrap)
    if role == "mono":
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


def button(text: str, kind: str = "", on_click=None, tooltip: str = "") -> QPushButton:
    widget = QPushButton(text)
    if kind:
        widget.setProperty("kind", kind)
    if on_click:
        widget.clicked.connect(on_click)
    if tooltip:
        widget.setToolTip(tooltip)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    return widget


class Pill(QLabel):
    def __init__(self, text: str = "", key: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.set(text, key or text)

    def set(self, text: str, key: str = "") -> None:
        self.setText(text)
        self.setStyleSheet(theme.pill_style(key or text.lower()))


class Card(QFrame):
    def __init__(
        self,
        parent: QWidget | None = None,
        margins: int = theme.CARD_PADDING,
        title: str = "",
        subtitle: str = "",
    ) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(margins, margins - 2, margins, margins)
        self.body.setSpacing(theme.CARD_GAP)
        self.actions: QHBoxLayout | None = None
        if title:
            self.actions = self.header(title, subtitle)

    def header(self, title: str, subtitle: str = "") -> QHBoxLayout:
        """Title (+ optional subtitle) on the left, an actions row on the right."""
        row = QHBoxLayout()
        row.setSpacing(theme.SM)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(label(title, "h2"))
        if subtitle:
            text.addWidget(label(subtitle, "muted", wrap=True))
        row.addLayout(text, 1)
        actions = QHBoxLayout()
        actions.setSpacing(theme.SM)
        row.addLayout(actions)
        self.body.addLayout(row)
        self.body.addSpacing(2)
        return actions


class Banner(QFrame):
    """Inline notice with optional action button."""

    def __init__(self, kind: str = "warn", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("banner", kind)
        row = QHBoxLayout(self)
        row.setContentsMargins(theme.LG, theme.MD, theme.MD, theme.MD)
        row.setSpacing(theme.MD)
        self.text = label(wrap=True)
        row.addWidget(self.text, 1)
        self.action = button("")
        self.action.hide()
        row.addWidget(self.action)
        self._handler = None

    def show_message(self, text: str, action: str = "", handler=None) -> None:
        self.text.setText(text)
        if self._handler:
            self.action.clicked.disconnect(self._handler)
            self._handler = None
        if action and handler:
            self.action.setText(action)
            self.action.clicked.connect(handler)
            self._handler = handler
            self.action.show()
        else:
            self.action.hide()
        self.show()


class ChoiceCard(QFrame):
    """A large clickable option (used for "Frappe Manager" vs "Frappe Deployer")."""

    clicked = Signal()

    def __init__(self, title: str, subtitle: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("choice", True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        box = QVBoxLayout(self)
        box.setContentsMargins(theme.LG + 2, theme.LG, theme.LG + 2, theme.LG)
        box.setSpacing(6)
        self.title = label(title, "h2")
        self.subtitle = label(subtitle, "muted", wrap=True)
        box.addWidget(self.title)
        box.addWidget(self.subtitle)
        self.note = label("", wrap=True)
        self.note.hide()
        box.addWidget(self.note)
        box.addStretch()

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self.isEnabled():
            self.clicked.emit()
        super().mousePressEvent(event)


class LogView(QPlainTextEdit):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LogView")
        self.setReadOnly(True)
        self.setMaximumBlockCount(20000)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

    def append_text(self, text: str) -> None:
        bar = self.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)
        if at_bottom:
            bar.setValue(bar.maximum())


_icon_cache: dict[str, QPixmap] = {}


class AppIcon(QLabel):
    """Marketplace app icon, fetched in the background and cached."""

    def __init__(self, size: int = 48, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self._url = ""
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            f"border-radius: {size // 5}px; background: {theme.current.subtle}; color: {theme.current.muted};"
            f" font-size: {size // 2}px; font-weight: 600;"
        )

    def set_url(self, url: str, fallback: str = "") -> None:
        self._url = url
        self.clear()
        self.setText(fallback[:1].upper())
        if not url:
            return
        if url in _icon_cache:
            self._show(_icon_cache[url])
            return

        def loaded(data: bytes | None, wanted: str = url) -> None:
            if not data:
                return
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                _icon_cache[wanted] = pixmap
                if self._url == wanted:
                    self._show(pixmap)

        run_async(lambda: marketplace.fetch_icon(url), loaded)

    def _show(self, pixmap: QPixmap) -> None:
        ratio = self.devicePixelRatioF()
        scaled = pixmap.scaled(
            int(self._size * ratio),
            int(self._size * ratio),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(ratio)
        self.setText("")
        self.setPixmap(scaled)


def hline() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    return line


def scroll_page(parent_layout: QVBoxLayout | None = None) -> tuple[QScrollArea, QWidget]:
    """A vertically scrolling page whose content keeps the canvas colour."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    body = QWidget()
    scroll.setWidget(body)
    # QScrollArea.setWidget() turns on autoFillBackground, painting the window grey.
    body.setAutoFillBackground(False)
    scroll.viewport().setAutoFillBackground(False)
    if parent_layout is not None:
        parent_layout.addWidget(scroll)
    return scroll, body


def align_forms(*forms: QFormLayout) -> None:
    """Give every label column the same width so stacked forms line up."""
    labels = []
    for form in forms:
        for row_index in range(form.rowCount()):
            item = form.itemAt(row_index, QFormLayout.ItemRole.LabelRole)
            if item and item.widget():
                labels.append(item.widget())
    width = max((w.sizeHint().width() for w in labels), default=0)
    for widget in labels:
        widget.setMinimumWidth(width)


def splitter(*panes: QWidget, sizes: tuple[int, ...] = ()) -> QSplitter:
    """Side-by-side panes with a real gutter (handle width can't be set from a stylesheet)."""
    split = QSplitter(Qt.Orientation.Horizontal)
    split.setHandleWidth(theme.SECTION_GAP)
    split.setChildrenCollapsible(False)
    for pane in panes:
        split.addWidget(pane)
    if sizes:
        split.setSizes(list(sizes))
    return split


def empty_state(title: str, text: str, action: QPushButton | None = None) -> QWidget:
    """Centred placeholder shown instead of an empty list / panel."""
    widget = QWidget()
    box = QVBoxLayout(widget)
    box.setSpacing(theme.SM)
    box.addStretch()
    width = 460
    for item in (label(title, "h2"), label(text, "muted", wrap=True)):
        item.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        # An aligned, word-wrapped label isn't given its wrapped height; size it explicitly.
        item.setFixedWidth(width)
        item.setMinimumHeight(item.heightForWidth(width))
        box.addWidget(item, alignment=Qt.AlignmentFlag.AlignHCenter)
    if action is not None:
        box.addSpacing(theme.XS)
        box.addWidget(action, alignment=Qt.AlignmentFlag.AlignHCenter)
    box.addStretch()
    return widget


def run_dialog(dialog: QDialog) -> int:
    """exec() a modal dialog, then free it — dialogs are parented to long-lived windows."""
    try:
        return dialog.exec()
    finally:
        dialog.deleteLater()  # deferred: callers can still read the dialog's fields afterwards


def pick_fmd_config(parent: QWidget) -> str:
    """File dialog for an fmd ``site.toml``; returns the chosen path or ``""``."""
    path, _ = QFileDialog.getOpenFileName(
        parent, "Import fmd config", str(Path.home()), "fmd config (*.toml);;All files (*)"
    )
    return path


def form_layout(parent: QWidget | None = None) -> QFormLayout:
    """Form with fields that grow (macOS defaults to fixed-width fields) and even rhythm."""
    form = QFormLayout(parent) if parent is not None else QFormLayout()
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    form.setHorizontalSpacing(theme.LG)
    form.setVerticalSpacing(theme.MD - 2)
    return form


def tidy_view(view: QAbstractItemView) -> None:
    """Shared look for lists/tables: no stripes, roomy rows, no focus rectangles."""
    view.setAlternatingRowColors(False)
    view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    if hasattr(view, "setUniformRowHeights"):
        view.setUniformRowHeights(True)


def fit_height(view: QAbstractItemView, rows: int, max_rows: int = 14, row_height: int = 0) -> None:
    """Size a table to its rows so short lists don't leave a sea of empty space."""
    header = view.header() if hasattr(view, "header") else None
    head = header.sizeHint().height() if header is not None and not header.isHidden() else 0
    per_row = row_height or (view.sizeHintForRow(0) if rows else 36) or 36
    shown = max(1, min(rows, max_rows))
    view.setFixedHeight(head + per_row * shown + 8)


class StatusRow(QWidget):
    """Sidebar health line: coloured dot · name · value."""

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 3, 6, 3)
        layout.setSpacing(theme.SM)
        self.dot = QLabel("●")
        self.dot.setFixedWidth(10)
        self.name = QLabel(name)
        self.name.setObjectName("StatusName")
        self.value = QLabel("…")
        self.value.setObjectName("StatusValue")
        layout.addWidget(self.dot)
        layout.addWidget(self.name, 1)
        layout.addWidget(self.value)
        self.set("…", "unknown")

    def set(self, text: str, key: str) -> None:
        fg, _bg = theme.STATUS_COLORS.get(key, theme.STATUS_COLORS["unknown"])
        self.dot.setStyleSheet(f"color: {fg}; font-size: 9px;")
        self.value.setText(text)


def page_header(title: str, subtitle: str = "") -> tuple[QWidget, QHBoxLayout]:
    """Title block with a right-aligned action area; returns (widget, actions_layout)."""
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, theme.XS)
    layout.setSpacing(theme.LG)
    text = QVBoxLayout()
    text.setSpacing(theme.XS)
    text.addWidget(label(title, "h1"))
    if subtitle:
        text.addWidget(label(subtitle, "muted", wrap=True))
    layout.addLayout(text, 1)
    actions = QHBoxLayout()
    actions.setSpacing(theme.SM)
    layout.addLayout(actions)
    layout.setAlignment(actions, Qt.AlignmentFlag.AlignVCenter)
    return widget, actions
