"""Open a generated HTML flashcard in a native app window (WebView2 on Windows, WKWebView on macOS)."""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from pathlib import Path

WINDOW_TITLE = "Aufstieg | Химия"


def run_view(html_path: str, title: str | None = None) -> int:
    """Blocking: show the HTML file in a webview window (must run in its own process)."""
    import webview

    p = Path(html_path).resolve()
    webview.create_window(
        title or p.stem,
        url=p.as_uri(),
        width=1500,
        height=950,
        min_size=(640, 400),
        background_color="#0c0c0c",
    )
    webview.start()
    return 0


def open_in_window(html_path: Path, title: str | None = None) -> str:
    """Spawn the viewer window as a separate process; fall back to the browser on failure."""
    try:
        import webview  # noqa: F401
    except Exception:
        webbrowser.open(html_path.resolve().as_uri())
        return "pywebview не найден — открыто в браузере"

    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--view", str(html_path.resolve())]
    else:
        cmd = [sys.executable, "-m", "cbyd_flashcards", "--view", str(html_path.resolve())]
    if title:
        cmd += ["--view-title", title]
    try:
        popen_kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        else:
            popen_kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **popen_kwargs)
        return f"Открыто в окне: {html_path.name}"
    except Exception:
        webbrowser.open(html_path.resolve().as_uri())
        return "Окно не запустилось — открыто в браузере"
