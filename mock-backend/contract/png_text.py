"""A PNG that reads a short text, drawn with a 5x7 bitmap font. Standard library
only, so the model contract suite can send a real image without Pillow.

    from png_text import png_text
    data = png_text("NAPKIN 42")          # bytes of a PNG, black on white
"""

from __future__ import annotations

import struct
import zlib

_FONT = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    " ": ["00000"] * 7,
}


def png_text(text: str, scale: int = 12, margin: int = 3) -> bytes:
    """Black text on white, 8-bit greyscale."""
    glyphs = [_FONT[c] for c in text.upper()]
    cols = 6 * len(glyphs) - 1 + 2 * margin
    rows = 7 + 2 * margin
    grid = [[0] * cols for _ in range(rows)]
    x = margin
    for g in glyphs:
        for r, line in enumerate(g):
            for c, bit in enumerate(line):
                if bit == "1":
                    grid[margin + r][x + c] = 1
        x += 6
    w, h = cols * scale, rows * scale
    raw = bytearray()
    for r in range(rows):
        line = bytearray([0])  # filter type 0
        for c in range(cols):
            line += (b"\x00" if grid[r][c] else b"\xff") * scale
        raw += bytes(line) * scale

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))
