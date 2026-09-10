"""Rasterise the app's star logo into the launcher's .icns bundle.

The logo lives in `src/anki_assistant/web/static/star.svg`, a trace of the 40x40
sprite: one `<rect>` per run of identical pixels. Painting it needs no SVG engine
and no image library — every rect is splatted onto the target buffer with exact
area coverage, which antialiases correctly at each icon size. `iconutil` (part of
macOS) packs the sizes into the .icns.

    uv run python scripts/app/make_icon.py scripts/app/img/icon.icns
"""

from __future__ import annotations

import math
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

SVG = Path(__file__).resolve().parent.parent.parent / "src/anki_assistant/web/static/star.svg"
MASTER = 1024  # the PNG kept next to the .icns, for previewing
SVG_NS = "{http://www.w3.org/2000/svg}"

Color = tuple[float, float, float]
Rect = tuple[int, int, int, int, Color]


def load_sprite(path: Path) -> tuple[int, list[Rect]]:
    """Return the sprite's grid size and its rectangles, in grid units."""
    root = ET.parse(path).getroot()
    _, _, width, height = (float(v) for v in (root.get("viewBox") or "").split())
    if width != height:
        raise ValueError(f"{path}: expected a square viewBox, got {width}x{height}")

    rects: list[Rect] = []
    for group in root.iter(SVG_NS + "g"):
        hex_code = (group.get("fill") or "#000000").lstrip("#")
        color = tuple(int(hex_code[i : i + 2], 16) / 255 for i in (0, 2, 4))
        for rect in group.iter(SVG_NS + "rect"):
            x, y, w, h = (int(float(rect.get(k, 0))) for k in ("x", "y", "width", "height"))
            rects.append((x, y, w, h, color))  # ty: ignore[invalid-argument-type]
    return int(width), rects


def render(grid: int, rects: list[Rect], size: int) -> bytearray:
    """Draw the sprite at `size` px and return the PNG bytes.

    Colours are accumulated premultiplied by coverage: the sprite's rectangles never
    overlap, so summing them is exact area averaging — no seams between neighbours.
    """
    scale = size / grid
    buf = [0.0] * (size * size * 4)
    for gx, gy, gw, gh, (r, g, b) in rects:
        x0, x1 = gx * scale, (gx + gw) * scale
        y0, y1 = gy * scale, (gy + gh) * scale
        for py in range(max(0, int(y0)), min(size, math.ceil(y1))):
            cover_y = min(y1, py + 1) - max(y0, py)
            if cover_y <= 0:
                continue
            for px in range(max(0, int(x0)), min(size, math.ceil(x1))):
                cover = (min(x1, px + 1) - max(x0, px)) * cover_y
                if cover <= 0:
                    continue
                i = (py * size + px) * 4
                buf[i] += r * cover
                buf[i + 1] += g * cover
                buf[i + 2] += b * cover
                buf[i + 3] += cover

    rows = bytearray()
    for y in range(size):
        rows.append(0)  # filter type: none
        for x in range(size):
            i = (y * size + x) * 4
            a = min(1.0, buf[i + 3])
            if a <= 0:
                rows += b"\x00\x00\x00\x00"
                continue
            rows += bytes(
                (
                    round(min(1.0, buf[i] / a) * 255),
                    round(min(1.0, buf[i + 1] / a) * 255),
                    round(min(1.0, buf[i + 2] / a) * 255),
                    round(a * 255),
                )
            )
    return png(size, rows)


def png(size: int, rows: bytearray) -> bytearray:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return bytearray(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "scripts/app/img/icon.icns").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    grid, rects = load_sprite(SVG)

    iconset = out.with_suffix(".iconset")
    subprocess.run(["rm", "-rf", str(iconset)], check=True)
    iconset.mkdir(parents=True)
    drawn: dict[int, bytearray] = {}
    for size in (16, 32, 128, 256, 512):
        for scale, suffix in ((1, ""), (2, "@2x")):
            pixels = size * scale
            if pixels not in drawn:
                drawn[pixels] = render(grid, rects, pixels)
            (iconset / f"icon_{size}x{size}{suffix}.png").write_bytes(drawn[pixels])
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    subprocess.run(["rm", "-rf", str(iconset)], check=True)

    master = out.with_name(f"icon-{MASTER}.png")
    master.write_bytes(drawn.get(MASTER) or render(grid, rects, MASTER))
    print(f"{master}\n{out}")


if __name__ == "__main__":
    main()
