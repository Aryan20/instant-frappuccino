"""Look & feel: one spacing scale, one colour palette per scheme, one stylesheet.

Everything visual goes through here so pages only pick *roles* (``role="h1"``,
``kind="primary"``, ``card=True``) and the shared spacing constants below. The palette is
chosen from the effective colour scheme (system / light / dark) and re-applied when the OS
switches, so macOS Aqua and every Linux desktop get the same Frappe-UI-like result.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from fmapp.core import paths

# -- spacing scale (px) -------------------------------------------------------------------
XS, SM, MD, LG, XL, XXL = 4, 8, 12, 16, 24, 32
PAGE_MARGINS = (XXL, 28, XXL, 28)  # left, top, right, bottom of every page
SECTION_GAP = 20  # between blocks on a page
CARD_PADDING = 20
CARD_GAP = MD  # between elements inside a card
CONTROL_HEIGHT = 30
SIDEBAR_WIDTH = 232


@dataclass(frozen=True)
class Palette:
    canvas: str  # page background
    sidebar: str
    surface: str  # cards, tables, inputs
    subtle: str  # hover, header rows, secondary buttons
    border: str
    border_strong: str
    text: str
    muted: str
    faint: str
    primary_bg: str
    primary_fg: str
    primary_hover: str
    focus: str
    selection: str
    log_bg: str
    log_fg: str


LIGHT = Palette(
    canvas="#ffffff",
    sidebar="#f8f8f8",
    surface="#ffffff",
    subtle="#f3f3f3",
    border="#ececec",
    border_strong="#dcdcdc",
    text="#171717",
    muted="#6b6b6b",
    faint="#9a9a9a",
    primary_bg="#171717",
    primary_fg="#ffffff",
    primary_hover="#383838",
    focus="#2490ef",
    selection="#ebf3fd",
    log_bg="#111113",
    log_fg="#e5e7eb",
)
DARK = Palette(
    canvas="#171717",
    sidebar="#1e1e1e",
    surface="#1f1f1f",
    subtle="#2a2a2a",
    border="#2f2f2f",
    border_strong="#3d3d3d",
    text="#f1f1f1",
    muted="#a3a3a3",
    faint="#7a7a7a",
    primary_bg="#f1f1f1",
    primary_fg="#171717",
    primary_hover="#d4d4d4",
    focus="#4aa3ff",
    selection="#1d3550",
    log_bg="#0c0c0d",
    log_fg="#e5e7eb",
)

STATUS_COLORS = {
    "running": ("#16a34a", "rgba(22,163,74,0.13)"),
    "partial": ("#d97706", "rgba(217,119,6,0.14)"),
    "stopped": ("#737373", "rgba(115,115,115,0.14)"),
    "unknown": ("#737373", "rgba(115,115,115,0.14)"),
    "broken": ("#dc2626", "rgba(220,38,38,0.12)"),
    "queued": ("#737373", "rgba(115,115,115,0.14)"),
    "succeeded": ("#16a34a", "rgba(22,163,74,0.13)"),
    "failed": ("#dc2626", "rgba(220,38,38,0.12)"),
    "cancelled": ("#737373", "rgba(115,115,115,0.14)"),
    "fm": ("#2563eb", "rgba(37,99,235,0.12)"),
    "deployer": ("#7c3aed", "rgba(124,58,237,0.12)"),
}

current: Palette = LIGHT


def _chevron(color: str) -> str:
    """Combo-box arrow as an SVG file (QSS can only reference images by path)."""
    path = paths.cache_dir() / f"chevron-{color.lstrip('#')}.svg"
    if not path.exists():
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="16" height="16">'
            f'<path d="M4.5 6.5 8 10l3.5-3.5" fill="none" stroke="{color}" stroke-width="1.6" '
            'stroke-linecap="round" stroke-linejoin="round"/></svg>'
        )
    return path.as_posix()


def stylesheet(p: Palette) -> str:
    h = CONTROL_HEIGHT
    return f"""
* {{ outline: 0; }}
QMainWindow, QDialog {{ background: {p.canvas}; }}
QStackedWidget#Content {{ background: {p.canvas}; }}
QScrollArea, QScrollArea > QWidget#qt_scrollarea_viewport {{ background: transparent; border: none; }}
QToolTip {{ background: {p.text}; color: {p.canvas}; border: none; padding: 5px 8px; border-radius: 6px; }}
QStatusBar {{ background: {p.canvas}; color: {p.muted}; border-top: 1px solid {p.border}; }}
QStatusBar::item {{ border: none; }}

/* sidebar */
#Sidebar {{ background: {p.sidebar}; border-right: 1px solid {p.border}; }}
#Sidebar QListWidget {{ background: transparent; border: none; font-size: 13px; }}
#Sidebar QListWidget::item {{ padding: 7px 10px; margin: 1px 0; border-radius: 7px; color: {p.text}; }}
#Sidebar QListWidget::item:selected {{ background: {p.surface}; color: {p.text};
    border: 1px solid {p.border}; font-weight: 600; }}
#Sidebar QListWidget::item:hover:!selected {{ background: {p.subtle}; }}
#Brand {{ font-size: 15px; font-weight: 700; color: {p.text}; }}
#BrandSub {{ color: {p.muted}; font-size: 12px; }}
#SidebarSection {{ color: {p.faint}; font-size: 11px; font-weight: 600; }}
QLabel#StatusName {{ color: {p.text}; font-size: 12px; }}
QLabel#StatusValue {{ color: {p.muted}; font-size: 12px; }}

/* typography */
QLabel {{ color: {p.text}; }}
QLabel[role="h1"] {{ font-size: 22px; font-weight: 700; }}
QLabel[role="h2"] {{ font-size: 15px; font-weight: 600; }}
QLabel[role="h3"] {{ font-size: 13px; font-weight: 600; }}
QLabel[role="muted"] {{ color: {p.muted}; }}
QLabel[role="faint"] {{ color: {p.faint}; font-size: 12px; }}
QLabel[role="key"] {{ color: {p.muted}; }}
QLabel[role="mono"] {{ font-family: Menlo, "DejaVu Sans Mono", monospace; font-size: 12px; }}
QLabel[role="error"] {{ color: #dc2626; }}
QLabel a {{ color: {p.focus}; }}

/* surfaces */
QFrame[card="true"] {{ background: {p.surface}; border: 1px solid {p.border}; border-radius: 12px; }}
QFrame[banner="warn"] {{ background: rgba(217,119,6,0.10); border: 1px solid rgba(217,119,6,0.35);
    border-radius: 10px; }}
QFrame[banner="info"] {{ background: rgba(37,99,235,0.08); border: 1px solid rgba(37,99,235,0.30);
    border-radius: 10px; }}
QFrame[choice="true"] {{ background: {p.surface}; border: 1px solid {p.border_strong}; border-radius: 12px; }}
QFrame[choice="true"]:hover {{ border-color: {p.faint}; }}
QFrame[choice="true"][selected="true"] {{ border: 2px solid {p.text}; }}
QFrame#Divider {{ background: {p.border}; max-height: 1px; min-height: 1px; border: none; }}

/* buttons */
QPushButton {{
    background: {p.subtle}; color: {p.text}; border: 1px solid {p.border};
    border-radius: 8px; padding: 0 14px; min-height: {h}px; max-height: {h}px; font-size: 13px;
}}
QPushButton:hover {{ background: {p.border}; }}
QPushButton:pressed {{ background: {p.border_strong}; }}
QPushButton:disabled {{ color: {p.faint}; background: {p.subtle}; border-color: {p.border}; }}
QPushButton:checked {{ background: {p.border_strong}; }}
QPushButton[kind="primary"] {{ background: {p.primary_bg}; color: {p.primary_fg}; border: none;
    font-weight: 600; }}
QPushButton[kind="primary"]:hover {{ background: {p.primary_hover}; }}
QPushButton[kind="primary"]:disabled {{ background: {p.border_strong}; color: {p.surface}; }}
QPushButton[kind="danger"] {{ background: #dc2626; color: white; border: none; font-weight: 600; }}
QPushButton[kind="danger"]:hover {{ background: #b91c1c; }}
QPushButton[kind="danger"]:disabled {{ background: rgba(220,38,38,0.35); }}
QPushButton[kind="ghost"] {{ background: transparent; border: none; color: {p.muted}; padding: 0 6px; }}
QPushButton[kind="ghost"]:hover {{ color: {p.text}; background: {p.subtle}; }}

/* inputs */
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{
    background: {p.surface}; color: {p.text}; border: 1px solid {p.border_strong};
    border-radius: 8px; selection-background-color: {p.focus}; selection-color: white;
}}
QLineEdit, QSpinBox, QComboBox {{ min-height: {h - 2}px; padding: 0 10px; }}
QPlainTextEdit {{ padding: 6px 8px; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border-color: {p.focus}; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {p.faint}; background: {p.subtle}; }}
QComboBox {{ padding-right: 28px; }}
QComboBox::drop-down {{ border: none; width: 26px; subcontrol-origin: padding;
    subcontrol-position: center right; }}
QComboBox::down-arrow {{ image: url("{_chevron(p.muted)}"); width: 14px; height: 14px; }}
QComboBox QAbstractItemView {{ background: {p.surface}; color: {p.text}; border: 1px solid {p.border};
    selection-background-color: {p.selection}; selection-color: {p.text}; padding: 4px; outline: 0; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 16px; border: none; background: transparent; }}
QCheckBox, QRadioButton {{ color: {p.text}; spacing: 8px; min-height: 24px; }}

/* lists & tables */
QTreeWidget, QTableWidget, QListWidget {{
    background: {p.surface}; color: {p.text}; border: 1px solid {p.border}; border-radius: 10px;
    padding: 2px;
}}
QTreeView::item, QListView::item {{ padding: 6px 4px; border: none; }}
QTreeView::item:hover, QListView::item:hover {{ background: {p.subtle}; }}
QTreeView::item:selected, QListView::item:selected, QTableView::item:selected {{
    background: {p.selection}; color: {p.text}; }}
QTableView {{ gridline-color: {p.border}; }}
QTableView::item {{ padding: 4px 6px; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{
    background: {p.surface}; color: {p.muted}; border: none; border-bottom: 1px solid {p.border};
    padding: 8px 10px; font-size: 12px; font-weight: 600;
}}
QTableCornerButton::section {{ background: {p.surface}; border: none; }}

/* tabs — left-aligned underline, Frappe style */
QTabWidget::pane {{ border: none; border-top: 1px solid {p.border}; top: -1px; background: transparent; }}
QTabWidget::tab-bar {{ alignment: left; left: 0; }}
QTabBar {{ qproperty-drawBase: 0; background: transparent; }}
QTabBar::tab {{ background: transparent; color: {p.muted}; border: none; border-bottom: 2px solid transparent;
    padding: 8px 2px 9px 2px; margin-right: 22px; font-size: 13px; }}
QTabBar::tab:hover {{ color: {p.text}; }}
QTabBar::tab:selected {{ color: {p.text}; border-bottom: 2px solid {p.text}; font-weight: 600; }}

/* group boxes (dialogs): a quiet section heading, not a framed box */
QGroupBox {{ border: none; border-top: 1px solid {p.border}; margin-top: 26px; padding: 12px 0 0 0;
    background: transparent; }}
QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left; left: 0; top: 0;
    color: {p.text}; font-weight: 600; font-size: 13px; }}

/* splitter, scrollbars, menus */
QSplitter::handle {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p.border_strong}; border-radius: 3px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p.border_strong}; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: none; border: none; width: 0; height: 0; }}

QPlainTextEdit#LogView {{
    font-family: Menlo, "DejaVu Sans Mono", monospace; font-size: 12px;
    background: {p.log_bg}; color: {p.log_fg}; border: none; border-radius: 10px; padding: 10px 12px;
    selection-background-color: #374151;
}}
"""


def pill_style(key: str) -> str:
    fg, bg = STATUS_COLORS.get(key, STATUS_COLORS["unknown"])
    return (
        f"QLabel {{ color: {fg}; background: {bg}; border-radius: 9px; padding: 2px 10px;"
        f" font-size: 11px; font-weight: 600; min-height: 16px; max-height: 18px; }}"
    )


def _is_dark(app: QApplication) -> bool:
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


_scheme_hooked = False


def apply(app: QApplication, theme: str = "system") -> None:
    global _scheme_hooked
    if sys.platform.startswith("linux"):
        # Fusion renders consistently across GNOME/KDE/xfce; our stylesheet does the rest.
        app.setStyle("Fusion")
    hints = QGuiApplication.styleHints()
    scheme = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(theme, Qt.ColorScheme.Unknown)
    hints.setColorScheme(scheme)
    if not _scheme_hooked:  # re-style when the OS switches light/dark
        hints.colorSchemeChanged.connect(lambda _s: _restyle(app))
        _scheme_hooked = True
    font = app.font()
    if sys.platform == "darwin":
        font.setPointSize(13)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
    _restyle(app)


def _restyle(app: QApplication) -> None:
    global current
    current = DARK if _is_dark(app) else LIGHT
    app.setStyleSheet(stylesheet(current))
