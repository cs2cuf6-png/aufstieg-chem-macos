"""Compact 4-shade grayscale + LZMA offline SQLite database (cbyd.db) for Chemistry By Design."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import lzma
import sqlite3
import struct
import sys
import time
from pathlib import Path
from typing import Callable

import httpx
import numpy as np
from PIL import Image

from .api import (
    CACHE_DIR,
    CatalogItem,
    StepData,
    SynthesisSequence,
    _download_sequence_recursive,
    fetch_catalog,
)

DB_FORMAT_VERSION = "gray4_lzma_v1"
_GRAY4_LUT = np.array([0, 85, 170, 255], dtype=np.uint8)
_IMG_HEADER = struct.Struct("<BHH I")


def find_db_path(explicit: Path | None = None) -> Path | None:
    """Locate an existing cbyd.db file in standard locations."""
    if explicit is not None:
        return explicit if explicit.exists() else None

    exe_dir = Path(sys.executable).resolve().parent
    candidates = [
        Path.cwd() / "cbyd.db",
        exe_dir / "cbyd.db",
    ]
    if sys.platform == "darwin":
        # Inside Aufstieg.app/Contents/Resources and next to Aufstieg.app
        candidates.append(exe_dir.parent / "Resources" / "cbyd.db")
        if exe_dir.name == "MacOS" and exe_dir.parent.name == "Contents":
            candidates.append(exe_dir.parent.parent.parent / "cbyd.db")
        candidates.append(Path.home() / "Documents" / "Aufstieg" / "cbyd.db")
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "cbyd.db")
    candidates.extend([
        Path(__file__).resolve().parent.parent / "cbyd.db",
        CACHE_DIR / "cbyd.db",
    ])
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _b64_to_bytes(b64_uri: str | None) -> bytes | None:
    if not b64_uri:
        return None
    if "," in b64_uri:
        b64_uri = b64_uri.split(",", 1)[1]
    return base64.b64decode(b64_uri)


def encode_image_4gray(raw_or_uri: str | bytes | None) -> tuple[int, int, int, bytes] | None:
    """Encode an image into 4-shade grayscale (2-bit packed) or lossless color if chromatic.

    Returns (mode, width, height, payload_bytes) where:
      mode 0 = 4-shade grayscale (2-bit packed, 4 pixels/byte)
      mode 1 = lossless PNG (for <1% genuinely colored images)
      mode 2 = lossless WebP (for <1% genuinely colored images)
    """
    raw = _b64_to_bytes(raw_or_uri) if isinstance(raw_or_uri, str) else raw_or_uri
    if not raw:
        return None

    im = Image.open(io.BytesIO(raw))
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

    if max_chroma <= 10:
        gray = np.asarray(rgb.convert("L"), dtype=np.uint8)
        h, w = gray.shape
        q = np.digitize(gray, [64, 140, 210]).astype(np.uint8)
        pad = (-w) % 4
        if pad:
            q = np.pad(q, ((0, 0), (0, pad)), constant_values=3)
        packed = (q[:, 0::4] << 6) | (q[:, 1::4] << 4) | (q[:, 2::4] << 2) | q[:, 3::4]
        return (0, w, h, packed.tobytes())

    # Genuinely colored image (<1% of database): keep lossless color
    w, h = rgb.size
    buf_png = io.BytesIO()
    rgb.save(buf_png, format="PNG", optimize=True, compress_level=9)
    best_mode, best_data = 1, buf_png.getvalue()

    buf_webp = io.BytesIO()
    try:
        rgb.save(buf_webp, format="WEBP", lossless=True, method=6, quality=100)
        if buf_webp.tell() < len(best_data):
            best_mode, best_data = 2, buf_webp.getvalue()
    except Exception:
        pass
    return (best_mode, w, h, best_data)


def decode_image_record_to_pil(rec: tuple[int, int, int, bytes] | None) -> Image.Image | None:
    """Decode an image record (mode, w, h, data) into a PIL Image."""
    if rec is None:
        return None
    mode, w, h, data = rec
    if mode == 0:
        stride = (w + 3) // 4
        packed = np.frombuffer(data, dtype=np.uint8).reshape((h, stride))
        q = np.empty((h, stride * 4), dtype=np.uint8)
        q[:, 0::4] = (packed >> 6) & 0x03
        q[:, 1::4] = (packed >> 4) & 0x03
        q[:, 2::4] = (packed >> 2) & 0x03
        q[:, 3::4] = packed & 0x03
        gray = _GRAY4_LUT[q[:, :w]]
        return Image.fromarray(gray, mode="L")
    return Image.open(io.BytesIO(data))


def decode_image_record_to_b64(rec: tuple[int, int, int, bytes] | None) -> str:
    """Decode an image record (mode, w, h, data) into a data URI."""
    if rec is None:
        return ""
    mode, _w, _h, data = rec
    if mode == 1:
        return "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    if mode == 2:
        return "data:image/webp;base64," + base64.b64encode(data).decode("ascii")
    im = decode_image_record_to_pil(rec)
    if im is None:
        return ""
    buf = io.BytesIO()
    im.save(buf, format="PNG", compress_level=6)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def pack_sequence(seq: SynthesisSequence) -> bytes:
    """Serialize a SynthesisSequence tree with 4-shade grayscale images and LZMA compression."""
    images: list[tuple[int, int, int, bytes]] = []

    def add_img(b64_uri: str | None) -> int:
        rec = encode_image_4gray(b64_uri)
        if rec is None:
            return -1
        idx = len(images)
        images.append(rec)
        return idx

    def ser_seq(s: SynthesisSequence) -> dict:
        return {
            "name": s.name,
            "path": s.path,
            "doi": s.doi,
            "start": add_img(s.start_b64),
            "citation": add_img(s.citation_b64),
            "steps": [
                {
                    "idx": st.step_index,
                    "rnum": st.reagent_num,
                    "reagent": add_img(st.reagent_b64),
                    "target": add_img(st.target_b64),
                    "nr": add_img(st.named_reaction_b64),
                    "sub": ser_seq(st.subsequence) if st.subsequence else None,
                }
                for st in s.steps
            ],
        }

    tree_bytes = json.dumps(ser_seq(seq), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    out = bytearray()
    out.extend(struct.pack("<I", len(tree_bytes)))
    out.extend(tree_bytes)
    for mode, w, h, data in images:
        out.extend(_IMG_HEADER.pack(mode, w, h, len(data)))
        out.extend(data)
    return lzma.compress(bytes(out), preset=3)


def _unpack_raw(payload: bytes) -> tuple[dict, list[tuple[int, int, int, bytes]]]:
    raw = lzma.decompress(payload)
    (tree_len,) = struct.unpack_from("<I", raw, 0)
    pos = 4
    tree = json.loads(raw[pos : pos + tree_len].decode("utf-8"))
    pos += tree_len
    images: list[tuple[int, int, int, bytes]] = []
    while pos < len(raw):
        mode, w, h, dlen = _IMG_HEADER.unpack_from(raw, pos)
        pos += _IMG_HEADER.size
        images.append((mode, w, h, raw[pos : pos + dlen]))
        pos += dlen
    return tree, images


def unpack_sequence(payload: bytes) -> SynthesisSequence:
    """Deserialize an LZMA-packed synthesis payload back into a SynthesisSequence."""
    tree, images = _unpack_raw(payload)

    def get_b64(idx: int) -> str:
        if idx < 0 or idx >= len(images):
            return ""
        return decode_image_record_to_b64(images[idx])

    def deser_seq(d: dict) -> SynthesisSequence:
        steps: list[StepData] = []
        for st in d.get("steps", []):
            nr_idx = st.get("nr", -1)
            sub_d = st.get("sub")
            steps.append(
                StepData(
                    step_index=st["idx"],
                    reagent_num=st["rnum"],
                    reagent_b64=get_b64(st["reagent"]),
                    target_b64=get_b64(st["target"]),
                    named_reaction_b64=get_b64(nr_idx) if nr_idx >= 0 else None,
                    subsequence=deser_seq(sub_d) if sub_d else None,
                )
            )
        cit_idx = d.get("citation", -1)
        return SynthesisSequence(
            name=d.get("name", ""),
            path=d.get("path", ""),
            doi=d.get("doi", ""),
            start_b64=get_b64(d["start"]),
            steps=steps,
            citation_b64=get_b64(cit_idx) if cit_idx >= 0 else None,
        )

    return deser_seq(tree)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: Path) -> sqlite3.Connection:
    """Create or open a cbyd.db SQLite file with schema initialized."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS catalog (
            path TEXT PRIMARY KEY,
            full TEXT NOT NULL,
            name TEXT NOT NULL,
            author TEXT NOT NULL,
            year TEXT NOT NULL,
            drug TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS syntheses (
            path TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            payload BLOB,
            error TEXT,
            updated_at REAL NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('format', ?)",
        (DB_FORMAT_VERSION,),
    )
    conn.commit()
    return conn


def load_catalog_from_db(db_path: Path) -> list[CatalogItem]:
    """Load all catalog entries stored in cbyd.db."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT full, path, year, drug, name, author FROM catalog"
        ).fetchall()
        return [
            CatalogItem(full=r[0], path=r[1], year=r[2], drug=r[3], name=r[4], author=r[5])
            for r in rows
        ]
    finally:
        conn.close()


def load_sequence_from_db(db_path: Path, item_path: str) -> SynthesisSequence | None:
    """Load and unpack a single SynthesisSequence from cbyd.db, or None if missing."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT payload FROM syntheses WHERE path = ? AND status = 'ok'",
            (item_path,),
        ).fetchone()
        if not row or not row[0]:
            return None
        return unpack_sequence(row[0])
    finally:
        conn.close()


def load_raw_sequence_from_db(
    db_path: Path, item_path: str
) -> tuple[dict, list[tuple[int, int, int, bytes]]] | None:
    """Fast-load raw (tree, image_records) from cbyd.db without PNG/base64 re-encoding."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT payload FROM syntheses WHERE path = ? AND status = 'ok'",
            (item_path,),
        ).fetchone()
        if not row or not row[0]:
            return None
        return _unpack_raw(row[0])
    finally:
        conn.close()


def load_target_preview_from_db(db_path: Path, item_path: str) -> Image.Image | None:
    """Fast-extract only the final target molecule PIL image from cbyd.db."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT payload FROM syntheses WHERE path = ? AND status = 'ok'",
            (item_path,),
        ).fetchone()
        if not row or not row[0]:
            return None
        tree, images = _unpack_raw(row[0])
        steps = tree.get("steps", [])
        img_idx = steps[-1]["target"] if steps else tree.get("start", -1)
        if 0 <= img_idx < len(images):
            return decode_image_record_to_pil(images[img_idx])
        return None
    finally:
        conn.close()


def get_db_stats(db_path: Path) -> dict[str, int]:
    """Return counts of catalog, downloaded ok, and error entries in cbyd.db."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cat_cnt = conn.execute("SELECT COUNT(*) FROM catalog").fetchone()[0]
        ok_cnt = conn.execute(
            "SELECT COUNT(*) FROM syntheses WHERE status = 'ok'"
        ).fetchone()[0]
        err_cnt = conn.execute(
            "SELECT COUNT(*) FROM syntheses WHERE status = 'error'"
        ).fetchone()[0]
        return {"catalog": cat_cnt, "ok": ok_cnt, "error": err_cnt}
    finally:
        conn.close()


async def build_database(
    db_path: Path,
    concurrency: int = 24,
    delay: float = 0.0,
    retry_errors: bool = False,
    limit: int | None = None,
    progress_cb: Callable[[int, int, str, int], None] | None = None,
) -> dict[str, int]:
    """Download syntheses from CByD and pack them into a 4-shade grayscale SQLite DB.

    Resumable: already-completed syntheses ('ok') are skipped automatically.
    Uses persistent queue workers, high keep-alive concurrency, and batched SQLite commits.
    """
    items, _ = await fetch_catalog(force_refresh=False)
    conn = init_db(db_path)

    conn.executemany(
        "INSERT OR REPLACE INTO catalog(path, full, name, author, year, drug) VALUES (?, ?, ?, ?, ?, ?)",
        [(it.path, it.full, it.name, it.author, it.year, it.drug) for it in items],
    )
    conn.commit()

    done_rows = conn.execute(
        "SELECT path, status FROM syntheses"
    ).fetchall()
    skip_paths = {
        r[0]
        for r in done_rows
        if r[1] == "ok" or (not retry_errors and r[1] == "error")
    }

    pending = [it for it in items if it.path not in skip_paths]
    if limit is not None and limit > 0:
        pending = pending[:limit]

    total = len(items)
    completed = sum(1 for r in done_rows if r[1] == "ok")
    errors = 0
    uncommitted = 0

    img_sem = asyncio.Semaphore(80)
    db_lock = asyncio.Lock()
    queue: asyncio.Queue[CatalogItem] = asyncio.Queue()
    for it in pending:
        queue.put_nowait(it)

    limits = httpx.Limits(max_connections=100, max_keepalive_connections=100)
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, limits=limits) as client:

            async def worker() -> None:
                nonlocal completed, errors, uncommitted
                while True:
                    try:
                        it = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        seq = await _download_sequence_recursive(
                            client=client,
                            sem=img_sem,
                            raw_path=it.path,
                            display_name=it.full,
                        )
                        blob = await asyncio.to_thread(pack_sequence, seq)
                        async with db_lock:
                            conn.execute(
                                "INSERT OR REPLACE INTO syntheses(path, status, payload, error, updated_at) "
                                "VALUES (?, 'ok', ?, NULL, ?)",
                                (it.path, blob, time.time()),
                            )
                            completed += 1
                            uncommitted += 1
                            if uncommitted >= 25:
                                conn.commit()
                                uncommitted = 0
                    except Exception as exc:
                        async with db_lock:
                            conn.execute(
                                "INSERT OR REPLACE INTO syntheses(path, status, payload, error, updated_at) "
                                "VALUES (?, 'error', NULL, ?, ?)",
                                (it.path, str(exc), time.time()),
                            )
                            errors += 1
                            uncommitted += 1
                            if uncommitted >= 25:
                                conn.commit()
                                uncommitted = 0
                    finally:
                        if progress_cb:
                            progress_cb(completed, total, it.full, errors)
                        queue.task_done()
                        if delay > 0:
                            await asyncio.sleep(delay)

            num_workers = min(max(1, concurrency), max(1, len(pending)))
            await asyncio.gather(*(worker() for _ in range(num_workers)))
    finally:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('updated_at', ?)",
            (str(int(time.time())),),
        )
        conn.commit()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
        except Exception:
            pass
        conn.close()

    return {"total": total, "ok": completed, "errors": errors}
