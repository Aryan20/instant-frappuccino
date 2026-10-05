"""Artwork and Finder layout for the macOS installer image.

Finder draws icon labels black in light mode and white in dark mode (and they can't be hidden),
so the band behind the labels is a mid "latte" tone where both have ~4.6:1 contrast. Above it
an espresso header carries the title; the arrow sits on the darker part where cream reads well.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
    QTransform,
)

WIDTH, HEIGHT = 640, 420  # Finder window content size (points)
ICON_SIZE = 128
ICON_Y = 214  # icon centre line
APP_X, APPS_X = 170, 470  # icon centres

ESPRESSO = QColor("#2a1c15")
CREAM = QColor("#f6ede3")
FOAM = QColor("#e8d9c9")


def _font(size: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont()
    font.setFamilies([".AppleSystemUIFont", "SF Pro Display", "Helvetica Neue", "Arial"])
    font.setPointSizeF(size)
    font.setWeight(weight)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


def render_background(target: Path, scale: int, app_name: str, version: str) -> Path:
    _app = QGuiApplication.instance() or QGuiApplication(["dmg-art"])
    image = QImage(WIDTH * scale, HEIGHT * scale, QImage.Format.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(scale)
    painter = QPainter(image)
    painter.setRenderHints(
        QPainter.RenderHint.Antialiasing
        | QPainter.RenderHint.TextAntialiasing
        | QPainter.RenderHint.SmoothPixmapTransform
    )

    # Espresso → latte. Stop at ~0.72 is where Finder labels sit (luminance ≈ 0.18).
    base = QLinearGradient(0, 0, 0, HEIGHT)
    base.setColorAt(0.00, QColor("#24170f"))
    base.setColorAt(0.30, QColor("#3d2a1f"))
    base.setColorAt(0.55, QColor("#6b4f3f"))
    base.setColorAt(0.72, QColor("#8b6f5e"))
    base.setColorAt(1.00, QColor("#b2977f"))
    painter.fillRect(QRectF(0, 0, WIDTH, HEIGHT), base)

    # Soft "steam" glows for depth.
    for cx, cy, r, alpha in ((90, 40, 220, 34), (560, 70, 180, 22), (320, 470, 360, 40)):
        glow = QRadialGradient(QPointF(cx, cy), r)
        tint = QColor(FOAM)
        tint.setAlpha(alpha)
        glow.setColorAt(0.0, tint)
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(QRectF(0, 0, WIDTH, HEIGHT), glow)

    # Thin foam line under the header.
    line = QColor(FOAM)
    line.setAlpha(46)
    painter.setPen(QPen(line, 1))
    painter.drawLine(QPointF(48, 112), QPointF(WIDTH - 48, 112))

    # Title + tagline.
    painter.setPen(CREAM)
    painter.setFont(_font(25, QFont.Weight.Bold))
    painter.drawText(QRectF(0, 34, WIDTH, 40), Qt.AlignmentFlag.AlignHCenter, app_name)
    tagline = QColor(FOAM)
    tagline.setAlpha(190)
    painter.setPen(tagline)
    painter.setFont(_font(12.5))
    painter.drawText(
        QRectF(0, 74, WIDTH, 24),
        Qt.AlignmentFlag.AlignHCenter,
        "Frappe sites, locally — on top of fm, fmd and Docker",
    )

    # Curved arrow from the app towards Applications.
    start = QPointF(APP_X + ICON_SIZE / 2 + 18, ICON_Y)
    end = QPointF(APPS_X - ICON_SIZE / 2 - 22, ICON_Y)
    path = QPainterPath(start)
    path.cubicTo(QPointF(start.x() + 40, ICON_Y - 26), QPointF(end.x() - 40, ICON_Y - 26), end)
    arrow = QColor(CREAM)
    arrow.setAlpha(215)
    pen = QPen(arrow, 3.2, Qt.PenStyle.DashLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    pen.setDashPattern([2.4, 2.2])
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(path)
    # Arrowhead follows the curve's tangent at its end.
    head = QPainterPath(QPointF(-12, -9))
    head.lineTo(QPointF(1, 0))
    head.lineTo(QPointF(-12, 9))
    turn = QTransform()
    turn.translate(end.x(), end.y())
    turn.rotate(-path.angleAtPercent(1.0))  # Qt angles are counter-clockwise
    painter.setPen(
        QPen(arrow, 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    )
    painter.drawPath(turn.map(head))

    # Footer hint on the light part of the gradient.
    hint = QColor(ESPRESSO)
    hint.setAlpha(205)
    painter.setPen(hint)
    painter.setFont(_font(12, QFont.Weight.Medium))
    painter.drawText(
        QRectF(0, HEIGHT - 52, WIDTH, 20),
        Qt.AlignmentFlag.AlignHCenter,
        f"Drag {app_name} onto Applications to install",
    )
    version_color = QColor(ESPRESSO)
    version_color.setAlpha(140)
    painter.setPen(version_color)
    painter.setFont(_font(10.5))
    painter.drawText(
        QRectF(0, HEIGHT - 32, WIDTH, 18),
        Qt.AlignmentFlag.AlignHCenter,
        f"Version {version}  ·  Not signed yet: right-click › Open on first launch",
    )
    painter.end()
    image.save(str(target))
    return target


def settings(app: Path, background: Path, volume_icon: Path) -> dict:
    """dmgbuild settings: fixed window, no chrome, icons placed on the artwork."""
    return {
        "format": "UDZO",
        "filesystem": "HFS+",
        "files": [str(app)],
        "symlinks": {"Applications": "/Applications"},
        "icon": str(volume_icon),
        "background": str(background),
        "window_rect": ((200, 140), (WIDTH, HEIGHT)),
        "default_view": "icon-view",
        "show_status_bar": False,
        "show_tab_view": False,
        "show_toolbar": False,
        "show_pathbar": False,
        "show_sidebar": False,
        "show_icon_preview": False,
        "include_icon_view_settings": True,
        "arrange_by": None,
        "icon_size": ICON_SIZE,
        "text_size": 13,
        "label_pos": "bottom",
        "icon_locations": {app.name: (APP_X, ICON_Y), "Applications": (APPS_X, ICON_Y)},
    }
