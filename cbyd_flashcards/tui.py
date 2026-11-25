"""Fullscreen Textual TUI for Chemistry By Design offline flashcard generator."""

from __future__ import annotations

import random
import webbrowser
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    Static,
)

from .api import (
    CatalogItem,
    fetch_catalog,
    fetch_synthesis_bundle,
    make_safe_filename,
    search_catalog,
)
from .html_generator import save_flashcard_html


class CByDApp(App):
    """Interactive terminal UI for Chemistry By Design."""

    TITLE = "Aufstieg | Химия — Chemistry By Design"
    CSS = """
    Screen {
        layout: vertical;
        background: #0d1117;
        color: #e6edf3;
    }

    Header {
        background: #161b22;
        color: #79c0ff;
        text-style: bold;
    }

    Footer {
        background: #161b22;
        color: #8b949e;
    }

    #top-bar {
        height: auto;
        padding: 1 2 0 2;
        layout: vertical;
    }

    #search-input {
        width: 100%;
        margin-bottom: 1;
        background: #161b22;
        border: round #30363d;
        color: #f0f6fc;
        padding: 0 1;
    }

    #search-input:focus {
        border: round #58a6ff;
        background: #1c2128;
    }

    #main-body {
        height: 1fr;
        padding: 0 2 1 2;
    }

    #results-table {
        width: 1.25fr;
        height: 1fr;
        background: #161b22;
        border: round #30363d;
    }

    #results-table:focus {
        border: round #58a6ff;
    }

    #results-table > .datatable--header {
        background: #1c2128;
        color: #79c0ff;
        text-style: bold;
    }

    #results-table > .datatable--cursor {
        background: #1f3a5f;
        color: #ffffff;
        text-style: bold;
    }

    #side-panel {
        width: 1fr;
        min-width: 40;
        height: 1fr;
        margin-left: 2;
        padding: 1 2;
        background: #161b22;
        border: round #30363d;
        layout: vertical;
    }

    #brand-box {
        height: auto;
        margin-bottom: 1;
        padding-bottom: 1;
        border-bottom: solid #21262d;
    }

    #brand-title {
        text-style: bold;
        color: #79c0ff;
    }

    #brand-sub {
        color: #8b949e;
    }

    #meta-title {
        text-style: bold;
        color: #f0f6fc;
        margin-bottom: 1;
    }

    #meta-details {
        height: auto;
        color: #c9d1d9;
        margin-bottom: 1;
    }

    #action-buttons {
        margin-top: 1;
        margin-bottom: 1;
        height: 3;
        layout: horizontal;
    }

    #generate-btn {
        width: 1fr;
        margin-right: 1;
        background: #1f6feb;
        color: #ffffff;
        border: none;
        text-style: bold;
    }

    #generate-btn:hover {
        background: #388bfd;
    }

    #open-btn {
        width: 1fr;
        background: #238636;
        color: #ffffff;
        border: none;
        text-style: bold;
    }

    #open-btn:hover {
        background: #2ea043;
    }

    #status-label {
        margin-top: 1;
        color: #8b949e;
    }

    #progress-bar {
        margin-top: 1;
        display: none;
    }

    #progress-bar.-visible {
        display: block;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Выход"),
        Binding("ctrl+r", "open_random", "Случайное"),
        Binding("ctrl+o", "open_selected_or_last", "Открыть HTML"),
        Binding("slash", "focus_search", "Поиск"),
    ]

    def __init__(self, output_dir: Path | None = None) -> None:
        super().__init__()
        self.output_dir = output_dir or (Path.cwd() / "flashcards")
        self.catalog: list[CatalogItem] = []
        self.filtered: list[CatalogItem] = []
        self.selected_item: CatalogItem | None = None
        self.last_generated_path: Path | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="top-bar"):
            yield Input(
                placeholder="Поиск по названию, автору, году или ссылке Chemistry By Design...",
                id="search-input",
            )
        with Horizontal(id="main-body"):
            yield DataTable(id="results-table", cursor_type="row", zebra_stripes=True)
            with Vertical(id="side-panel"):
                with Vertical(id="brand-box"):
                    yield Static("Aufstieg | Химия", id="brand-title")
                    yield Static(
                        "Офлайн HTML-флешкарты · Chemistry By Design",
                        id="brand-sub",
                    )
                yield Static("Выберите синтез", id="meta-title")
                yield Static("Загрузка каталога...", id="meta-details")
                with Horizontal(id="action-buttons"):
                    yield Button(
                        "Скачать (Enter)",
                        id="generate-btn",
                        variant="primary",
                        disabled=True,
                    )
                    yield Button(
                        "Открыть",
                        id="open-btn",
                        variant="success",
                        disabled=True,
                    )
                yield ProgressBar(id="progress-bar", total=100, show_eta=False)
                yield Label("", id="status-label")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#results-table", DataTable)
        table.add_columns("Соединение", "Автор", "Год")
        self.load_catalog(force_refresh=False)

    @work(exclusive=True)
    async def load_catalog(self, force_refresh: bool = False) -> None:
        status = self.query_one("#status-label", Label)
        status.update("Загрузка каталога CByD...")
        try:
            items, from_cache = await fetch_catalog(force_refresh=force_refresh)
            self.catalog = items
            src = "из кэша" if from_cache else "с сайта CByD"
            status.update(f"Загружено {len(items)} синтезов ({src}).")
            self.apply_filter()
        except Exception as exc:
            status.update(f"Ошибка загрузки каталога: {exc}")

    def _existing_path_for(self, item: CatalogItem | None) -> Path | None:
        if not item:
            return None
        candidate = self.output_dir / make_safe_filename(item)
        return candidate if candidate.exists() else None

    def apply_filter(self) -> None:
        query = self.query_one("#search-input", Input).value
        self.filtered = search_catalog(self.catalog, query, mode="all")
        table = self.query_one("#results-table", DataTable)
        table.clear()

        display_slice = self.filtered[:500]
        for idx, it in enumerate(display_slice):
            table.add_row(it.name or it.full, it.author, it.year, key=str(idx))

        if display_slice:
            self.selected_item = display_slice[0]
            self.update_side_panel()
        else:
            self.selected_item = None
            self.query_one("#meta-title", Static).update("Ничего не найдено")
            self.query_one("#meta-details", Static).update(
                "[dim]Попробуйте изменить поисковый запрос.[/dim]"
            )
            self.query_one("#generate-btn", Button).disabled = True
            self.query_one("#open-btn", Button).disabled = self.last_generated_path is None

    def update_side_panel(self) -> None:
        it = self.selected_item
        if not it:
            return
        self.query_one("#meta-title", Static).update(it.full)
        existing = self._existing_path_for(it)
        file_status = (
            "[bold #3fb950]✓ уже скачан[/bold #3fb950]"
            if existing
            else "[dim]ещё не создан[/dim]"
        )
        lines = [
            f"[dim]Соединение:[/dim]  {it.name or '—'}",
            f"[dim]Автор:[/dim]       {it.author or '—'}",
            f"[dim]Год:[/dim]         {it.year or '—'}",
            f"[dim]Препарат:[/dim]    {it.drug or '—'}",
            f"[dim]Найдено:[/dim]     {len(self.filtered)}",
            f"[dim]Файл HTML:[/dim]   {file_status}",
        ]
        self.query_one("#meta-details", Static).update("\n".join(lines))
        self.query_one("#generate-btn", Button).disabled = False
        self.query_one("#open-btn", Button).disabled = (
            existing is None and self.last_generated_path is None
        )

    @on(Input.Changed, "#search-input")
    def on_search_changed(self) -> None:
        self.apply_filter()

    @on(Input.Submitted, "#search-input")
    def on_search_submitted(self) -> None:
        if self.selected_item:
            self.start_generation(self.selected_item)

    @on(DataTable.RowHighlighted, "#results-table")
    def on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key and event.row_key.value is not None:
            idx = int(event.row_key.value)
            if 0 <= idx < len(self.filtered):
                self.selected_item = self.filtered[idx]
                self.update_side_panel()

    @on(DataTable.RowSelected, "#results-table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value is not None:
            idx = int(event.row_key.value)
            if 0 <= idx < len(self.filtered):
                self.selected_item = self.filtered[idx]
                self.start_generation(self.selected_item)

    @on(Button.Pressed, "#generate-btn")
    def on_generate_clicked(self) -> None:
        if self.selected_item:
            self.start_generation(self.selected_item)

    @on(Button.Pressed, "#open-btn")
    def on_open_clicked(self) -> None:
        self.action_open_selected_or_last()

    @work(exclusive=True)
    async def start_generation(self, item: CatalogItem) -> None:
        pb = self.query_one("#progress-bar", ProgressBar)
        status = self.query_one("#status-label", Label)
        gen_btn = self.query_one("#generate-btn", Button)
        open_btn = self.query_one("#open-btn", Button)

        gen_btn.disabled = True
        pb.add_class("-visible")
        pb.update(total=100, progress=0)
        status.update(f"Скачивание: {item.full}...")

        def on_progress(done: int, total: int, label: str) -> None:
            if total > 0:
                pb.update(total=total, progress=done)

        try:
            bundle = await fetch_synthesis_bundle(item, progress_cb=on_progress)
            filename = make_safe_filename(item)
            out_path = save_flashcard_html(item, bundle, self.output_dir, filename)
            self.last_generated_path = out_path
            pb.update(total=100, progress=100)
            status.update(f"Готово! Сохранено:\n{out_path.name}")
            open_btn.disabled = False
            self.update_side_panel()
        except Exception as exc:
            status.update(f"Ошибка генерации: {exc}")
        finally:
            gen_btn.disabled = False

    def action_open_random(self) -> None:
        pool = getattr(self, "filtered_items", None) or getattr(self, "catalog", None) or []
        if not pool:
            return
        idx = random.randrange(len(pool))
        self.selected_item = pool[idx]
        table = self.query_one(DataTable)
        if 0 <= idx < table.row_count:
            table.move_cursor(row=idx)

    def action_open_selected_or_last(self) -> None:
        target = self._existing_path_for(self.selected_item) or self.last_generated_path
        if target and target.exists():
            webbrowser.open(target.resolve().as_uri())

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()
