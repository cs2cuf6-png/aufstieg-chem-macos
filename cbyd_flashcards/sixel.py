"""Terminal Sixel graphics detection and fast pure-Python Sixel encoder."""

from __future__ import annotations

import io
import os
import re
import sys
import time

import numpy as np
from PIL import Image

_DA1_CELL_RE = re.compile(r"\x1b\[6;(\d+);(\d+)t")
_DA1_ATTR_RE = re.compile(r"\x1b\[\?([0-9;]*)c")


def detect_sixel_support(timeout: float = 0.12) -> tuple[bool, int, int]:
    """Query the terminal for Sixel support (DA1) and cell pixel dimensions.

    Returns (supports_sixel, cell_width_px, cell_height_px).
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return False, 9, 18

    buf = ""
    try:
        if os.name == "nt":
            import msvcrt

            while msvcrt.kbhit():
                msvcrt.getwch()
            sys.stdout.write("\x1b[c\x1b[16t")
            sys.stdout.flush()

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if msvcrt.kbhit():
                    buf += msvcrt.getwch()
                    if "c" in buf and ("t" in buf or time.monotonic() + 0.02 >= deadline):
                        # Give a tiny moment for the second response if it's arriving
                        time.sleep(0.015)
                        while msvcrt.kbhit():
                            buf += msvcrt.getwch()
                        break
                else:
                    time.sleep(0.005)
        else:
            import select
            import termios
            import tty

            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                sys.stdout.write("\x1b[c\x1b[16t")
                sys.stdout.flush()
                deadline = time.monotonic() + timeout
                while True:
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        break
                    r, _, _ = select.select([fd], [], [], rem)
                    if not r:
                        break
                    buf += os.read(fd, 128).decode("ascii", errors="ignore")
                    if "c" in buf and "t" in buf:
                        break
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        return False, 9, 18

    supports = False
    m_attr = _DA1_ATTR_RE.search(buf)
    if m_attr:
        params = [p for p in m_attr.group(1).split(";") if p]
        supports = "4" in params

    cell_w, cell_h = 9, 18
    m_cell = _DA1_CELL_RE.search(buf)
    if m_cell:
        ch = int(m_cell.group(1))
        cw = int(m_cell.group(2))
        if 6 <= cw <= 32 and 10 <= ch <= 64:
            cell_w, cell_h = cw, ch

    return supports, cell_w, cell_h


def _encode_indexed_sixel(
    idx: np.ndarray,
    palette_header: str,
    color_ids: tuple[int, ...] = (1, 2, 3),
) -> str:
    """Fast vectorized Sixel encoder for a 2D uint8 palette-index array (0 = transparent)."""
    h, w = idx.shape
    pad_h = (-h) % 6
    if pad_h:
        idx = np.pad(idx, ((0, pad_h), (0, 0)), constant_values=0)

    out = [
        f'\x1bP0;1q"1;1;{w};{h}',
        palette_header,
    ]
    weights = np.array([1, 2, 4, 8, 16, 32], dtype=np.uint8)[:, None]

    for y in range(0, idx.shape[0], 6):
        band = idx[y : y + 6]
        band_parts: list[str] = []
        for color in color_ids:
            mask = band == color
            if not mask.any():
                continue
            vals = (mask * weights).sum(axis=0, dtype=np.uint8)
            # Trim trailing empty columns (0) in this band/color to shrink output 2-3x
            nz = np.flatnonzero(vals)
            if nz.size == 0:
                continue
            vals = vals[: int(nz[-1]) + 1] + 63

            if vals.size == 1:
                band_parts.append(f"#{color}{chr(int(vals[0]))}")
                continue

            changes = np.flatnonzero(vals[1:] != vals[:-1]) + 1
            starts = np.empty(changes.size + 1, dtype=np.intp)
            starts[0] = 0
            starts[1:] = changes
            ends = np.empty(changes.size + 1, dtype=np.intp)
            ends[:-1] = changes
            ends[-1] = vals.size
            counts = ends - starts
            chars = vals[starts]

            rle = [
                f"!{c}{chr(ch)}" if c > 3 else chr(ch) * c
                for ch, c in zip(chars.tolist(), counts.tolist())
            ]
            band_parts.append(f"#{color}" + "".join(rle))
        if band_parts:
            out.append("$".join(band_parts))
        out.append("-")

    out.append("\x1b\\")
    return "".join(out)


def encode_molecule_sixel(
    source: Image.Image | bytes,
    box_w_px: int,
    box_h_px: int,
) -> str:
    """Encode a chemical structure image as a crisp 4-shade dark-mode Sixel string."""
    if isinstance(source, bytes):
        im = Image.open(io.BytesIO(source))
    else:
        im = source

    if im.mode in ("RGBA", "LA") or "transparency" in im.info:
        base = Image.new("RGBA", im.size, (255, 255, 255, 255))
        base.alpha_composite(im.convert("RGBA"))
        gray = base.convert("L")
    else:
        gray = im.convert("L")

    arr = np.asarray(gray)
    ys, xs = np.where(arr < 225)
    if len(xs) > 0:
        gray = gray.crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))

    pad_x = max(4, box_w_px // 20)
    pad_y = max(4, box_h_px // 16)
    avail_w = max(16, box_w_px - 2 * pad_x)
    avail_h = max(16, box_h_px - 2 * pad_y)

    scale = min(avail_w / max(1, gray.width), avail_h / max(1, gray.height))
    nw = max(1, int(round(gray.width * scale)))
    nh = max(1, int(round(gray.height * scale)))
    resized = gray.resize((nw, nh), Image.Resampling.LANCZOS)

    canvas = Image.new("L", (box_w_px, box_h_px), 255)
    canvas.paste(resized, ((box_w_px - nw) // 2, (box_h_px - nh) // 2))

    # Invert onto transparent background: 0=bg, 1=dim gray, 2=mid gray, 3=bright white
    inv = 255 - np.asarray(canvas, dtype=np.uint8)
    idx = np.digitize(inv, [36, 105, 180]).astype(np.uint8)
    return _encode_indexed_sixel(
        idx,
        "#0;2;5;5;5#1;2;35;40;46#2;2;68;73;80#3;2;96;98;100",
        color_ids=(1, 2, 3),
    )


def encode_scene_sixel(idx: np.ndarray) -> str:
    """Encode a pre-composed chain viewer scene (uint8 index map) as Sixel.

    Palette:
      0 = transparent background
      1 = dim gray (anti-alias)
      2 = mid gray
      3 = crisp bright white (chemical structures & text)
      4 = bright cyan/blue (reaction arrow & accents)
      5 = muted slate (hidden flashcard frame)
      6 = amber/gold (hidden '?' badge)
    """
    palette = (
        "#0;2;5;5;5"
        "#1;2;35;40;46"
        "#2;2;68;73;80"
        "#3;2;96;98;100"
        "#4;2;35;75;98"
        "#5;2;28;42;62"
        "#6;2;95;75;32"
    )
    return _encode_indexed_sixel(idx, palette, color_ids=(1, 2, 3, 4, 5, 6))
