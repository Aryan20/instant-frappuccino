"""Build a native, self-contained bundle for the current OS.

    uv run python packaging/build.py            # macOS: dist/Instant Frappuccino.app + .dmg
                                                # Linux: dist/instant-frappuccino/ + a .tar.gz
PyInstaller can't cross-compile, so run this on each target OS (CI does both).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
ICONS = ROOT / "packaging" / "icons"
DIST = ROOT / "dist"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT / "packaging"))

from fmapp import APP_ID, APP_NAME, __version__  # noqa: E402

# Qt modules we never import; excluding them keeps the bundle small.
EXCLUDES = [
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtPdf",
    "PySide6.QtMultimedia",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "tkinter",
]


def render_icons() -> dict[str, Path]:
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    _app = QGuiApplication.instance() or QGuiApplication(["build"])
    renderer = QSvgRenderer(str(SRC / "fmapp" / "assets" / "icon.svg"))
    ICONS.mkdir(parents=True, exist_ok=True)

    def png(size: int, target: Path) -> Path:
        image = QImage(QSize(size, size), QImage.Format.Format_ARGB32)
        image.fill(0)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        image.save(str(target))
        return target

    out = {"png": png(512, ICONS / f"{APP_ID}.png")}
    if sys.platform == "darwin":
        iconset = ICONS / f"{APP_ID}.iconset"
        iconset.mkdir(exist_ok=True)
        for size in (16, 32, 128, 256, 512):
            png(size, iconset / f"icon_{size}x{size}.png")
            png(size * 2, iconset / f"icon_{size}x{size}@2x.png")
        icns = ICONS / f"{APP_ID}.icns"
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], check=True)
        out["icns"] = icns
    return out


def pyinstaller(icon: Path) -> None:
    sep = ";" if sys.platform == "win32" else ":"
    name = APP_NAME if sys.platform == "darwin" else APP_ID
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(ROOT / "packaging" / "entry.py"),
        "--name",
        name,
        "--windowed",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST),
        "--workpath",
        str(ROOT / "build"),
        "--paths",
        str(SRC),
        "--add-data",
        f"{SRC / 'fmapp' / 'assets'}{sep}fmapp/assets",
        "--icon",
        str(icon),
    ]
    for module in EXCLUDES:
        args += ["--exclude-module", module]
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", "com.rtcamp.instant-frappuccino"]
    subprocess.run(args, check=True, cwd=ROOT)


MACOS_MIN = "13.0"  # Ventura: covers the current macOS and the three releases before it


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def check_min_macos(app: Path) -> None:
    """Fail the build if any bundled binary needs a newer macOS than MACOS_MIN."""
    too_new = []
    for file in app.rglob("*"):
        if not file.is_file() or file.is_symlink():
            continue
        if file.suffix not in (".so", ".dylib") and not os.access(file, os.X_OK):
            continue
        out = subprocess.run(["vtool", "-show-build", str(file)], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "minos" and _version(parts[1]) > _version(MACOS_MIN):
                too_new.append(f"{file.relative_to(app)} (macOS {parts[1]})")
                break
    if too_new:
        listing = "\n  ".join(too_new[:20])
        sys.exit(f"These bundled binaries need a newer macOS than {MACOS_MIN}:\n  {listing}")
    print(f"All bundled binaries run on macOS {MACOS_MIN}+")


def finish_macos() -> Path:
    app = DIST / f"{APP_NAME}.app"
    plist = app / "Contents" / "Info.plist"
    # Version + dark-mode support; PyInstaller leaves these at defaults.
    for key, value in (("CFBundleShortVersionString", __version__), ("CFBundleVersion", __version__)):
        subprocess.run(["plutil", "-replace", key, "-string", value, str(plist)], check=True)
    subprocess.run(
        ["plutil", "-replace", "NSRequiresAquaSystemAppearance", "-bool", "NO", str(plist)], check=True
    )
    # Older macOS shows "requires macOS 13" instead of crashing on a too-new library.
    subprocess.run(
        ["plutil", "-replace", "LSMinimumSystemVersion", "-string", MACOS_MIN, str(plist)], check=True
    )
    check_min_macos(app)
    dmg = DIST / f"{APP_ID}-{__version__}-macos-{platform.machine()}.dmg"
    dmg.unlink(missing_ok=True)
    import dmg_art
    import dmgbuild

    art = ROOT / "build" / "dmg-art"
    art.mkdir(parents=True, exist_ok=True)
    background = dmg_art.render_background(art / "background.png", 1, APP_NAME, __version__)
    dmg_art.render_background(art / "background@2x.png", 2, APP_NAME, __version__)  # Retina
    # lookForHiDPI merges background.png + background@2x.png into one multi-resolution TIFF.
    dmgbuild.build_dmg(
        str(dmg),
        APP_NAME,
        settings=dmg_art.settings(app, background, ICONS / f"{APP_ID}.icns"),
        lookForHiDPI=True,
    )
    subprocess.run(["hdiutil", "verify", str(dmg)], check=True, capture_output=True)
    return dmg


def finish_linux(icon_png: Path) -> Path:
    bundle = DIST / APP_ID
    shutil.copy(ROOT / "packaging" / "linux" / f"{APP_ID}.desktop", bundle / f"{APP_ID}.desktop")
    shutil.copy(icon_png, bundle / f"{APP_ID}.png")
    installer = bundle / "install.sh"
    installer.write_text(
        "#!/bin/sh\n# Installs Instant Frappuccino for the current user (no root needed).\nset -e\n"
        'DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        'DEST="${XDG_DATA_HOME:-$HOME/.local/share}"\n'
        'mkdir -p "$HOME/.local/bin" "$DEST/applications" "$DEST/icons/hicolor/512x512/apps"\n'
        f'ln -sf "$DIR/{APP_ID}" "$HOME/.local/bin/{APP_ID}"\n'
        f'sed "s|^Exec=.*|Exec=$DIR/{APP_ID}|" "$DIR/{APP_ID}.desktop" '
        f'> "$DEST/applications/{APP_ID}.desktop"\n'
        f'cp "$DIR/{APP_ID}.png" "$DEST/icons/hicolor/512x512/apps/{APP_ID}.png"\n'
        'echo "Installed. Launch Instant Frappuccino from your app menu or run: instant-frappuccino"\n'
    )
    installer.chmod(0o755)
    archive = DIST / f"{APP_ID}-{__version__}-linux-{platform.machine()}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(bundle, arcname=APP_ID)
    return archive


def main() -> None:
    icons = render_icons()
    if sys.platform == "darwin":
        pyinstaller(icons["icns"])
        artifact = finish_macos()
    elif sys.platform.startswith("linux"):
        pyinstaller(icons["png"])
        artifact = finish_linux(icons["png"])
    else:
        sys.exit("Only macOS and Linux are supported targets.")
    print(f"\nBuilt {artifact}")


if __name__ == "__main__":
    main()
