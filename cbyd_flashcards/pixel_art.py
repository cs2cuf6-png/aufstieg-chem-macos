"""Exact 30x28 pixel-for-pixel Aufstieg mountain art for the terminal menu."""

from __future__ import annotations

import base64
import io
import os
import zlib

import numpy as np
from PIL import Image
from rich.text import Text

_W = 30
_H = 28
_DATA_Z = "eNrt1LFKw1AUBmBfotpBK+omQh9A0EWoglAqKsVBpE5ZBRHESV/AIgVBcCgKBScHcYqOokOI4OAmiFLQzU0oHvkv/OX0mkZtbaLg8HNz0+Z8ufceIiJd8oN5vK3Kb6z1lzOfX4tlHwZTEzK27pt89t/K3qV0wh0tnJlEtV567bru8c2Xnx3uH4/lfONy755qEocZtevf18R7fpPikSfu9av0JaYi8WEiW2XfJAoXewvTKV3Ixq5n0t2T6ahNE4GHtXKE2649MlSQMBPJTJeMx7W268Jk7D6azG8LrjHCLZ48NJit2trUgQUnO7dv+hcj5un0SqD7Hd+2BlILxqPZLLnsTksufg8y9TmGBefezO1N5gJt7pPt8lvkbJ42GKiTSM7W59gL20Wf4b7uN+3DpIv1YURN1GFd/M5r1uA5o6fwXro+rtHjB1cvUj6vfuh1mrov8I6ox57R69KmdnVdfkMQuDPOoVlDM5tzOtwXBD2Dkfe0a9fTLrK86tbPIqznWZ/RJueog/O3n11arJizYrAGuLhvv99/os076LVOLw=="

# Coordinates of the 4 sky stars in the 30x28 grid (y, x)
_STAR_LIST = [(0, 20), (1, 9), (4, 26), (7, 4)]
_STARS = set(_STAR_LIST)
_STAR_PALETTE = [
    (229, 216, 231),
    (168, 155, 201),
    (121, 192, 255),
    (255, 255, 255),
]

# Waving flag frames in the (y=3..6, x=16..20) region
_FLAG_BLUE = (62, 122, 208)
_FLAG_LIGHT = (95, 158, 240)
_FLAG_AREA = {(y, x) for y in range(3, 7) for x in range(16, 21)}
_FLAG_FRAMES = [
    # Frame 0: original shape
    {(4, 16), (4, 17), (5, 16), (5, 17), (5, 18), (5, 19), (6, 16), (6, 17), (6, 18), (6, 19)},
    # Frame 1: tail lifts up
    {(4, 16), (4, 17), (4, 19), (5, 16), (5, 17), (5, 18), (5, 19), (5, 20), (6, 16), (6, 17), (6, 18)},
    # Frame 2: crest in the middle
    {(3, 18), (4, 16), (4, 17), (4, 18), (4, 19), (5, 16), (5, 17), (5, 19), (5, 20), (6, 16), (6, 17)},
    # Frame 3: tail dips down
    {(4, 16), (4, 17), (5, 16), (5, 17), (5, 18), (6, 16), (6, 17), (6, 18), (6, 19), (6, 20)},
]

_RAW: bytes | None = None
_CACHED_IDLE: Text | None = None


def _get_raw() -> bytes:
    global _RAW
    if _RAW is None:
        _RAW = zlib.decompress(base64.b64decode(_DATA_Z))
    return _RAW


def get_mountain_pixel_art(
    star_frame: int | None = None,
    flag_frame: int | None = None,
    single_star_step: int | None = None,
) -> Text:
    """Return a Rich Text rendering of the exact 30x28 mountain pixel art.

    - When all args are None, renders 100% unmodified original pixels (cached).
    - star_frame: twinkles all 4 stars (used when scrolling or downloading).
    - single_star_step: twinkles one star on any other user action.
    - flag_frame: waves the flag (used during download).
    """
    global _CACHED_IDLE
    if star_frame is None and flag_frame is None and single_star_step is None and _CACHED_IDLE is not None:
        return _CACHED_IDLE

    raw = _get_raw()
    t = Text(no_wrap=True)
    active_single_star = _STAR_LIST[single_star_step % len(_STAR_LIST)] if single_star_step is not None else None

    def px(y: int, x: int) -> tuple[int, int, int, int]:
        idx = (y * _W + x) * 4
        r, g, b, a = raw[idx], raw[idx + 1], raw[idx + 2], raw[idx + 3]
        if a > 0 and (y, x) in _STARS:
            if star_frame is not None:
                c = _STAR_PALETTE[(star_frame + x) % len(_STAR_PALETTE)]
                return c[0], c[1], c[2], 255
            if active_single_star is not None and (y, x) == active_single_star:
                c = _STAR_PALETTE[(single_star_step + 2) % len(_STAR_PALETTE)]
                return c[0], c[1], c[2], 255
        if flag_frame is not None and (y, x) in _FLAG_AREA:
            flag_set = _FLAG_FRAMES[flag_frame % len(_FLAG_FRAMES)]
            if (y, x) in flag_set:
                c = _FLAG_LIGHT if x >= 19 else _FLAG_BLUE
                return c[0], c[1], c[2], 255
            return 0, 0, 0, 0
        return r, g, b, a

    render_h = _H - 2
    for y in range(0, render_h, 2):
        for x in range(_W):
            rt, gt, bt, at = px(y, x)
            rb, gb, bb, ab = px(y + 1, x)

            if at == 0 and ab == 0:
                t.append(" ")
            elif at == 0 and ab > 0:
                t.append("▄", style=f"rgb({rb},{gb},{bb})")
            elif at > 0 and ab == 0:
                t.append("▀", style=f"rgb({rt},{gt},{bt})")
            else:
                t.append("▀", style=f"rgb({rt},{gt},{bt}) on rgb({rb},{gb},{bb})")
        if y + 2 < render_h:
            t.append("\n")

    if star_frame is None and flag_frame is None and single_star_step is None:
        _CACHED_IDLE = t
    return t


_BRAILLE_BITS = (
    (0, 0, 0x01), (1, 0, 0x02), (2, 0, 0x04), (3, 0, 0x40),
    (0, 1, 0x08), (1, 1, 0x10), (2, 1, 0x20), (3, 1, 0x80),
)


def render_molecule_braille(
    source: Image.Image | bytes,
    width_cells: int = 36,
    height_cells: int = 8,
    style: str = "#c9d1d9",
) -> Text:
    """Render a chemical structure image into crisp Unicode Braille art."""
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

    pw, ph = width_cells * 2, height_cells * 4
    scale = min(pw / max(1, gray.width), ph / max(1, gray.height))
    nw = max(1, int(round(gray.width * scale)))
    nh = max(1, int(round(gray.height * scale)))
    resized = gray.resize((nw, nh), Image.Resampling.LANCZOS)

    canvas = Image.new("L", (pw, ph), 255)
    canvas.paste(resized, ((pw - nw) // 2, (ph - nh) // 2))
    ink = np.asarray(canvas) < 215

    t = Text(no_wrap=True)
    for cy in range(height_cells):
        row_chars: list[str] = []
        y0 = cy * 4
        for cx in range(width_cells):
            x0 = cx * 2
            code = 0x2800
            for dy, dx, bit in _BRAILLE_BITS:
                if ink[y0 + dy, x0 + dx]:
                    code |= bit
            row_chars.append(chr(code))
        t.append("".join(row_chars), style=style)
        if cy + 1 < height_cells:
            t.append("\n")
    return t


def is_legacy_windows_console() -> bool:
    """Return True when running inside legacy Windows conhost.exe without modern font fallback."""
    if os.name != "nt":
        return False
    modern_vars = (
        "WT_SESSION",
        "TERM_PROGRAM",
        "WEZTERM_EXECUTABLE",
        "ALACRITTY_WINDOW_ID",
        "ConEmuPID",
        "ANSICON",
    )
    return not any(os.environ.get(v) for v in modern_vars)


def render_molecule_halfblock(
    source: Image.Image | bytes,
    width_cells: int = 36,
    height_cells: int = 10,
) -> Text:
    """Render a chemical structure image using CP437/Consolas-safe half-block characters."""
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

    pw, ph = width_cells, height_cells * 2
    scale = min(pw / max(1, gray.width), ph / max(1, gray.height))
    nw = max(1, int(round(gray.width * scale)))
    nh = max(1, int(round(gray.height * scale)))
    resized = gray.resize((nw, nh), Image.Resampling.LANCZOS)

    canvas = Image.new("L", (pw, ph), 255)
    canvas.paste(resized, ((pw - nw) // 2, (ph - nh) // 2))

    # Normalized ink intensity [0..1] with mild contrast boost so thin bonds stay legible
    ink = np.clip((255.0 - np.asarray(canvas, dtype=np.float32)) / 255.0, 0.0, 1.0)
    ink = np.power(ink, 0.72)

    def _to_rgb(v: float) -> tuple[int, int, int] | None:
        if v < 0.09:
            return None
        c = int(round(75 + 180 * v))
        return min(255, c), min(255, c + 4), min(255, c + 10)

    t = Text(no_wrap=True)
    for y in range(0, ph, 2):
        for x in range(pw):
            top = _to_rgb(float(ink[y, x]))
            bot = _to_rgb(float(ink[y + 1, x]))
            if top is None and bot is None:
                t.append(" ")
            elif top is None and bot is not None:
                t.append("▄", style=f"rgb({bot[0]},{bot[1]},{bot[2]})")
            elif top is not None and bot is None:
                t.append("▀", style=f"rgb({top[0]},{top[1]},{top[2]})")
            else:
                t.append(
                    "▀",
                    style=f"rgb({top[0]},{top[1]},{top[2]}) on rgb({bot[0]},{bot[1]},{bot[2]})",
                )
        if y + 2 < ph:
            t.append("\n")
    return t
