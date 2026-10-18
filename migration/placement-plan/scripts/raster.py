"""Minimal RGB raster with a 5x7 bitmap font and a PNG writer (standard library only).

Deterministic: the same drawing calls always produce the same bytes
(zlib level 9, PNG filter 0 on every row, no timestamps or text chunks).
"""

from __future__ import annotations

import struct
import zlib

# 5x7 glyphs, one string of five '#'/'.' cells per row (uppercase only).
FONT = {
    "A": [".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "B": ["####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."],
    "C": [".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."],
    "D": ["####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."],
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#...."],
    "G": [".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".####"],
    "H": ["#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"],
    "I": [".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "J": ["..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."],
    "K": ["#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"],
    "L": ["#....", "#....", "#....", "#....", "#....", "#....", "#####"],
    "M": ["#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"],
    "N": ["#...#", "#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#"],
    "O": [".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "P": ["####.", "#...#", "#...#", "####.", "#....", "#....", "#...."],
    "Q": [".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"],
    "R": ["####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"],
    "S": [".####", "#....", "#....", ".###.", "....#", "....#", "####."],
    "T": ["#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."],
    "U": ["#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."],
    "V": ["#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."],
    "W": ["#...#", "#...#", "#...#", "#.#.#", "#.#.#", "#.#.#", ".#.#."],
    "X": ["#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"],
    "Y": ["#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."],
    "Z": ["#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"],
    "0": [".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."],
    "1": ["..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "2": [".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"],
    "3": ["#####", "...#.", "..#..", "...#.", "....#", "#...#", ".###."],
    "4": ["...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."],
    "5": ["#####", "#....", "####.", "....#", "....#", "#...#", ".###."],
    "6": ["..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."],
    "7": ["#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."],
    "8": [".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."],
    "9": [".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."],
    " ": [".....", ".....", ".....", ".....", ".....", ".....", "....."],
    "#": [".#.#.", ".#.#.", "#####", ".#.#.", "#####", ".#.#.", ".#.#."],
    ",": [".....", ".....", ".....", ".....", ".##..", "..#..", ".#..."],
    "-": [".....", ".....", ".....", "#####", ".....", ".....", "....."],
    ".": [".....", ".....", ".....", ".....", ".....", ".##..", ".##.."],
    "+": [".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."],
    "(": ["...#.", "..#..", ".#...", ".#...", ".#...", "..#..", "...#."],
    ")": [".#...", "..#..", "...#.", "...#.", "...#.", "..#..", ".#..."],
    "/": [".....", "....#", "...#.", "..#..", ".#...", "#....", "....."],
    ":": [".....", ".##..", ".##..", ".....", ".##..", ".##..", "....."],
    "=": [".....", ".....", "#####", ".....", "#####", ".....", "....."],
    ";": [".....", ".##..", ".##..", ".....", ".##..", "..#..", ".#..."],
}
GLYPH_W, GLYPH_H = 5, 7


class Canvas:
    def __init__(self, width: int, height: int, background):
        self.w, self.h = width, height
        self.buf = bytearray(bytes(background) * (width * height))

    def put(self, x: int, y: int, color):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = 3 * (y * self.w + x)
            self.buf[i:i + 3] = bytes(color)

    def rect(self, x0: int, y0: int, x1: int, y1: int, color):
        """Fill the inclusive rectangle [x0, x1] x [y0, y1]."""
        x0, x1 = max(0, x0), min(self.w - 1, x1)
        if x0 > x1:
            return
        row = bytes(color) * (x1 - x0 + 1)
        for y in range(max(0, y0), min(self.h - 1, y1) + 1):
            i = 3 * (y * self.w + x0)
            self.buf[i:i + len(row)] = row

    def outline(self, x0: int, y0: int, x1: int, y1: int, color):
        self.rect(x0, y0, x1, y0, color)
        self.rect(x0, y1, x1, y1, color)
        self.rect(x0, y0, x0, y1, color)
        self.rect(x1, y0, x1, y1, color)

    def diamond(self, cx: int, cy: int, r: int, color, ring=None):
        if ring is not None:
            self.diamond(cx, cy, r + 1, ring)
        for dy in range(-r, r + 1):
            k = r - abs(dy)
            self.rect(cx - k, cy + dy, cx + k, cy + dy, color)

    def cross(self, cx: int, cy: int, arm: int, color):
        self.rect(cx - arm, cy, cx + arm, cy, color)
        self.rect(cx, cy - arm, cx, cy + arm, color)

    def text(self, x: int, y: int, s: str, color, scale: int = 1, halo=None) -> int:
        """Draw `s` (uppercased) with its top-left at (x, y); returns the advance width."""
        s = s.upper()
        if halo is not None:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx or dy:
                        self.text(x + dx, y + dy, s, halo, scale)
        cx = x
        for ch in s:
            g = FONT.get(ch, FONT[" "])
            for gy, row in enumerate(g):
                for gx, cell in enumerate(row):
                    if cell == "#":
                        self.rect(cx + gx * scale, y + gy * scale, cx + gx * scale + scale - 1,
                                  y + gy * scale + scale - 1, color)
            cx += (GLYPH_W + 1) * scale
        return cx - x

    @staticmethod
    def text_width(s: str, scale: int = 1) -> int:
        return len(s) * (GLYPH_W + 1) * scale

    def paste(self, other: "Canvas", x: int, y: int):
        for row in range(other.h):
            ty = y + row
            if not 0 <= ty < self.h:
                continue
            src = other.buf[3 * row * other.w:3 * (row + 1) * other.w]
            self.buf[3 * (ty * self.w + x):3 * (ty * self.w + x) + len(src)] = src

    def crop_scaled(self, x0: int, y0: int, x1: int, y1: int, scale: int, background) -> "Canvas":
        """Crop [x0, x1) x [y0, y1) (areas outside are `background`) and scale up by nearest neighbour."""
        out = Canvas((x1 - x0) * scale, (y1 - y0) * scale, background)
        bg = bytes(background)
        for y in range(y0, y1):
            if 0 <= y < self.h:
                parts = []
                for x in range(x0, x1):
                    px = bytes(self.buf[3 * (y * self.w + x):3 * (y * self.w + x) + 3]) if 0 <= x < self.w else bg
                    parts.append(px * scale)
                row = b"".join(parts)
            else:
                row = bg * ((x1 - x0) * scale)
            for k in range(scale):
                oy = (y - y0) * scale + k
                out.buf[3 * oy * out.w:3 * (oy + 1) * out.w] = row
        return out

    def png(self) -> bytes:
        raw = bytearray()
        stride = 3 * self.w
        for y in range(self.h):
            raw.append(0)
            raw += self.buf[y * stride:(y + 1) * stride]

        def chunk(kind: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
                + chunk(b"IEND", b""))
