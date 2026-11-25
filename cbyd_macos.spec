# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for macOS (builds Aufstieg.app bundle with native WKWebView)."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

_wv_datas, _wv_bins, _wv_hidden = collect_all("webview")

hiddenimports = (
    _wv_hidden
    + [
        "proxy_tools",
        "bottle",
        "objc",
        "Foundation",
        "AppKit",
        "Cocoa",
        "WebKit",
        "Quartz",
        "CoreFoundation",
        "Security",
        "UniformTypeIdentifiers",
        "PyObjCTools",
        "PyObjCTools.AppHelper",
    ]
    + collect_submodules("rich._unicode_data")
    + [
        "cbyd_flashcards.webview_window",
        "PIL._webp",
        "sqlite3",
        "_lzma",
        "httpx",
        "httpcore",
        "certifi",
        "cbyd_flashcards.db",
        "cbyd_flashcards.sixel",
        "cbyd_flashcards.conhost_gfx",
        "cbyd_flashcards.term_ui",
        "cbyd_flashcards.chain_view",
        "cbyd_flashcards.html_generator",
        "cbyd_flashcards.pixel_art",
    ]
)

datas = collect_data_files("certifi") + [("contacts.html", ".")] + _wv_datas
if Path("cbyd.db").is_file():
    datas.append(("cbyd.db", "."))

icon_path = Path("aufstieg.icns")
icon_arg = str(icon_path) if icon_path.is_file() else None

a = Analysis(
    ["cbyd_entry.py"],
    pathex=[],
    binaries=_wv_bins,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "pytest", "pythonnet", "clr_loader"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="cbyd",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="cbyd",
)

app = BUNDLE(
    coll,
    name="Aufstieg.app",
    icon=icon_arg,
    bundle_identifier="com.aufchem.aufstieg",
    info_plist={
        "CFBundleName": "Aufstieg",
        "CFBundleDisplayName": "Aufstieg | Химия",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
    },
)
