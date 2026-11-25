"""Self-contained offline HTML flashcard generator (snake layout)."""

from __future__ import annotations

import base64
import html
import io
import json
import urllib.parse
import zlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .api import CatalogItem, SynthesisSequence

@dataclass(frozen=True)
class _LayoutCfg:
    gap_min: int
    gap_pad: int
    row_vgap_min: int
    arrow_inset: int
    block_pad: int
    turn_side_gap: int
    turn_blob_pad: int
    margin: int
    label_vgap: int
    optimize_a4: bool


_SCREEN_CFG = _LayoutCfg(
    gap_min=300,
    gap_pad=150,
    row_vgap_min=240,
    arrow_inset=18,
    block_pad=28,
    turn_side_gap=70,
    turn_blob_pad=90,
    margin=90,
    label_vgap=20,
    optimize_a4=False,
)

_PDF_CFG = _LayoutCfg(
    gap_min=165,
    gap_pad=96,
    row_vgap_min=120,
    arrow_inset=14,
    block_pad=24,
    turn_side_gap=38,
    turn_blob_pad=36,
    margin=16,
    label_vgap=14,
    optimize_a4=True,
)

ROW_STEPS = 6
ROW_TARGET = 12000
SPLIT_MIN_GAP = 40   # reagent/conditions split threshold
A4_PAGE_W = 1106.0   # printable A4 landscape width in CSS px (293 mm)
A4_PAGE_H = 771.0    # printable A4 landscape height in CSS px (without citation)
A4_PAGE_H_CIT = 758.0  # printable A4 landscape height in CSS px (with citation)
LAYOUT_VERSION_TAG = "<!-- cbyd-layout-v2 -->"

def ink_mask(im: Image.Image) -> np.ndarray:
    """True where the image has actual ink (works for RGBA and RGB/white files)."""
    arr = np.asarray(im.convert("RGBA")).astype(np.int16)
    a = arr[:, :, 3]
    rgbmin = arr[:, :, :3].min(axis=2)
    if a.min() < 250:                       # has transparency
        return (a > 8) & (rgbmin < 250)
    return rgbmin < 245                     # opaque, white background


def trim(im: Image.Image, pad: int = 8) -> Image.Image:
    im = im.convert("RGBA")
    m = ink_mask(im)
    ys, xs = np.where(m)
    if len(xs) == 0:
        return im
    im = im.crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    out = Image.new("RGBA", (im.width + 2 * pad, im.height + 2 * pad), (0, 0, 0, 0))
    out.alpha_composite(im, (pad, pad))
    return out


def split_reaction(im: Image.Image):
    """Reaction PNGs contain [reagent structure] + [conditions text].
    Split them on the widest empty horizontal band, if present."""
    im = im.convert("RGBA")
    ink = ink_mask(im).any(axis=1)
    runs, start = [], None
    for y, v in enumerate(ink):
        if not v and start is None:
            start = y
        elif v and start is not None:
            runs.append((start, y - 1))
            start = None
    if start is not None:
        runs.append((start, len(ink) - 1))
    inner = [r for r in runs if r[0] > 0 and r[1] < len(ink) - 1]
    if inner:
        g = max(inner, key=lambda r: r[1] - r[0])
        if g[1] - g[0] + 1 >= SPLIT_MIN_GAP and g[0] >= 70 and (len(ink) - 1 - g[1]) >= 70:
            top = trim(im.crop((0, 0, im.width, g[0])))
            bot = trim(im.crop((0, g[1] + 1, im.width, im.height)))
            return top, bot
    return None, trim(im)


def data_uri(im: Image.Image) -> str:
    """Flatten transparency onto white and embed the smallest lossless image.

    CByD returns either palette PNGs or RGBA images whose ink lives in the
    alpha channel.  Saving RGBA as-is wastes a lot of bytes, so the image is
    composited onto the white board background first.  For black/gray line art
    an 8-bit grayscale copy is usually smaller; for indexed images the original
    palette often wins.  Every candidate is encoded as PNG and as lossless
    WebP, and the smallest payload is embedded — pixel-identical, but much
    faster to open over a phone connection.
    """
    has_alpha = im.mode in ("RGBA", "LA") or "transparency" in im.info
    if has_alpha:
        base = Image.new("RGBA", im.size, (255, 255, 255, 255))
        base.alpha_composite(im.convert("RGBA"))
        rgb = base.convert("RGB")
    else:
        rgb = im.convert("RGB")

    arr = np.asarray(rgb, dtype=np.int16)
    max_chroma = int(
        np.maximum(
            np.abs(arr[..., 0] - arr[..., 1]),
            np.maximum(np.abs(arr[..., 0] - arr[..., 2]), np.abs(arr[..., 1] - arr[..., 2])),
        ).max()
    )
    cand = rgb.convert("L") if max_chroma <= 10 else rgb

    buf_png = io.BytesIO()
    cand.save(buf_png, format="PNG", compress_level=6)
    best, best_mime = buf_png.getvalue(), "image/png"

    buf_webp = io.BytesIO()
    try:
        cand.save(buf_webp, format="WEBP", lossless=True, method=1, quality=50)
        data = buf_webp.getvalue()
        if len(data) < len(best):
            best, best_mime = data, "image/webp"
    except Exception:
        pass
    return f"data:{best_mime};base64," + base64.b64encode(best).decode("ascii")


# ---- layout --------------------------------------------------------------
def gap_for(step, cfg: _LayoutCfg = _SCREEN_CFG) -> float:
    w = cfg.gap_min
    if step["reagent"]:
        w = max(w, step["reagent"].width + cfg.gap_pad)
    if step["cond"]:
        w = max(w, step["cond"].width + cfg.gap_pad)
    return w


def partition_rows(steps, k: int, cfg: _LayoutCfg):
    """Split the step list into k contiguous rows of similar horizontal width."""
    n = len(steps)
    if n == 0:
        return [(0, -1)]
    k = max(1, min(n, k))
    node_w = [steps[0]["substrate"].width] + [s["product"].width for s in steps]
    gaps = [gap_for(s, cfg) for s in steps]
    PN = [0.0]
    for w in node_w:
        PN.append(PN[-1] + w)
    PG = [0.0]
    for g in gaps:
        PG.append(PG[-1] + g)

    def row_w_r0(a: int, b: int) -> float:
        return (PN[b + 2] - PN[a]) + (PG[b + 1] - PG[a])

    def row_w_rn(a: int, b: int) -> float:
        if not cfg.optimize_a4:
            return row_w_r0(a, b)
        # Step `a` is the vertical turn; horizontal steps on this row are a+1..b.
        if a == b:
            return PN[a + 2] - PN[a + 1]
        return (PN[b + 2] - PN[a + 1]) + (PG[b + 1] - PG[a + 1])

    INF = float("inf")
    dp = [[INF] * (n + 1) for _ in range(k + 1)]
    cut = [[-1] * (n + 1) for _ in range(k + 1)]
    dp[0][0] = 0.0
    for j in range(1, k + 1):
        for i in range(j, n + 1):
            for t in range(j - 1, i):
                if dp[j - 1][t] == INF:
                    continue
                rw = row_w_r0(t, i - 1) if j == 1 else row_w_rn(t, i - 1)
                val = max(dp[j - 1][t], rw)
                if val < dp[j][i]:
                    dp[j][i] = val
                    cut[j][i] = t

    rows, i = [], n
    rem = k
    while rem > 0:
        t = cut[rem][i]
        rows.append((t, i - 1))
        i = t
        rem -= 1
    rows.reverse()
    return rows


def _layout_for_rows(steps, rows, cfg: _LayoutCfg):
    """Place nodes, arrows, and labels for a given row partition."""
    n_nodes = len(steps) + 1
    pos = [None] * n_nodes                      # top-left corner
    row_of_node = [0] * n_nodes

    # first row
    a, b = rows[0]
    pos[0] = [0.0, 0.0]
    for i in range(a, b + 1):
        gap = gap_for(steps[i], cfg)
        pos[i + 1] = [pos[i][0] + steps[i]["substrate"].width + gap, 0.0]
        row_of_node[i + 1] = 0

    for r in range(1, len(rows)):
        a, b = rows[r]
        s = a                                    # shared node (end of previous row)
        direction = -1 if r % 2 == 1 else 1
        cx = pos[s][0] + steps[s - 1]["product"].width / 2
        w_next = steps[s]["product"].width if cfg.optimize_a4 else steps[s]["substrate"].width
        pos[s + 1] = [cx - w_next / 2, 0.0]
        row_of_node[s + 1] = r
        for i in range(s + 1, b + 1):
            gap = gap_for(steps[i], cfg)
            if direction > 0:
                pos[i + 1] = [pos[i][0] + steps[i]["substrate"].width + gap, 0.0]
            else:
                pos[i + 1] = [pos[i][0] - gap - steps[i]["product"].width, 0.0]
            row_of_node[i + 1] = r

    # --- vertical placement --------------------------------------------------
    def node_h(i):
        return steps[i - 1]["product"].height if i > 0 else steps[0]["substrate"].height

    top_ext, bot_ext = [], []
    for r, (a, b) in enumerate(rows):
        nodes = list(range(a, b + 2)) if r == 0 else list(range(a + 1, b + 2))
        t = max([node_h(n) / 2 for n in nodes] + [0])
        btm = t
        for i in range(a if r == 0 else a + 1, b + 1):
            if steps[i]["reagent"]:
                t = max(t, steps[i]["reagent"].height + cfg.block_pad)
            if steps[i]["cond"]:
                btm = max(btm, steps[i]["cond"].height + cfg.block_pad)
        top_ext.append(t)
        bot_ext.append(btm)

    mid = [0.0] * len(rows)
    mid[0] = top_ext[0]
    for r in range(1, len(rows)):
        a, _b = rows[r]
        s = a
        bottom_of_s = mid[r - 1] + node_h(s) / 2
        turn = steps[s]
        blob_h = 0
        if turn["reagent"]:
            blob_h += turn["reagent"].height + cfg.label_vgap
        if turn["cond"]:
            blob_h += turn["cond"].height + cfg.label_vgap
        vgap = max(cfg.row_vgap_min, blob_h + cfg.turn_blob_pad)
        row_top = bottom_of_s + vgap
        if cfg.optimize_a4:
            min_center_dist = bot_ext[r - 1] + top_ext[r] + 28
            mid[r] = max(mid[r - 1] + min_center_dist, row_top + top_ext[r])
        else:
            mid[r] = row_top + top_ext[r]

    for i in range(n_nodes):
        pos[i][1] = mid[row_of_node[i]] - node_h(i) / 2

    # --- arrows, labels ------------------------------------------------------
    arrows, labels = [], []
    for r, (a, b) in enumerate(rows):
        direction = 1 if r == 0 else (-1 if r % 2 == 1 else 1)
        for i in range(a if r == 0 else a + 1, b + 1):
            if direction > 0:
                x1 = pos[i][0] + steps[i]["substrate"].width + cfg.arrow_inset
                x2 = pos[i + 1][0] - cfg.arrow_inset
            else:
                x1 = pos[i][0] - cfg.arrow_inset
                x2 = pos[i + 1][0] + steps[i]["product"].width + cfg.arrow_inset
            y = mid[r]
            arrows.append([x1, y, x2, y])
            add_blocks(steps[i], labels, (x1 + x2) / 2, y, cfg)

        if r > 0:
            s = a
            cx = pos[s][0] + steps[s - 1]["product"].width / 2
            y1 = mid[r - 1] + node_h(s) / 2 + cfg.arrow_inset
            y2 = pos[s + 1][1] - cfg.arrow_inset
            arrows.append([cx, y1, cx, y2])
            add_turn_blocks(steps[s], labels, cx, (y1 + y2) / 2, r, cfg)

    return pos, arrows, labels, mid, rows


def _layout_bbox_size(steps, pos, arrows, labels, margin: int) -> tuple[float, float]:
    n_nodes = len(steps) + 1
    xs0, ys0, xs1, ys1 = [], [], [], []
    for i in range(n_nodes):
        im = steps[i - 1]["product"] if i > 0 else steps[0]["substrate"]
        xs0.append(pos[i][0])
        ys0.append(pos[i][1])
        xs1.append(pos[i][0] + im.width)
        ys1.append(pos[i][1] + im.height)
    for l in labels:
        xs0.append(l["x"])
        ys0.append(l["y"])
        xs1.append(l["x"] + l["w"])
        ys1.append(l["y"] + l["h"])
    for a in arrows:
        xs0.extend((a[0], a[2]))
        ys0.extend((a[1], a[3]))
        xs1.extend((a[0], a[2]))
        ys1.extend((a[1], a[3]))
    return (max(xs1) - min(xs0) + 2 * margin, max(ys1) - min(ys0) + 2 * margin)


def build(steps, cfg: _LayoutCfg = _SCREEN_CFG, page_w: float = A4_PAGE_W, page_h: float = A4_PAGE_H):
    """Build snake layout (standard screen layout or A4-optimized PDF layout)."""
    n = len(steps)
    if n == 0:
        return [[0.0, 0.0]], [], [], [0.0], [(0, -1)]

    if not cfg.optimize_a4:
        total = sum(s["substrate"].width + gap_for(s, cfg) for s in steps) + steps[-1]["product"].width
        k = int(min(n, max(1, round(n / ROW_STEPS))))
        while k < n and total / k > ROW_TARGET:
            k += 1
        rows = partition_rows(steps, k, cfg)
        return _layout_for_rows(steps, rows, cfg)

    best = None
    for k in range(1, min(n, 14) + 1):
        rows = partition_rows(steps, k, cfg)
        for _ in range(3):
            improved = False
            pos0, arr0, lbl0, _, _ = _layout_for_rows(steps, rows, cfg)
            w0, h0 = _layout_bbox_size(steps, pos0, arr0, lbl0, cfg.margin)
            sc0 = min(page_w / max(1.0, w0), page_h / max(1.0, h0))
            for b_idx in range(len(rows) - 1):
                for delta in (-1, 1):
                    a1, b1 = rows[b_idx]
                    a2, b2 = rows[b_idx + 1]
                    nb1, na2 = b1 + delta, a2 + delta
                    if nb1 < a1 or na2 > b2:
                        continue
                    cand = list(rows)
                    cand[b_idx] = (a1, nb1)
                    cand[b_idx + 1] = (na2, b2)
                    pc, ac, lc, _, _ = _layout_for_rows(steps, cand, cfg)
                    wc, hc = _layout_bbox_size(steps, pc, ac, lc, cfg.margin)
                    scc = min(page_w / max(1.0, wc), page_h / max(1.0, hc))
                    if scc > sc0 + 1e-6:
                        rows = cand
                        sc0 = scc
                        improved = True
            if not improved:
                break

        res = _layout_for_rows(steps, rows, cfg)
        w, h = _layout_bbox_size(steps, res[0], res[1], res[2], cfg.margin)
        sc = min(page_w / max(1.0, w), page_h / max(1.0, h))
        if best is None or sc > best[0]:
            best = (sc, res)

    return best[1]


def add_blocks(step, labels, cx, y, cfg: _LayoutCfg = _SCREEN_CFG):
    if step["reagent"]:
        r = step["reagent"]
        labels.append({"x": cx - r.width / 2, "y": y - cfg.block_pad - r.height,
                       "w": r.width, "h": r.height, "im": r})
    if step["cond"]:
        c = step["cond"]
        labels.append({"x": cx - c.width / 2, "y": y + cfg.block_pad,
                       "w": c.width, "h": c.height, "im": c})


def add_turn_blocks(step, labels, cx, cy, r, cfg: _LayoutCfg = _SCREEN_CFG):
    # stack reagent above conditions, place on the outer side of the turn
    parts = [p for p in (step["reagent"], step["cond"]) if p]
    if not parts:
        return
    w = max(p.width for p in parts)
    h = sum(p.height for p in parts) + cfg.label_vgap * (len(parts) - 1)
    direction = -1 if r % 2 == 1 else 1
    x = cx - cfg.turn_side_gap - w if direction > 0 else cx + cfg.turn_side_gap
    y = cy - h / 2
    for p in parts:
        labels.append({"x": x + (w - p.width) / 2, "y": y, "w": p.width, "h": p.height, "im": p})
        y += p.height + cfg.label_vgap


# ---- preview / static PNG -----------------------------------------------
_FONTS: dict[int, ImageFont.ImageFont] = {}



TEMPLATE_Z = (
    "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"
    "0QK5O/VBmPY2UBpiqEGvRX8VV9fevPg3+u13Lg=="
)

def _load_template() -> str:
    return zlib.decompress(base64.b64decode(TEMPLATE_Z)).decode("utf-8")


def _decode_data_uri(data_uri_str: str) -> Image.Image:
    payload = data_uri_str.split(",", 1)[1] if "," in data_uri_str else data_uri_str
    return Image.open(io.BytesIO(base64.b64decode(payload)))


def _plural_steps(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "стадий"
    r = n % 10
    if r == 1:
        return "стадия"
    if 2 <= r <= 4:
        return "стадии"
    return "стадий"


def render_flashcard_html(
    item: CatalogItem,
    sequence: SynthesisSequence,
    for_pdf: bool = False,
) -> str:
    """Render a self-contained offline HTML flashcard (screen or 2-page A4 PDF mode)."""
    cfg = _PDF_CFG if for_pdf else _SCREEN_CFG
    struct_imgs = {}
    if sequence.start_b64:
        struct_imgs[1] = trim(_decode_data_uri(sequence.start_b64))

    steps = []
    idx = 1
    for st in sequence.steps:
        if not st.reagent_b64 or not st.target_b64:
            continue
        struct_imgs[idx + 2] = trim(_decode_data_uri(st.target_b64))
        reaction = _decode_data_uri(st.reagent_b64)
        reagent, cond = split_reaction(reaction)
        steps.append({
            "substrate": struct_imgs[idx],
            "product": struct_imgs[idx + 2],
            "reagent": reagent,
            "cond": cond,
        })
        idx += 2

    page_h = A4_PAGE_H_CIT if sequence.citation_b64 else A4_PAGE_H
    pos, arrows, labels, mid, rows = build(steps, cfg=cfg, page_w=A4_PAGE_W, page_h=page_h)

    nodes = []
    if steps:
        n_nodes = len(steps) + 1
        stage_num = 0
        for i in range(n_nodes):
            im = steps[i - 1]["product"] if i > 0 else steps[0]["substrate"]
            hidden = 0 < i < n_nodes - 1
            if hidden:
                stage_num += 1
            nodes.append({
                "x": pos[i][0],
                "y": pos[i][1],
                "w": im.width,
                "h": im.height,
                "im": im,
                "hidden": hidden,
                "num": stage_num if hidden else 0,
            })
    elif 1 in struct_imgs:
        im = struct_imgs[1]
        nodes.append({
            "x": 0.0,
            "y": 0.0,
            "w": im.width,
            "h": im.height,
            "im": im,
            "hidden": False,
            "num": 0,
        })

    cim = None
    cw = ch = 0.0
    if sequence.citation_b64:
        c = trim(_decode_data_uri(sequence.citation_b64), pad=4)
        cw = max(1, int(c.width * 0.55))
        ch = max(1, int(c.height * 0.55))
        cim = c.resize((cw, ch), Image.LANCZOS)

    minx = min([n["x"] for n in nodes] + [l["x"] for l in labels] + [a[0] for a in arrows] + [a[2] for a in arrows])
    miny = min([n["y"] for n in nodes] + [l["y"] for l in labels] + [a[1] for a in arrows] + [a[3] for a in arrows])
    maxx = max([n["x"] + n["w"] for n in nodes] + [l["x"] + l["w"] for l in labels] + [a[0] for a in arrows] + [a[2] for a in arrows])
    maxy = max([n["y"] + n["h"] for n in nodes] + [l["y"] + l["h"] for l in labels] + [a[1] for a in arrows] + [a[3] for a in arrows])

    dx, dy = cfg.margin - minx, cfg.margin - miny
    for n in nodes:
        n["x"] += dx
        n["y"] += dy
    for l in labels:
        l["x"] += dx
        l["y"] += dy
    for a in arrows:
        a[0] += dx
        a[1] += dy
        a[2] += dx
        a[3] += dy
    W = maxx - minx + 2 * cfg.margin
    H = maxy - miny + 2 * cfg.margin

    if not for_pdf and cim is not None:
        labels.append({"x": cfg.margin, "y": H + 90, "w": cw, "h": ch, "im": cim})
        H += ch + 170

    data = {
        "W": round(W),
        "H": round(H),
        "pdfMode": bool(for_pdf),
        "citation": {"src": data_uri(cim)} if (for_pdf and cim is not None) else None,
        "nodes": [
            {
                "x": round(n["x"], 2),
                "y": round(n["y"], 2),
                "w": n["w"],
                "h": n["h"],
                "src": data_uri(n["im"]),
                "hidden": bool(n["hidden"]),
                "num": n["num"],
            }
            for n in nodes
        ],
        "labels": [
            {
                "x": round(l["x"], 2),
                "y": round(l["y"], 2),
                "w": l["w"],
                "h": l["h"],
                "src": data_uri(l["im"]),
            }
            for l in labels
        ],
        "arrows": [[round(v, 2) for v in a] for a in arrows],
    }

    title = getattr(item, "full_title", None) or item.name
    meta_bits = []
    if item.author:
        meta_bits.append(item.author)
    if item.year:
        meta_bits.append(str(item.year))
    meta_bits.append(f"{len(steps)} {_plural_steps(len(steps))}")
    meta = " · ".join(meta_bits)
    if sequence.doi:
        doi = urllib.parse.unquote(sequence.doi).replace("%2F", "/")
        meta += f' · <a href="https://doi.org/{doi}" target="_blank" rel="noopener">doi:{doi}</a>'

    tpl = _load_template()
    return (
        tpl.replace("{{TITLE}}", html.escape(title))
        .replace("{{NAME}}", html.escape(title))
        .replace("{{META}}", meta)
        .replace("{{W}}", str(round(W)))
        .replace("{{H}}", str(round(H)))
        .replace("{{DATA}}", json.dumps(data, ensure_ascii=False))
    )


def save_flashcard_html(
    item: CatalogItem,
    sequence: SynthesisSequence,
    output_dir: Path,
    filename: str,
    for_pdf: bool = False,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / filename
    html_content = render_flashcard_html(item, sequence, for_pdf=for_pdf)
    out_path.write_text(html_content, encoding="utf-8")
    return out_path
