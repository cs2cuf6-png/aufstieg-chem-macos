"""In-terminal step-by-step viewer for synthesis chains (Sixel / GDI / Braille)."""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from PIL import Image, ImageChops, ImageDraw
from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from .api import SynthesisSequence
from .conhost_gfx import draw_dib_on_conhost, encode_scene_dib
from .db import decode_image_record_to_pil
from .pixel_art import render_molecule_braille
from .sixel import encode_scene_sixel

Loader = Callable[[], "Image.Image | None"]


@dataclass
class Step:
    idx: int
    reagent: Loader
    target: Loader
    sub: "Node | None" = None


@dataclass
class Node:
    name: str
    start: Loader
    steps: list[Step] = field(default_factory=list)


@dataclass
class Gfx:
    """Terminal graphics capabilities handed over by term_ui."""

    mode: str  # "sixel" | "gdi" | "text"
    cell_w: int
    cell_h: int
    real_stdout: object
    set_gdi: Callable[[tuple[int, int, bytes] | None, int, int], None]


# ---- data normalisation ---------------------------------------------------
def node_from_raw(tree: dict, images: list) -> Node:
    cache: dict[int, Image.Image | None] = {}

    def loader(i: int) -> Loader:
        def _l() -> Image.Image | None:
            if i < 0 or i >= len(images):
                return None
            if i not in cache:
                cache[i] = decode_image_record_to_pil(images[i])
            return cache[i]

        return _l

    def conv(d: dict) -> Node:
        return Node(
            name=d.get("name", ""),
            start=loader(d.get("start", -1)),
            steps=[
                Step(
                    idx=s["idx"],
                    reagent=loader(s.get("reagent", -1)),
                    target=loader(s.get("target", -1)),
                    sub=conv(s["sub"]) if s.get("sub") else None,
                )
                for s in d.get("steps", [])
            ],
        )

    return conv(tree)


def node_from_sequence(seq: SynthesisSequence) -> Node:
    def b64_loader(uri: str | None) -> Loader:
        cache: list[Image.Image | None] = []

        def _l() -> Image.Image | None:
            if not cache:
                if not uri or "," not in uri:
                    cache.append(None)
                else:
                    cache.append(Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))))
            return cache[0]

        return _l

    def conv(s: SynthesisSequence) -> Node:
        return Node(
            name=s.name,
            start=b64_loader(s.start_b64),
            steps=[
                Step(
                    idx=st.step_index,
                    reagent=b64_loader(st.reagent_b64),
                    target=b64_loader(st.target_b64),
                    sub=conv(st.subsequence) if st.subsequence else None,
                )
                for st in s.steps
            ],
        )

    return conv(seq)


# ---- scene composition ----------------------------------------------------
def _ink(im: Image.Image | None) -> Image.Image | None:
    """Return cropped 'ink' image (255 = ink on 0 background) or None."""
    if im is None:
        return None
    if im.mode in ("RGBA", "LA") or "transparency" in im.info:
        base = Image.new("RGBA", im.size, (255, 255, 255, 255))
        base.alpha_composite(im.convert("RGBA"))
        gray = base.convert("L")
    else:
        gray = im.convert("L")
    arr = np.asarray(gray)
    ys, xs = np.where(arr < 225)
    if len(xs):
        gray = gray.crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    return Image.eval(gray, lambda v: 255 - v)


def _paste_fit(canvas: Image.Image, ink: Image.Image | None, box_xywh: tuple[int, int, int, int],
               anchor: str = "center") -> None:
    if ink is None:
        return
    bx, by, bw, bh = box_xywh
    bw, bh = max(8, bw), max(8, bh)
    scale = min(bw / ink.width, bh / ink.height, 1.6)
    nw, nh = max(1, int(ink.width * scale)), max(1, int(ink.height * scale))
    small = ink.resize((nw, nh), Image.Resampling.LANCZOS)
    x = bx + (bw - nw) // 2
    y = by + (bh - nh) // 2 if anchor == "center" else by + bh - nh
    region = canvas.crop((x, y, x + nw, y + nh))
    canvas.paste(ImageChops.lighter(region, small), (x, y))


def compose_scene(
    left: Image.Image | None,
    reagent: Image.Image | None,
    right: Image.Image | None,
    w: int,
    h: int,
) -> np.ndarray:
    """Compose reactant -> (reagent over arrow) -> product into a palette-index map."""
    ink = Image.new("L", (w, h), 0)
    pad = max(6, w // 60)
    side_w = int(w * 0.28)
    mid_x0 = pad + side_w + pad
    mid_w = w - 2 * (pad + side_w + pad)
    arrow_y = int(h * 0.62) if reagent is not None else h // 2

    struct_h = int(2 * min(arrow_y, h - arrow_y)) - 2 * pad
    struct_h = max(struct_h, h // 3)
    sy = arrow_y - struct_h // 2
    _paste_fit(ink, _ink(left), (pad, sy, side_w, struct_h))
    _paste_fit(ink, _ink(right), (w - pad - side_w, sy, side_w, struct_h))
    if reagent is not None:
        _paste_fit(ink, _ink(reagent), (mid_x0, pad, mid_w, arrow_y - 2 * pad), anchor="bottom")

    idx = np.digitize(np.asarray(ink), [36, 105, 180]).astype(np.uint8)

    arrow = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(arrow)
    th = max(2, h // 220)
    x0, x1 = mid_x0, mid_x0 + mid_w
    d.line([(x0, arrow_y), (x1, arrow_y)], fill=255, width=th)
    hs = max(8, h // 40)
    d.polygon([(x1, arrow_y), (x1 - 2 * hs, arrow_y - hs), (x1 - 2 * hs, arrow_y + hs)], fill=255)
    idx[np.asarray(arrow) > 0] = 4
    return idx


# ---- viewer ---------------------------------------------------------------
def run_chain_viewer(
    live,
    console,
    title: str,
    root: Node,
    gfx: Gfx,
    read_key: Callable[[], str],
    open_html: Callable[[], str],
) -> str:
    """Interactive viewer loop. Returns a status message for the main menu."""
    stack: list[list] = [[root, 0]]  # [node, step index]
    scene_cache: dict[tuple, object] = {}
    status = ""

    def layout() -> tuple[int, int, int]:
        cols = max(20, console.width - 2)
        rows = max(6, console.height - 6)
        return cols, rows, 0

    def build(node: Node, k: int, cols: int, rows: int):
        key = (id(node), k, cols, rows, gfx.mode)
        if key in scene_cache:
            return scene_cache[key]
        if node.steps:
            st = node.steps[k]
            left = node.start() if k == 0 else node.steps[k - 1].target()
            reagent, right = st.reagent(), st.target()
        else:
            left, reagent, right = node.start(), None, None
        if gfx.mode == "text":
            idx = compose_scene(left, reagent, right, cols * 2, rows * 4)
            gray = Image.fromarray((255 - np.select(
                [idx == 0, idx == 1, idx == 2], [0, 85, 170], 255)).astype(np.uint8), "L")
            res: object = render_molecule_braille(gray, width_cells=cols, height_cells=rows)
        else:
            w, h = cols * gfx.cell_w, rows * gfx.cell_h
            idx = compose_scene(left, reagent, right, w, h)
            res = encode_scene_sixel(idx) if gfx.mode == "sixel" else encode_scene_dib(idx)
        if len(scene_cache) > 24:
            scene_cache.pop(next(iter(scene_cache)))
        scene_cache[key] = res
        return res

    def draw(show_gfx: bool = True) -> None:
        node, k = stack[-1]
        cols, rows, _ = layout()
        depth = " › ".join(["цепочка"] + [f"стадия {n.steps[i].idx}" for n, i in stack[:-1]])
        head = Text(no_wrap=True, overflow="ellipsis")
        head.append(f" {title}", style="bold bright_blue")
        head.append(f"  ·  {depth}", style="dim")
        info = Text(no_wrap=True, overflow="ellipsis")
        if node.steps:
            st = node.steps[k]
            info.append(f" Стадия {k + 1}/{len(node.steps)}", style="bold white")
            if st.sub:
                info.append("  ·  есть подцепочка (Enter — открыть)", style="bold yellow")
        else:
            info.append(" Единственная структура", style="bold white")
        if status:
            info.append(f"  ·  {status}", style="cyan")
        foot = Text(no_wrap=True)
        for key, desc in [("←→", "стадии"), ("Enter", "подцепочка"),
                          ("Ctrl+B", "HTML в браузере"), ("Esc", "назад")]:
            foot.append(f" {key}", style="bold bright_blue")
            foot.append(f" {desc} ", style="dim")

        scene = build(node, k, cols, rows)
        if gfx.mode == "text":
            body: object = scene
        else:
            body = Text("\n".join([" " * cols] * rows), no_wrap=True)
        group = Group(head, info, body, Text(""), foot)
        if gfx.mode == "sixel":
            gfx.real_stdout.write("\x1b[?2026h")
        try:
            live.update(group, refresh=True)
            if gfx.mode == "sixel":
                gfx.real_stdout.write(f"\x1b7\x1b[3;2H{scene}\x1b8")
                gfx.real_stdout.flush()
            elif gfx.mode == "gdi":
                gfx.set_gdi(scene, 2, 3)
                draw_dib_on_conhost(scene, 2, 3, gfx.cell_w, gfx.cell_h)
        finally:
            if gfx.mode == "sixel":
                gfx.real_stdout.write("\x1b[?2026l")
                gfx.real_stdout.flush()

    try:
        while True:
            draw()
            status = ""
            key = read_key()
            node, k = stack[-1]
            if key in ("CTRL_C",):
                break
            if key in ("ESC", "BACKSPACE"):
                if len(stack) == 1:
                    break
                stack.pop()
            elif key in ("RIGHT", "DOWN", "PGDN", " ") and node.steps:
                if k < len(node.steps) - 1:
                    stack[-1][1] += 1
            elif key in ("LEFT", "UP", "PGUP") and node.steps:
                if k > 0:
                    stack[-1][1] -= 1
            elif key == "ENTER" and node.steps and node.steps[k].sub is not None:
                stack.append([node.steps[k].sub, 0])
            elif key == "CTRL_B":
                status = open_html()
    finally:
        if gfx.mode == "gdi":
            gfx.set_gdi(None, 0, 0)
    return ""
