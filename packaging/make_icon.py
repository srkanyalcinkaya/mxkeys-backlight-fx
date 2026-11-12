"""Draws the app icon and writes packaging/icon.icns (macOS only: needs `iconutil`)."""
import os
import subprocess
import sys

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QRadialGradient

HERE = os.path.dirname(os.path.abspath(__file__))


def draw(size: int) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 1024
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(100 * s, 100 * s, 824 * s, 824 * s), 185 * s, 185 * s)
    g = QLinearGradient(0, 100 * s, 0, 924 * s)
    g.setColorAt(0, QColor("#262a34"))
    g.setColorAt(1, QColor("#111318"))
    p.fillPath(bg, g)
    p.setClipPath(bg)
    glow = QRadialGradient(512 * s, 540 * s, 430 * s)
    glow.setColorAt(0, QColor(255, 210, 122, 150))
    glow.setColorAt(1, QColor(255, 210, 122, 0))
    p.fillRect(QRectF(0, 0, size, size), glow)
    kb = QPainterPath()
    kx, ky, kw, kh = 212 * s, 380 * s, 600 * s, 300 * s
    kb.addRoundedRect(QRectF(kx, ky, kw, kh), 48 * s, 48 * s)
    p.fillPath(kb, QColor("#2f333d"))
    rows, pad, gap = [8, 8, 7, 5], 36 * s, 16 * s
    rh = (kh - 2 * pad - gap * (len(rows) - 1)) / len(rows)
    for r, n in enumerate(rows):
        cw = (kw - 2 * pad - gap * (n - 1)) / n
        for c in range(n):
            key = QPainterPath()
            key.addRoundedRect(QRectF(kx + pad + c * (cw + gap), ky + pad + r * (rh + gap), cw, rh), 10 * s, 10 * s)
            p.fillPath(key, QColor(255, 240, 205))
    p.end()
    return img


def main():
    app = QGuiApplication(sys.argv)  # noqa: F841
    iconset = os.path.join(HERE, "icon.iconset")
    os.makedirs(iconset, exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        draw(base).save(os.path.join(iconset, f"icon_{base}x{base}.png"))
        draw(base * 2).save(os.path.join(iconset, f"icon_{base}x{base}@2x.png"))
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(HERE, "icon.icns")], check=True)
    for f in os.listdir(iconset):
        os.remove(os.path.join(iconset, f))
    os.rmdir(iconset)
    print("wrote", os.path.join(HERE, "icon.icns"))


if __name__ == "__main__":
    main()
