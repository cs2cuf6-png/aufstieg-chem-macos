"""CLI and TUI entry point for Chemistry By Design offline flashcard generator."""

from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from .api import (
    fetch_catalog,
    fetch_synthesis_bundle,
    make_safe_filename,
    search_catalog,
)
from .db import (
    build_database,
    find_db_path,
    load_catalog_from_db,
    load_sequence_from_db,
)
from .html_generator import save_flashcard_html
from .term_ui import run_term_ui

console = Console()


async def run_cli_generate(
    query: str,
    output_dir: Path,
    open_browser: bool,
    refresh: bool,
    db_path: Path | None = None,
) -> int:
    """Resolve query, load from cbyd.db or download synthesis, and save HTML flashcard."""
    resolved_db = find_db_path(db_path)
    items = None
    if resolved_db is not None and not refresh:
        items = load_catalog_from_db(resolved_db)
    if not items:
        with console.status("Загрузка каталога Chemistry By Design..."):
            items, _ = await fetch_catalog(force_refresh=refresh)

    matches = search_catalog(items, query, mode="all")
    if not matches:
        console.print(f"[red]Ничего не найдено по запросу:[/red] {query}")
        return 1

    item = matches[0]
    if len(matches) > 1 and matches[0].full.lower() != query.strip().lower():
        console.print(
            f"[yellow]Найдено {len(matches)} совпадений. Выбран лучший вариант:[/yellow] [bold]{item.full}[/bold]"
        )

    bundle = load_sequence_from_db(resolved_db, item.path) if resolved_db is not None else None
    if bundle is not None:
        console.print(f"[cyan]Из локальной базы:[/cyan] {resolved_db.name}")
    else:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            console=console,
        ) as progress:
            task_id = progress.add_task(f"Скачивание: {item.full}", total=1)

            def on_progress(done: int, total: int, _label: str) -> None:
                progress.update(task_id, total=max(1, total), completed=done)

            bundle = await fetch_synthesis_bundle(item, progress_cb=on_progress)

    filename = make_safe_filename(item)
    out_path = save_flashcard_html(item, bundle, output_dir, filename)
    console.print(f"[green]Готово![/green] Файл сохранён: [bold]{out_path}[/bold]")

    if open_browser:
        webbrowser.open(out_path.resolve().as_uri())

    return 0


async def run_cli_build_db(
    db_path: Path,
    retry_errors: bool = False,
    limit: int | None = None,
) -> int:
    """Download all syntheses and pack into a 4-shade grayscale SQLite database."""
    console.print(f"[bold cyan]Сборка офлайн-базы (4 оттенка серого + LZMA):[/bold cyan] {db_path}")
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task_id = progress.add_task("Подготовка...", total=1)

        def on_progress(done: int, total: int, name: str, errors: int) -> None:
            short = (name[:38] + "…") if len(name) > 39 else name
            err_txt = f" [red](ошибок: {errors})[/red]" if errors else ""
            progress.update(
                task_id,
                total=max(1, total),
                completed=done,
                description=f"{short}{err_txt}",
            )

        stats = await build_database(
            db_path=db_path,
            retry_errors=retry_errors,
            limit=limit,
            progress_cb=on_progress,
        )

    size_mb = db_path.stat().st_size / (1024 * 1024) if db_path.exists() else 0.0
    console.print(
        f"[green]Готово![/green] В базе: [bold]{stats['ok']}/{stats['total']}[/bold] синтезов, "
        f"ошибок: {stats['errors']}, размер: [bold]{size_mb:.1f} МБ[/bold]"
    )
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="cbyd",
        description="Генератор самодостаточных офлайн HTML-флешкарт с Chemistry By Design",
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="Ссылка на синтез CByD или название соединения (если не указано — запускается TUI)",
    )
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if sys.platform == "darwin" and exe_dir.name == "MacOS" and exe_dir.parent.name == "Contents":
            default_out = Path.home() / "Documents" / "Aufstieg" / "flashcards"
        else:
            default_out = exe_dir / "flashcards"
    else:
        default_out = Path.cwd() / "flashcards"
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=default_out,
        help="Папка для сохранения HTML-файлов (по умолчанию ./flashcards)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Автоматически открыть готовый HTML в браузере",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Принудительно обновить локальный кэш каталога CByD",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Запустить полноэкранный Textual TUI вместо терминального меню",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Путь к локальной базе cbyd.db (по умолчанию ищется автоматически)",
    )
    parser.add_argument(
        "--build-db",
        action="store_true",
        help="Скачать всю базу CByD и упаковать в cbyd.db (4 оттенка серого + LZMA, с докачкой)",
    )
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="При --build-db повторно скачать синтезы, завершившиеся ошибкой",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Ограничить число скачиваемых синтезов при --build-db (для теста)",
    )
    parser.add_argument(
        "--no-sixel",
        action="store_true",
        help="Отключить графический вывод Sixel и использовать текстовый рендер",
    )

    parser.add_argument("--view", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--view-title", default=None, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)

    if args.view:
        from .webview_window import run_view

        sys.exit(run_view(str(args.view), args.view_title))

    if args.build_db:
        target_db = args.db or (Path.cwd() / "cbyd.db")
        code = asyncio.run(
            run_cli_build_db(
                db_path=target_db,
                retry_errors=args.retry_errors,
                limit=args.limit,
            )
        )
        sys.exit(code)
    elif args.query:
        code = asyncio.run(
            run_cli_generate(
                query=args.query,
                output_dir=args.output,
                open_browser=args.open,
                refresh=args.refresh,
                db_path=args.db,
            )
        )
        sys.exit(code)
    elif args.tui:
        from .tui import CByDApp

        app = CByDApp(output_dir=args.output)
        app.run()
    else:
        run_term_ui(output_dir=args.output, db_path=args.db, use_sixel=not args.no_sixel)


if __name__ == "__main__":
    main()
