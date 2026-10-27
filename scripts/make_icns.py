"""Render cullspeed/resources/icon.svg into a macOS .icns (uses Qt's SVG renderer and `iconutil`).

Usage: python scripts/make_icns.py OUTPUT.icns
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication, QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer

SVG = Path(__file__).resolve().parent.parent / "cullspeed" / "resources" / "icon.svg"
SIZES = (16, 32, 128, 256, 512)


def render(renderer: QSvgRenderer, size: int, path: Path):
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    image.save(str(path))


def main():
    output = Path(sys.argv[1]).resolve()
    app = QGuiApplication(["make_icns", "-platform", "offscreen"])  # noqa: F841 (QPainter needs an app)
    renderer = QSvgRenderer(str(SVG))
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for size in SIZES:
            render(renderer, size, iconset / f"icon_{size}x{size}.png")
            render(renderer, size * 2, iconset / f"icon_{size}x{size}@2x.png")
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(output)], check=True)


if __name__ == "__main__":
    main()
