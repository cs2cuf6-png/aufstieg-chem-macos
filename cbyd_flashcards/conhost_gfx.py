"""Win32 GDI high-resolution bitmap overlay for legacy Windows conhost.exe."""

from __future__ import annotations

import ctypes
import io
import os

import numpy as np
from PIL import Image

from .pixel_art import is_legacy_windows_console

if os.name == "nt":
    from ctypes import wintypes

    class _COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class _SMALL_RECT(ctypes.Structure):
        _fields_ = [
            ("Left", ctypes.c_short),
            ("Top", ctypes.c_short),
            ("Right", ctypes.c_short),
            ("Bottom", ctypes.c_short),
        ]

    class _CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
        _fields_ = [
            ("dwSize", _COORD),
            ("dwCursorPosition", _COORD),
            ("wAttributes", wintypes.WORD),
            ("srWindow", _SMALL_RECT),
            ("dwMaximumWindowSize", _COORD),
        ]

    class _CONSOLE_FONT_INFOEX(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.ULONG),
            ("nFont", wintypes.DWORD),
            ("dwFontSize", _COORD),
            ("FontFamily", wintypes.UINT),
            ("FontWeight", wintypes.UINT),
            ("FaceName", ctypes.c_wchar * 32),
        ]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class _RGBQUAD(ctypes.Structure):
        _fields_ = [
            ("rgbBlue", ctypes.c_ubyte),
            ("rgbGreen", ctypes.c_ubyte),
            ("rgbRed", ctypes.c_ubyte),
            ("rgbReserved", ctypes.c_ubyte),
        ]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", _RGBQUAD * 1)]


def _setup_win32():
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    kernel32.GetConsoleWindow.restype = wintypes.HWND
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    gdi32.SetDIBitsToDevice.argtypes = [
        wintypes.HDC,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
    ]
    gdi32.SetDIBitsToDevice.restype = ctypes.c_int
    return kernel32, user32, gdi32


def can_use_conhost_gdi() -> bool:
    """Return True if running directly inside a visible legacy Windows conhost.exe window."""
    if os.name != "nt" or not is_legacy_windows_console():
        return False
    try:
        kernel32, user32, _ = _setup_win32()
        hwnd = kernel32.GetConsoleWindow()
        return bool(hwnd) and bool(user32.IsWindowVisible(hwnd))
    except Exception:
        return False


def get_conhost_cell_size() -> tuple[int, int]:
    """Return exact (cell_w, cell_h) in GetDC(hwnd) pixels for the current conhost.exe window.

    Prefers GetClientRect / visible (cols, rows) over GetCurrentConsoleFontEx because
    GetCurrentConsoleFontEx reports unscaled logical metrics on HiDPI Windows displays
    (125%/150%/175%), whereas GetClientRect matches the exact coordinate space of SetDIBitsToDevice.
    """
    if os.name != "nt":
        return 8, 16
    try:
        kernel32, user32, _ = _setup_win32()
        hwnd = kernel32.GetConsoleWindow()
        h_out = kernel32.GetStdHandle(wintypes.DWORD(-11 & 0xFFFFFFFF))

        csbi = _CONSOLE_SCREEN_BUFFER_INFO()
        rect = wintypes.RECT()
        if (
            hwnd
            and not user32.IsIconic(hwnd)
            and kernel32.GetConsoleScreenBufferInfo(h_out, ctypes.byref(csbi))
            and user32.GetClientRect(hwnd, ctypes.byref(rect))
        ):
            cols = csbi.srWindow.Right - csbi.srWindow.Left + 1
            rows = csbi.srWindow.Bottom - csbi.srWindow.Top + 1
            client_w = rect.right - rect.left
            client_h = rect.bottom - rect.top
            if cols >= 20 and rows >= 10 and client_w > 0 and client_h > 0:
                cw = client_w // cols
                ch = client_h // rows
                if 4 <= cw <= 48 and 8 <= ch <= 96:
                    return cw, ch

        fi = _CONSOLE_FONT_INFOEX()
        fi.cbSize = ctypes.sizeof(_CONSOLE_FONT_INFOEX)
        if kernel32.GetCurrentConsoleFontEx(h_out, False, ctypes.byref(fi)):
            cw, ch = int(fi.dwFontSize.X), int(fi.dwFontSize.Y)
            if 4 <= cw <= 48 and 8 <= ch <= 96:
                return cw, ch
    except Exception:
        pass
    return 8, 16


def encode_molecule_dib(
    source: Image.Image | bytes,
    box_w_px: int,
    box_h_px: int,
    bg_rgb: tuple[int, int, int] = (12, 12, 12),
) -> tuple[int, int, bytes]:
    """Prepare a crisp 32-bit BGRA top-down DIB buffer of a dark-mode chemical structure."""
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

    # Crisp dark-mode inversion: white ink on conhost background (#0c0c0c)
    inv = np.clip((255.0 - np.asarray(canvas, dtype=np.float32)) / 255.0, 0.0, 1.0)
    inv = np.power(inv, 0.85)

    r = np.clip(bg_rgb[0] + inv * (242 - bg_rgb[0]), 0, 255).astype(np.uint8)
    g = np.clip(bg_rgb[1] + inv * (246 - bg_rgb[1]), 0, 255).astype(np.uint8)
    b = np.clip(bg_rgb[2] + inv * (252 - bg_rgb[2]), 0, 255).astype(np.uint8)

    bgra = np.empty((box_h_px, box_w_px, 4), dtype=np.uint8)
    bgra[..., 0] = b
    bgra[..., 1] = g
    bgra[..., 2] = r
    bgra[..., 3] = 255
    return box_w_px, box_h_px, bgra.tobytes()


_SCENE_BGRA_LUT = np.array(
    [
        [12, 12, 12, 255],      # 0 = conhost bg (#0c0c0c)
        [117, 102, 89, 255],    # 1 = dim gray
        [204, 186, 173, 255],   # 2 = mid gray
        [255, 250, 245, 255],   # 3 = crisp white
        [250, 191, 89, 255],    # 4 = cyan/blue arrow (BGR)
        [158, 107, 71, 255],    # 5 = slate frame (BGR)
        [82, 191, 242, 255],    # 6 = amber '?' badge (BGR)
    ],
    dtype=np.uint8,
)


def encode_scene_dib(idx: np.ndarray) -> tuple[int, int, bytes]:
    """Convert a uint8 palette-index scene array into a 32-bit BGRA top-down DIB."""
    h, w = idx.shape
    clamped = np.clip(idx, 0, len(_SCENE_BGRA_LUT) - 1)
    bgra = _SCENE_BGRA_LUT[clamped]
    return w, h, np.ascontiguousarray(bgra).tobytes()


def draw_dib_on_conhost(
    dib: tuple[int, int, bytes],
    col_1based: int,
    row_1based: int,
    cell_w: int,
    cell_h: int,
) -> bool:
    """Paint a prepared BGRA DIB onto the legacy conhost.exe window at (col, row)."""
    if os.name != "nt":
        return False
    try:
        kernel32, user32, gdi32 = _setup_win32()
        hwnd = kernel32.GetConsoleWindow()
        if not hwnd or not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return False

        w, h, data = dib
        x = max(0, (col_1based - 1) * cell_w)
        y = max(0, (row_1based - 1) * cell_h)

        rect = wintypes.RECT()
        if user32.GetClientRect(hwnd, ctypes.byref(rect)):
            if x >= rect.right or y >= rect.bottom:
                return False

        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        hdc = user32.GetDC(hwnd)
        if not hdc:
            return False
        try:
            gdi32.SetDIBitsToDevice(
                hdc,
                x,
                y,
                w,
                h,
                0,
                0,
                0,
                h,
                data,
                ctypes.byref(bmi),
                0,  # DIB_RGB_COLORS
            )
            return True
        finally:
            user32.ReleaseDC(hwnd, hdc)
    except Exception:
        return False
