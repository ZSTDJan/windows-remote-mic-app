"""Generate the Windows shortcut/executable ICO from the shared SVG icon."""

from __future__ import annotations

import argparse
from pathlib import Path
import struct

from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _render_png(renderer: QSvgRenderer, size: int) -> bytes:
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()

    payload = QByteArray()
    buffer = QBuffer(payload)
    if not buffer.open(QIODevice.WriteOnly) or not image.save(buffer, "PNG"):
        raise RuntimeError(f"could not render {size}x{size} application icon")
    buffer.close()
    return bytes(payload)


def build_ico(svg_path: Path) -> bytes:
    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        raise ValueError(f"invalid SVG icon: {svg_path}")

    images = [(size, _render_png(renderer, size)) for size in ICON_SIZES]
    offset = 6 + 16 * len(images)
    entries = []
    payloads = []
    for size, payload in images:
        dimension = 0 if size == 256 else size
        entries.append(
            struct.pack(
                "<BBBBHHII",
                dimension,
                dimension,
                0,
                0,
                1,
                32,
                len(payload),
                offset,
            )
        )
        payloads.append(payload)
        offset += len(payload)
    return b"".join((struct.pack("<HHH", 0, 1, len(images)), *entries, *payloads))


def main() -> int:
    rc003_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=rc003_root
        / "src"
        / "ovb_rc003"
        / "assets"
        / "icons"
        / "remote-mic-connected.svg",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=rc003_root
        / "src"
        / "ovb_rc003"
        / "assets"
        / "icons"
        / "remote-mic.ico",
    )
    args = parser.parse_args()

    content = build_ico(args.source.resolve())
    output = args.output.resolve()
    if output.is_file() and output.read_bytes() == content:
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
