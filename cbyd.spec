# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_all

_wv_datas, _wv_bins, _wv_hidden = collect_all("webview")
_clr_datas, _clr_bins, _clr_hidden = collect_all("clr_loader")
_pn_datas, _pn_bins, _pn_hidden = collect_all("pythonnet")

hiddenimports = (
    _wv_hidden + _clr_hidden + _pn_hidden + ["clr", "proxy_tools", "bottle"]
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

datas = collect_data_files("certifi") + [("cbyd.db", "."), ("contacts.html", ".")] + _wv_datas + _clr_datas + _pn_datas

a = Analysis(
    ["cbyd_entry.py"],
    pathex=[],
    binaries=_wv_bins + _clr_bins + _pn_bins,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "pytest"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="cbyd",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
