"""Clean classic terminal UI with keyboard navigation (Rich + msvcrt/termios)."""

from __future__ import annotations

import asyncio
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .api import (
    CatalogItem,
    fetch_catalog,
    fetch_synthesis_bundle,
    fetch_target_preview_bytes,
    get_cached_preview_bytes,
    make_safe_filename,
    search_catalog,
)
from .webview_window import open_in_window
from .db import (
    find_db_path,
    load_catalog_from_db,
    load_sequence_from_db,
    load_target_preview_from_db,
)
from .conhost_gfx import (
    can_use_conhost_gdi,
    draw_dib_on_conhost,
    encode_molecule_dib,
    get_conhost_cell_size,
)
from .html_generator import LAYOUT_VERSION_TAG, save_flashcard_html
from .pixel_art import (
    get_mountain_pixel_art,
    is_legacy_windows_console,
    render_molecule_braille,
)
from .sixel import detect_sixel_support, encode_molecule_sixel

VISIBLE_ROWS = 16
PREVIEW_W = 36
PREVIEW_H = 10

_SPECIAL_LINK_URL = "https://t.me/Aufchem"
_SPECIAL_ITEMS = [
    CatalogItem(
        full="Kusmarcev Denis",
        path=f"__link__:{_SPECIAL_LINK_URL}",
        year="",
        drug="",
        name="Kusmarcev Denis",
        author="",
    ),
    CatalogItem(
        full="Safin Edward",
        path=f"__link__:{_SPECIAL_LINK_URL}",
        year="",
        drug="",
        name="Safin Edward",
        author="",
    ),
]


def _calc_visible_rows(console_height: int) -> int:
    """Stretch the synthesis list vertically while keeping 2-line footer hotkeys visible."""
    return max(5, console_height - 15)


def _find_contacts_file() -> Path | None:
    """Locate the creators contacts file (contacts.html / contacts.pdf / contacts.txt)."""
    bases = [Path.cwd()]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        bases.append(exe_dir)
        if sys.platform == "darwin":
            bases.append(exe_dir.parent / "Resources")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bases.append(Path(meipass))
    bases.append(Path(__file__).resolve().parent)
    bases.append(Path(__file__).resolve().parent.parent)
    for base in bases:
        for name in ("contacts.html", "contacts.pdf", "contacts.txt"):
            p = base / name
            if p.exists():
                return p
    return None


def _open_pdf_file(pdf_path: Path) -> None:
    """Open a PDF file with the OS default viewer (Preview on macOS, browser/viewer elsewhere)."""
    if sys.platform == "darwin":
        try:
            subprocess.Popen(
                ["open", str(pdf_path.resolve())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return
        except Exception:
            pass
    webbrowser.open(pdf_path.resolve().as_uri())


def _find_headless_browser() -> str | None:
    """Locate Edge, Chrome, or Chromium executable for HTML -> PDF compilation."""
    candidates: list[str | None] = [
        shutil.which("msedge"),
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    if sys.platform == "darwin":
        mac_apps = [
            "Google Chrome.app/Contents/MacOS/Google Chrome",
            "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "Brave Browser.app/Contents/MacOS/Brave Browser",
            "Chromium.app/Contents/MacOS/Chromium",
            "Arc.app/Contents/MacOS/Arc",
            "Vivaldi.app/Contents/MacOS/Vivaldi",
        ]
        for root in (Path("/Applications"), Path.home() / "Applications"):
            for rel in mac_apps:
                candidates.append(str(root / rel))
    elif sys.platform == "win32":
        candidates.extend([
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ])
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def _compile_html_to_pdf(html_path: Path, pdf_path: Path | None = None) -> Path:
    browser = _find_headless_browser()
    if not browser:
        if sys.platform == "darwin":
            raise RuntimeError("Не найден Chrome/Edge/Brave/Chromium для компиляции в PDF")
        raise RuntimeError("Не найден Edge или Chrome для компиляции в PDF")
    if pdf_path is None:
        pdf_path = html_path.with_suffix(".pdf")
    cmd = [
        browser,
        "--headless",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path.resolve()}",
        html_path.resolve().as_uri(),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0 or not pdf_path.exists():
        err_msg = (res.stderr or res.stdout or "").strip()
        raise RuntimeError(err_msg or f"Код возврата браузера: {res.returncode}")
    return pdf_path
_LEGACY_CONHOST = is_legacy_windows_console()
_SPINNER_FRAMES = (
    ("|", "/", "-", "\\")
    if _LEGACY_CONHOST
    else ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
)
_BRAND_ICON = "▲" if _LEGACY_CONHOST else "⛰"
_PREVIEW_ICON = "♦" if _LEGACY_CONHOST else "◈"
_CHECK_ICON = "√" if _LEGACY_CONHOST else "✓"


def _read_key(last_size: tuple[int, int] | None = None) -> str:
    """Read a single keypress cross-platform, returning 'RESIZE' if terminal size changes."""
    if os.name == "nt":
        import msvcrt

        while not msvcrt.kbhit():
            if last_size is not None:
                ts = shutil.get_terminal_size()
                if (ts.columns, ts.lines) != last_size:
                    return "RESIZE"
            time.sleep(0.03)

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            ext = msvcrt.getwch()
            if ext == "H":
                return "UP"
            if ext == "P":
                return "DOWN"
            if ext == "K":
                return "LEFT"
            if ext == "M":
                return "RIGHT"
            if ext == "I":
                return "PGUP"
            if ext == "Q":
                return "PGDN"
            return ""
        if ch == "\r":
            return "ENTER"
        if ch == "\x1b":
            return "ESC"
        if ch == "\x08":
            return "BACKSPACE"
        if ch in ("\x7f", "\x17"):
            return "CTRL_BACKSPACE"
        if ch == "\t":
            return "TAB"
        if ch == "\x01":
            return "CTRL_A"
        if ch == "\x02":
            return "CTRL_B"
        if ch == "\x03":
            return "CTRL_C"
        if ch == "\x0f":
            return "CTRL_O"
        if ch == "\x10":
            return "CTRL_P"
        if ch == "\x12":
            return "CTRL_R"
        if ch == "\x1a":
            return "CTRL_Z"
        if ord(ch) < 32:
            return ""
        return ch
    else:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                r, _, _ = select.select([fd], [], [], 0.03)
                if r:
                    break
                if last_size is not None:
                    ts = shutil.get_terminal_size()
                    if (ts.columns, ts.lines) != last_size:
                        return "RESIZE"

            b0 = os.read(fd, 1)
            if not b0:
                return ""
            code = b0[0]

            if code == 0x1B:  # ESC or escape sequence
                r, _, _ = select.select([fd], [], [], 0.03)
                if not r:
                    return "ESC"
                b1 = os.read(fd, 1)
                if not b1:
                    return "ESC"
                if b1 in (b"\x7f", b"\x08"):  # Option+Backspace on macOS
                    return "CTRL_BACKSPACE"
                if b1 in (b"[", b"O"):
                    seq = b1
                    for _ in range(8):
                        r, _, _ = select.select([fd], [], [], 0.02)
                        if not r:
                            break
                        bn = os.read(fd, 1)
                        if not bn:
                            break
                        seq += bn
                        if 0x40 <= bn[0] <= 0x7E:
                            break
                    if seq in (b"[A", b"OA"):
                        return "UP"
                    if seq in (b"[B", b"OB"):
                        return "DOWN"
                    if seq in (b"[C", b"OC"):
                        return "RIGHT"
                    if seq in (b"[D", b"OD"):
                        return "LEFT"
                    if seq == b"[5~":
                        return "PGUP"
                    if seq == b"[6~":
                        return "PGDN"
                    return ""
                return "ESC"

            if code in (0x0D, 0x0A):
                return "ENTER"
            if code in (0x7F, 0x08):
                return "BACKSPACE"
            if code == 0x17:  # Ctrl+W
                return "CTRL_BACKSPACE"
            if code == 0x09:
                return "TAB"
            if code == 0x01:
                return "CTRL_A"
            if code == 0x02:
                return "CTRL_B"
            if code == 0x03:
                return "CTRL_C"
            if code == 0x0F:
                return "CTRL_O"
            if code == 0x10:
                return "CTRL_P"
            if code == 0x12:
                return "CTRL_R"
            if code == 0x1A:
                return "CTRL_Z"
            if code < 32:
                return ""

            # Decode full UTF-8 character (e.g. Cyrillic)
            if code >= 0xC0:
                if code >= 0xF0:
                    need = 3
                elif code >= 0xE0:
                    need = 2
                else:
                    need = 1
                buf = bytearray(b0)
                for _ in range(need):
                    r, _, _ = select.select([fd], [], [], 0.02)
                    if not r:
                        break
                    buf.extend(os.read(fd, 1))
                return buf.decode("utf-8", errors="ignore")
            return b0.decode("ascii", errors="ignore")
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _existing_path(output_dir: Path, item: CatalogItem | None) -> Path | None:
    if not item or item.path.startswith("__link__:"):
        return None
    p = output_dir / make_safe_filename(item)
    return p if p.exists() else None


def _is_html_current(path: Path) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            return LAYOUT_VERSION_TAG not in f.read(512)
    except Exception:
        return False


def _build_progress_bar(done: int, total: int, width: int = 28) -> Text:
    pct = min(1.0, max(0.0, done / total)) if total > 0 else 0.0
    filled = int(round(pct * width))
    bar = Text()
    bar.append("━" * filled, style="bold cyan")
    bar.append("─" * (width - filled), style="dim bright_black")
    bar.append(f"  {int(pct * 100):3d}%", style="bold bright_blue")
    bar.append(f" ({done}/{total})", style="dim")
    return bar


def _placeholder_preview(msg: str) -> Text:
    """Create a fixed-height centered placeholder for the molecule preview box."""
    lines = [""] * PREVIEW_H
    lines[PREVIEW_H // 2] = msg.center(PREVIEW_W)
    return Text("\n".join(lines), style="dim", no_wrap=True)


def _render_ui(
    query: str,
    filtered: list[CatalogItem],
    selected_idx: int,
    scroll_offset: int,
    output_dir: Path,
    status_msg: str,
    total_catalog: int,
    download_state: dict | None = None,
    star_frame: int | None = None,
    single_star_step: int | None = None,
    preview_art: Text | None = None,
    db_active: bool = False,
    visible_rows: int = VISIBLE_ROWS,
    console_width: int = 120,
    console_height: int = 30,
) -> Group:
    # Header
    header = Text(no_wrap=True, overflow="ellipsis")
    header.append(f" {_BRAND_ICON}  Aufstieg | Химия (", style="bold bright_blue")
    header.append("@Aufchem", style="bold bright_blue link https://t.me/Aufchem")
    header.append(")", style="bold bright_blue")
    header.append("  ·  ", style="dim")
    header.append("Chemistry By Design", style="dim link https://chemistrybydesign.oia.arizona.edu/")
    header.append(f"  [{len(filtered)} / {total_catalog}]", style="dim cyan")

    # Search line
    search_line = Text(no_wrap=True, overflow="ellipsis")
    search_line.append(" Поиск › ", style="bold bright_blue")
    if query:
        search_line.append(query, style="bold white")
    else:
        search_line.append("введите название, автора, год или ссылку...", style="dim")
    search_line.append("█", style="bright_blue")

    search_panel = Panel(
        search_line,
        box=box.ROUNDED,
        border_style="bright_blue",
        padding=(0, 1),
    )

    # Results table
    table = Table(
        box=box.SIMPLE,
        expand=True,
        show_header=True,
        header_style="bold bright_blue",
        pad_edge=False,
        collapse_padding=True,
    )
    table.add_column("", width=2, no_wrap=True)
    table.add_column("Соединение", ratio=4, no_wrap=True, overflow="ellipsis")
    table.add_column("Автор", ratio=2, no_wrap=True, overflow="ellipsis")
    table.add_column("Год", width=6, justify="right", no_wrap=True)
    table.add_column("", width=2, justify="center", no_wrap=True)

    eff_rows = max(3, visible_rows - 2) if download_state is not None else visible_rows
    window = filtered[scroll_offset : scroll_offset + eff_rows]
    if not window:
        table.add_row("", "[dim]Ничего не найдено[/dim]", "", "", "")
        for _ in range(eff_rows - 1):
            table.add_row("", "", "", "", "")
    else:
        for i, item in enumerate(window):
            abs_idx = scroll_offset + i
            is_sel = abs_idx == selected_idx
            mark = "▸" if is_sel else " "
            done = _CHECK_ICON if _existing_path(output_dir, item) else ""
            row_style = "bold white on #1f3a5f" if is_sel else ""
            name_text = item.name or item.full
            table.add_row(
                Text(mark, style="bold cyan" if is_sel else "dim"),
                name_text,
                item.author or "—",
                item.year or "—",
                Text(done, style="bold green"),
                style=row_style,
            )
        for _ in range(eff_rows - len(window)):
            table.add_row("", "", "", "", "")

    list_panel = Panel(
        table,
        box=box.ROUNDED,
        border_style="bright_black",
        padding=(0, 1),
    )

    # Selected item details or active download widget
    if download_state is not None:
        frame = download_state["frame"]
        spinner = _SPINNER_FRAMES[frame % len(_SPINNER_FRAMES)]
        bar_w = min(28, max(8, console_width - 62)) if console_width >= 72 else min(28, max(8, console_width - 22))
        dl_line1 = Text(no_wrap=True, overflow="ellipsis")
        dl_line1.append(f" {spinner} ", style="bold cyan")
        dl_line1.append(download_state["stage"], style="bold white")
        dl_line2 = Text(" ", no_wrap=True, overflow="ellipsis")
        dl_line2.append_text(_build_progress_bar(download_state["done"], download_state["total"], width=bar_w))
        info_block: Group | Panel = Panel(
            Group(dl_line1, dl_line2),
            title=f"[bold cyan]{download_state['title']}[/bold cyan]",
            title_align="left",
            box=box.ROUNDED,
            border_style="cyan",
            padding=(0, 1),
        )
    else:
        sel_item = filtered[selected_idx] if 0 <= selected_idx < len(filtered) else None
        info_line1 = Text(no_wrap=True, overflow="ellipsis")
        if sel_item:
            existing = _existing_path(output_dir, sel_item)
            info_line1.append(f" {sel_item.full}", style="bold white")
            if sel_item.drug:
                info_line1.append(f"  ·  Препарат: {sel_item.drug}", style="dim")
            info_line1.append("  ·  ")
            if sel_item.path.startswith("__link__:"):
                info_line1.append(sel_item.path.split(":", 1)[1], style="cyan")
            elif existing:
                info_line1.append(f"{_CHECK_ICON} скачано", style="bold green")
            else:
                info_line1.append("ещё не скачан", style="dim")
        else:
            info_line1.append(" Нет выбранного синтеза", style="dim")

        info_line2 = Text(f" {status_msg}" if status_msg else " ", style="cyan", no_wrap=True, overflow="ellipsis")
        info_block = Group(info_line1, info_line2)

    # Footer hotkeys (2 compact rows so nothing ever truncates)
    keys_row1 = Text(no_wrap=True)
    for key, desc in [
        ("↑↓", "выбор"),
        ("Enter", "открыть"),
        ("Ctrl+Z", "очистить"),
        ("Ctrl+R", "случайное"),
    ]:
        keys_row1.append(f" {key}", style="bold bright_blue")
        keys_row1.append(f" {desc} ", style="dim")

    keys_row2 = Text(no_wrap=True)
    for key, desc in [
        ("Ctrl+P", "PDF"),
        ("Ctrl+O", "откр. PDF"),
        ("Ctrl+A", "контакты"),
        ("Esc", "выход"),
    ]:
        keys_row2.append(f" {key}", style="bold bright_blue")
        keys_row2.append(f" {desc} ", style="dim")

    left_col = Group(header, search_panel, list_panel, info_block, keys_row1, keys_row2)

    if console_width < 72:
        return Group(Text(""), left_col)

    right_header = Text(
        f" {_PREVIEW_ICON} Превью целевой молекулы",
        style="dim cyan",
        no_wrap=True,
        overflow="ellipsis",
    )

    sel_item = filtered[selected_idx] if 0 <= selected_idx < len(filtered) else None
    if sel_item is None:
        mol_body = _placeholder_preview("Выберите синтез")
    elif preview_art is not None:
        mol_body = preview_art
    else:
        mol_body = _placeholder_preview("Загрузка превью...")

    mol_title = sel_item.name if sel_item and sel_item.name else "Молекула"
    if len(mol_title) > 34:
        mol_title = mol_title[:33] + "…"
    preview_panel = Panel(
        Align.center(mol_body),
        title=f"[bold bright_blue]{mol_title}[/bold bright_blue]",
        title_align="left",
        box=box.ROUNDED,
        border_style="bright_blue",
        padding=(0, 1),
    )

    brand_caption = Text(
        "Aufstieg zu den Gipfeln",
        style="bold bright_blue",
        justify="center",
        no_wrap=True,
        overflow="ellipsis",
    )
    if console_height >= 28:
        if download_state is not None:
            f = download_state["frame"] // 2
            art = get_mountain_pixel_art(star_frame=f, flag_frame=f)
        else:
            art = get_mountain_pixel_art(
                star_frame=star_frame,
                flag_frame=star_frame,
                single_star_step=single_star_step,
            )
        right_col = Group(
            right_header,
            preview_panel,
            Align.center(art),
            Align.center(brand_caption),
        )
    else:
        right_col = Group(
            right_header,
            preview_panel,
            Align.center(brand_caption),
        )

    layout = Table.grid(expand=True, padding=(0, 2))
    layout.add_column(ratio=1)
    layout.add_column(width=40, no_wrap=True)
    layout.add_row(left_col, right_col)
    return Group(Text(""), layout)


def run_term_ui(
    output_dir: Path | None = None,
    db_path: Path | None = None,
    use_sixel: bool = True,
) -> None:
    """Run the classic interactive terminal UI with zero-flicker screen buffer."""
    out_dir = output_dir or (Path.cwd() / "flashcards")
    resolved_db = find_db_path(db_path)
    if use_sixel:
        supports_sixel, cell_w, cell_h = detect_sixel_support()
    else:
        supports_sixel, cell_w, cell_h = False, 9, 18
    use_gdi = (not supports_sixel) and can_use_conhost_gdi()
    if use_gdi:
        cell_w, cell_h = get_conhost_cell_size()
    box_w_px = PREVIEW_W * cell_w
    box_h_px = PREVIEW_H * cell_h
    real_stdout = sys.__stdout__ or sys.stdout
    console = Console()

    with console.status(
        "[cyan]Загрузка каталога Chemistry By Design...[/cyan]",
        spinner="line" if _LEGACY_CONHOST else "dots",
    ):
        try:
            catalog = load_catalog_from_db(resolved_db) if resolved_db is not None else []
            if catalog:
                status_msg = ""
            else:
                catalog, _ = asyncio.run(fetch_catalog(force_refresh=False))
                status_msg = ""
        except Exception as exc:
            console.print(f"[bold red]Ошибка загрузки каталога:[/bold red] {exc}")
            return

    catalog.extend(_SPECIAL_ITEMS)

    query = ""
    filtered = search_catalog(catalog, query, mode="all")
    selected_idx = 0
    scroll_offset = 0
    scroll_anim_frame = 0
    last_scroll_anim_t = 0.0
    action_step = 0

    def _advance_scroll_anim() -> int:
        nonlocal scroll_anim_frame, last_scroll_anim_t
        now = time.monotonic()
        if now - last_scroll_anim_t >= 0.15:
            scroll_anim_frame += 1
            last_scroll_anim_t = now
        return scroll_anim_frame

    preview_cache: dict[str, Text] = {}
    sixel_cache: dict[str, str] = {}
    dib_cache: dict[str, tuple[int, int, bytes]] = {}
    preview_lock = threading.Lock()
    ui_lock = threading.Lock()
    preview_seq = 0
    active_dib_box: list[tuple[int, int, bytes] | None] = [None]
    gdi_pos: list = [None, 3]  # custom (col, row) used while the chain viewer is open
    current_state = {
        "query": query,
        "filtered": filtered,
        "selected_idx": selected_idx,
        "scroll_offset": scroll_offset,
        "status_msg": status_msg,
    }

    def _build_preview(item: CatalogItem, source) -> tuple[Text, str | None, tuple[int, int, bytes] | None]:
        if supports_sixel:
            sx = encode_molecule_sixel(source, box_w_px, box_h_px)
            blank = Text("\n".join([" " * PREVIEW_W] * PREVIEW_H), no_wrap=True)
            return blank, sx, None
        if use_gdi:
            dib = encode_molecule_dib(source, box_w_px, box_h_px)
            blank = Text("\n".join([" " * PREVIEW_W] * PREVIEW_H), no_wrap=True)
            return blank, None, dib
        return render_molecule_braille(source, width_cells=PREVIEW_W, height_cells=PREVIEW_H), None, None

    def get_preview_for(item: CatalogItem | None) -> tuple[Text | None, str | None, tuple[int, int, bytes] | None]:
        if item is None:
            return None, None, None
        with preview_lock:
            cached = preview_cache.get(item.path)
            sx = sixel_cache.get(item.path)
            dib = dib_cache.get(item.path)
        if cached is not None:
            return cached, sx, dib
        if resolved_db is not None:
            pil_im = load_target_preview_from_db(resolved_db, item.path)
            if pil_im is not None:
                art, sx, dib = _build_preview(item, pil_im)
                with preview_lock:
                    preview_cache[item.path] = art
                    if sx is not None:
                        sixel_cache[item.path] = sx
                    if dib is not None:
                        dib_cache[item.path] = dib
                return art, sx, dib
        disk_bytes = get_cached_preview_bytes(item.path)
        if disk_bytes is not None:
            art, sx, dib = _build_preview(item, disk_bytes)
            with preview_lock:
                preview_cache[item.path] = art
                if sx is not None:
                    sixel_cache[item.path] = sx
                if dib is not None:
                    dib_cache[item.path] = dib
            return art, sx, dib
        return None, None, None

    def _draw_gfx_overlay(sx: str | None, dib: tuple[int, int, bytes] | None) -> None:
        if console.width < 72:
            active_dib_box[0] = None
            return
        # Right column: width=40, right padding=2 -> inner preview starts at col = width - 38, row = 4 (1 top blank line).
        col = max(1, console.width - 38)
        row = 4
        if supports_sixel and sx:
            real_stdout.write(f"\x1b7\x1b[{row};{col}H{sx}\x1b8")
            real_stdout.flush()
        elif use_gdi:
            active_dib_box[0] = dib
            if dib is not None:
                draw_dib_on_conhost(dib, col, row, cell_w, cell_h)

    last_rendered_size: list[tuple[int, int]] = [(console.size.width, console.size.height)]

    def _refresh_gdi_metrics() -> None:
        nonlocal cell_w, cell_h, box_w_px, box_h_px
        if not use_gdi:
            return
        new_cw, new_ch = get_conhost_cell_size()
        if (new_cw, new_ch) != (cell_w, cell_h):
            cell_w, cell_h = new_cw, new_ch
            box_w_px = PREVIEW_W * cell_w
            box_h_px = PREVIEW_H * cell_h
            with preview_lock:
                preview_cache.clear()
                dib_cache.clear()

    def update_screen(
        live_obj: Live,
        q: str,
        f_list: list[CatalogItem],
        s_idx: int,
        sc_off: int,
        st_msg: str,
        dl_st: dict | None = None,
        s_frame: int | None = None,
        ss_step: int | None = None,
        force_clear: bool = False,
    ) -> None:
        cur_size = (console.size.width, console.size.height)
        if cur_size != last_rendered_size[0]:
            last_rendered_size[0] = cur_size
            force_clear = True
            _refresh_gdi_metrics()
        sel = f_list[s_idx] if 0 <= s_idx < len(f_list) else None
        p_art, sx, dib = get_preview_for(sel)
        with ui_lock:
            if supports_sixel:
                real_stdout.write("\x1b[?2026h")
                real_stdout.flush()
            try:
                if force_clear:
                    real_stdout.write("\x1b[0m\x1b[2J\x1b[H")
                    real_stdout.flush()
                live_obj.update(
                    _render_ui(
                        q, f_list, s_idx, sc_off, out_dir, st_msg, len(catalog),
                        download_state=dl_st,
                        star_frame=s_frame,
                        single_star_step=ss_step,
                        preview_art=p_art,
                        db_active=resolved_db is not None,
                        visible_rows=_calc_visible_rows(cur_size[1]),
                        console_width=cur_size[0],
                        console_height=cur_size[1],
                    ),
                    refresh=True,
                )
                _draw_gfx_overlay(sx, dib)
            finally:
                if supports_sixel:
                    real_stdout.write("\x1b[?2026l")
                    real_stdout.flush()

    def schedule_preview_fetch(live_obj: Live, item: CatalogItem | None) -> None:
        nonlocal preview_seq
        if item is not None and item.path.startswith("__link__:"):
            return
        p_art, _, _ = get_preview_for(item)
        if item is None or p_art is not None:
            return
        preview_seq += 1
        my_seq = preview_seq
        target_item = item

        def _worker() -> None:
            time.sleep(0.18)
            if my_seq != preview_seq:
                return
            sx: str | None = None
            dib: tuple[int, int, bytes] | None = None
            try:
                raw = asyncio.run(fetch_target_preview_bytes(target_item))
                if raw is None:
                    art = _placeholder_preview("Нет превью")
                else:
                    art, sx, dib = _build_preview(target_item, raw)
            except Exception:
                art = _placeholder_preview("Ошибка превью")
            with preview_lock:
                preview_cache[target_item.path] = art
                if sx is not None:
                    sixel_cache[target_item.path] = sx
                if dib is not None:
                    dib_cache[target_item.path] = dib
            if my_seq != preview_seq:
                return
            f_list = current_state["filtered"]
            s_idx = current_state["selected_idx"]
            cur_it = f_list[s_idx] if 0 <= s_idx < len(f_list) else None
            if cur_it and cur_it.path == target_item.path:
                update_screen(
                    live_obj,
                    current_state["query"],
                    f_list,
                    s_idx,
                    current_state["scroll_offset"],
                    current_state["status_msg"],
                )

        threading.Thread(target=_worker, daemon=True).start()

    initial_item = filtered[selected_idx] if filtered else None
    initial_preview, initial_sixel, initial_dib = get_preview_for(initial_item)

    stop_gdi = threading.Event()
    if use_gdi:
        def _gdi_keepalive() -> None:
            while not stop_gdi.wait(0.12):
                dib = active_dib_box[0]
                if dib is not None and ui_lock.acquire(blocking=False):
                    try:
                        if gdi_pos[0] is not None:
                            draw_dib_on_conhost(dib, gdi_pos[0], gdi_pos[1], cell_w, cell_h)
                        elif console.width >= 72:
                            col = max(1, console.width - 38)
                            draw_dib_on_conhost(dib, col, 4, cell_w, cell_h)
                    finally:
                        ui_lock.release()

        threading.Thread(target=_gdi_keepalive, daemon=True).start()

    init_size = (console.size.width, console.size.height)
    last_rendered_size[0] = init_size
    with Live(
        _render_ui(
            query, filtered, selected_idx, scroll_offset, out_dir, status_msg, len(catalog),
            preview_art=initial_preview,
            db_active=resolved_db is not None,
            visible_rows=_calc_visible_rows(init_size[1]),
            console_width=init_size[0],
            console_height=init_size[1],
        ),
        console=console,
        screen=True,
        auto_refresh=False,
        redirect_stdout=False,
        redirect_stderr=False,
    ) as live:
        _draw_gfx_overlay(initial_sixel, initial_dib)
        schedule_preview_fetch(live, initial_item)
        while True:
            key = _read_key(last_rendered_size[0])
            cur_star_frame: int | None = None
            cur_single_star: int | None = None

            if key == "RESIZE":
                vis_rows = _calc_visible_rows(console.size.height)
                if selected_idx < scroll_offset:
                    scroll_offset = selected_idx
                elif selected_idx >= scroll_offset + vis_rows:
                    scroll_offset = max(0, selected_idx - vis_rows + 1)
                current_state["scroll_offset"] = scroll_offset
                update_screen(
                    live, query, filtered, selected_idx, scroll_offset, status_msg,
                    force_clear=True,
                )
                continue

            if key in ("ESC", "CTRL_C"):
                stop_gdi.set()
                return

            elif key == "UP":
                cur_star_frame = _advance_scroll_anim()
                if selected_idx > 0:
                    selected_idx -= 1
                    if selected_idx < scroll_offset:
                        scroll_offset = selected_idx

            elif key == "DOWN":
                cur_star_frame = _advance_scroll_anim()
                vis_rows = _calc_visible_rows(console.size.height)
                if selected_idx < len(filtered) - 1:
                    selected_idx += 1
                    if selected_idx >= scroll_offset + vis_rows:
                        scroll_offset = selected_idx - vis_rows + 1

            elif key == "PGUP":
                cur_star_frame = _advance_scroll_anim()
                vis_rows = _calc_visible_rows(console.size.height)
                selected_idx = max(0, selected_idx - vis_rows)
                scroll_offset = max(0, scroll_offset - vis_rows)

            elif key == "PGDN":
                cur_star_frame = _advance_scroll_anim()
                vis_rows = _calc_visible_rows(console.size.height)
                if filtered:
                    selected_idx = min(len(filtered) - 1, selected_idx + vis_rows)
                    if selected_idx >= scroll_offset + vis_rows:
                        scroll_offset = selected_idx - vis_rows + 1

            elif key == "BACKSPACE":
                action_step += 1
                cur_single_star = action_step
                if query:
                    query = query[:-1]
                    filtered = search_catalog(catalog, query, mode="all")
                    selected_idx = 0
                    scroll_offset = 0

            elif key == "CTRL_BACKSPACE":
                action_step += 1
                cur_single_star = action_step
                if query:
                    stripped = query.rstrip()
                    idx = stripped.rfind(" ")
                    query = stripped[: idx + 1] if idx != -1 else ""
                    filtered = search_catalog(catalog, query, mode="all")
                    selected_idx = 0
                    scroll_offset = 0

            elif key == "CTRL_Z":
                action_step += 1
                cur_single_star = action_step
                if query:
                    query = ""
                    filtered = search_catalog(catalog, query, mode="all")
                    selected_idx = 0
                    scroll_offset = 0

            elif key == "CTRL_R":
                action_step += 1
                cur_single_star = action_step
                if not filtered and catalog:
                    query = ""
                    filtered = search_catalog(catalog, query, mode="all")
                if filtered:
                    rand_idx = random.randrange(len(filtered))
                    if len(filtered) > 1 and rand_idx == selected_idx:
                        rand_idx = (rand_idx + 1) % len(filtered)
                    selected_idx = rand_idx
                    vis_rows = _calc_visible_rows(console.size.height)
                    if selected_idx < scroll_offset:
                        scroll_offset = selected_idx
                    elif selected_idx >= scroll_offset + vis_rows:
                        scroll_offset = selected_idx - vis_rows + 1
                    status_msg = f"Случайный выбор: {filtered[selected_idx].full}"

            elif key == "CTRL_A":
                action_step += 1
                cur_single_star = action_step
                contacts_path = _find_contacts_file()
                if contacts_path is not None:
                    webbrowser.open(contacts_path.resolve().as_uri())
                    status_msg = f"Открыты контакты: {contacts_path.name}"
                else:
                    status_msg = "Файл контактов (contacts.html) не найден"

            elif key == "CTRL_O":
                action_step += 1
                cur_single_star = action_step
                if 0 <= selected_idx < len(filtered):
                    item = filtered[selected_idx]
                    if item.path.startswith("__link__:"):
                        status_msg = "PDF недоступен для ссылки"
                    else:
                        pdf_path = (out_dir / make_safe_filename(item)).with_suffix(".pdf")
                        if pdf_path.exists():
                            _open_pdf_file(pdf_path)
                            status_msg = f"Открыт PDF: {pdf_path.name}"
                        else:
                            status_msg = "PDF ещё не создан (нажмите Ctrl+P)"

            elif key == "CTRL_P":
                action_step += 1
                cur_single_star = action_step
                if 0 <= selected_idx < len(filtered):
                    item = filtered[selected_idx]
                    if item.path.startswith("__link__:"):
                        status_msg = "PDF недоступен для ссылки"
                    else:
                        dl_state = {
                            "title": f"PDF: {item.full}",
                            "stage": "Подготовка HTML-флешкарты...",
                            "done": 0,
                            "total": 100,
                            "frame": 0,
                        }
                        result_box = []

                        def on_progress_pdf(done: int, total: int, label: str) -> None:
                            if total > 0:
                                dl_state["done"] = done
                                dl_state["total"] = total

                        def _pdf_worker() -> None:
                            tmp_html: Path | None = None
                            try:
                                bundle = (
                                    load_sequence_from_db(resolved_db, item.path)
                                    if resolved_db is not None
                                    else None
                                )
                                if bundle is None:
                                    bundle = asyncio.run(
                                        fetch_synthesis_bundle(item, progress_cb=on_progress_pdf)
                                    )
                                else:
                                    dl_state["done"] = 50
                                    dl_state["total"] = 100
                                dl_state["stage"] = "Сборка HTML для печати A4..."
                                filename = make_safe_filename(item)
                                tmp_html = save_flashcard_html(
                                    item,
                                    bundle,
                                    out_dir,
                                    f".tmp_pdf_{filename}",
                                    for_pdf=True,
                                )
                                dl_state["done"] = 75
                                dl_state["total"] = 100
                                dl_state["stage"] = "Компиляция HTML в PDF..."
                                target_pdf = (out_dir / filename).with_suffix(".pdf")
                                pdf_path = _compile_html_to_pdf(tmp_html, target_pdf)
                                dl_state["done"] = 100
                                result_box.append((pdf_path, None))
                            except Exception as exc:
                                result_box.append((None, exc))
                            finally:
                                if tmp_html is not None:
                                    try:
                                        tmp_html.unlink(missing_ok=True)
                                    except Exception:
                                        pass

                        t = threading.Thread(target=_pdf_worker, daemon=True)
                        t.start()
                        while t.is_alive():
                            dl_state["frame"] += 1
                            update_screen(
                                live, query, filtered, selected_idx, scroll_offset, status_msg, dl_st=dl_state
                            )
                            time.sleep(0.08)

                        pdf_path, err = result_box[0]
                        if err is None and pdf_path is not None:
                            status_msg = f"{_CHECK_ICON} PDF сохранён: {pdf_path.name}"
                        else:
                            status_msg = f"Ошибка PDF: {err}"

            elif key == "ENTER":
                action_step += 1
                cur_single_star = action_step
                if 0 <= selected_idx < len(filtered):
                    item = filtered[selected_idx]
                    if item.path.startswith("__link__:"):
                        url = item.path.split(":", 1)[1]
                        webbrowser.open(url)
                        status_msg = f"Открыто: {url}"
                        current_state["status_msg"] = status_msg
                        update_screen(
                            live, query, filtered, selected_idx, scroll_offset, status_msg,
                            s_frame=cur_star_frame, ss_step=cur_single_star,
                        )
                        continue
                    existing = _existing_path(out_dir, item)
                    if existing is not None and _is_html_current(existing):
                        status_msg = open_in_window(existing, item.full)
                    else:
                        dl_state = {
                            "title": item.full,
                            "stage": "Скачивание структур и стадий...",
                            "done": 0,
                            "total": 100,
                            "frame": 0,
                        }
                        result_box = []

                        def on_progress(done: int, total: int, label: str) -> None:
                            if total > 0:
                                dl_state["done"] = done
                                dl_state["total"] = total

                        def _dl_worker() -> None:
                            try:
                                bundle = (
                                    load_sequence_from_db(resolved_db, item.path)
                                    if resolved_db is not None
                                    else None
                                )
                                if bundle is None:
                                    bundle = asyncio.run(fetch_synthesis_bundle(item, progress_cb=on_progress))
                                else:
                                    dl_state["done"] = 1
                                    dl_state["total"] = 1
                                dl_state["stage"] = "Сборка HTML-флешкарты («змейка»)..."
                                filename = make_safe_filename(item)
                                out_path = save_flashcard_html(item, bundle, out_dir, filename)
                                result_box.append((out_path, None))
                            except Exception as exc:
                                result_box.append((None, exc))

                        t = threading.Thread(target=_dl_worker, daemon=True)
                        t.start()
                        while t.is_alive():
                            dl_state["frame"] += 1
                            update_screen(
                                live, query, filtered, selected_idx, scroll_offset, status_msg, dl_st=dl_state
                            )
                            time.sleep(0.08)

                        out_path, err = result_box[0]
                        if err is None and out_path is not None:
                            status_msg = f"{_CHECK_ICON} Сохранено: {out_path.name} (Enter — открыть)"
                        else:
                            status_msg = f"Ошибка: {err}"

            elif len(key) == 1 and key.isprintable():
                action_step += 1
                cur_single_star = action_step
                query += key
                filtered = search_catalog(catalog, query, mode="all")
                selected_idx = 0
                scroll_offset = 0

            current_state["query"] = query
            current_state["filtered"] = filtered
            current_state["selected_idx"] = selected_idx
            current_state["scroll_offset"] = scroll_offset
            current_state["status_msg"] = status_msg

            sel_item = filtered[selected_idx] if 0 <= selected_idx < len(filtered) else None
            update_screen(
                live, query, filtered, selected_idx, scroll_offset, status_msg,
                s_frame=cur_star_frame, ss_step=cur_single_star,
            )
            p_art, _, _ = get_preview_for(sel_item)
            if p_art is None:
                schedule_preview_fetch(live, sel_item)
