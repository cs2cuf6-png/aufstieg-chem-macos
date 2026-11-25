"""PyInstaller entry point for cbyd (Windows & macOS) with UTF-8, terminal relaunch, and crash-safe handling."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path


def _maybe_relaunch_in_macos_terminal() -> None:
    """If launched from Finder/Spotlight (.app) without a TTY on macOS, reopen inside Terminal.app."""
    if sys.platform != "darwin" or os.environ.get("CBYD_NO_TERM") == "1":
        return
    if len(sys.argv) > 1:
        return
    if sys.stdin and sys.stdout and sys.stdin.isatty() and sys.stdout.isatty():
        return

    try:
        cmd_parts = [sys.executable]
        if not getattr(sys, "frozen", False):
            cmd_parts.append(str(Path(__file__).resolve()))
        quoted_cmd = " ".join(shlex.quote(p) for p in cmd_parts)

        launcher = Path(tempfile.gettempdir()) / "aufstieg_launch.command"
        script = (
            "#!/bin/bash\n"
            "export CBYD_NO_TERM=1\n"
            "export TERM=${TERM:-xterm-256color}\n"
            "printf '\\033]0;Aufstieg | Химия\\007'\n"
            f"exec {quoted_cmd}\n"
        )
        launcher.write_text(script, encoding="utf-8")
        launcher.chmod(0o755)
        subprocess.Popen(
            ["open", "-a", "Terminal", str(launcher)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        sys.exit(0)
    except Exception:
        pass


def _maybe_relaunch_in_windows_terminal() -> None:
    """If double-clicked in legacy conhost.exe and Windows Terminal is installed, reopen in WT."""
    if os.name != "nt" or os.environ.get("CBYD_NO_WT") == "1":
        return
    if not (sys.stdin and sys.stdout and sys.stdin.isatty() and sys.stdout.isatty()):
        return
    # Only relaunch for interactive TUI (no CLI subcommands/flags like --help / --build-db / query)
    if len(sys.argv) > 1:
        return
    modern_vars = (
        "WT_SESSION",
        "TERM_PROGRAM",
        "WEZTERM_EXECUTABLE",
        "ALACRITTY_WINDOW_ID",
        "ConEmuPID",
    )
    if any(os.environ.get(v) for v in modern_vars):
        return

    wt = shutil.which("wt.exe")
    if not wt:
        local_app = os.environ.get("LOCALAPPDATA", "")
        cand = Path(local_app) / "Microsoft" / "WindowsApps" / "wt.exe"
        if cand.is_file():
            wt = str(cand)
    if not wt:
        return

    try:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("_PYI_", "_MEIPASS"))
        }
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and "PATH" in env:
            norm_mei = os.path.normcase(os.path.normpath(meipass))
            parts = [
                p
                for p in env["PATH"].split(os.pathsep)
                if os.path.normcase(os.path.normpath(p)) != norm_mei
            ]
            env["PATH"] = os.pathsep.join(parts)
        env["CBYD_NO_WT"] = "1"
        cmd = [wt, "--title", "Aufstieg | Химия", "-d", str(Path.cwd()), sys.executable]
        if not getattr(sys, "frozen", False):
            cmd.append(str(Path(__file__).resolve()))
        subprocess.Popen(cmd, env=env)
        sys.exit(0)
    except Exception:
        pass


def _configure_windows_console() -> None:
    """Force UTF-8 codepage, VT processing, and disable QuickEdit freeze on Windows conhost."""
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleOutputCP(65001)
            kernel32.SetConsoleCP(65001)
            # Enable ENABLE_PROCESSED_OUTPUT (0x0001) | ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004) on stdout
            STD_OUTPUT_HANDLE = -11
            h_out = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h_out, ctypes.byref(mode)):
                kernel32.SetConsoleMode(h_out, mode.value | 0x0001 | 0x0004)
            # Disable ENABLE_QUICK_EDIT_MODE (0x0040) on stdin so clicking the window
            # does not enter "Select / Выбрать" mode and freeze console rendering.
            STD_INPUT_HANDLE = -10
            h_in = kernel32.GetStdHandle(STD_INPUT_HANDLE)
            in_mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(h_in, ctypes.byref(in_mode)):
                ENABLE_QUICK_EDIT_MODE = 0x0040
                ENABLE_EXTENDED_FLAGS = 0x0080
                new_in_mode = (in_mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
                kernel32.SetConsoleMode(h_in, new_in_mode)
        except Exception:
            pass

    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _run() -> None:
    _maybe_relaunch_in_macos_terminal()
    _maybe_relaunch_in_windows_terminal()
    _configure_windows_console()
    if sys.platform == "darwin" and sys.stdout and sys.stdout.isatty() and len(sys.argv) <= 1:
        try:
            sys.stdout.write("\x1b]0;Aufstieg | Химия\x07")
            sys.stdout.flush()
        except Exception:
            pass
    try:
        from cbyd_flashcards.cli import main

        main()
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        if sys.stdin is not None and sys.stdin.isatty():
            try:
                input("\nПроизошла ошибка. Нажмите Enter для выхода...")
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    _run()
