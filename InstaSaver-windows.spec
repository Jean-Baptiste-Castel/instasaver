# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows build.

One file, no console window. The macOS build uses InstaSaver.spec instead,
which produces an .app bundle.
"""
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("instaloader", "browser_cookie3"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += [
    "Cryptodome",
    "Cryptodome.Cipher.AES",
    "win32crypt",           # browser_cookie3 decrypts Chrome cookies with this
]

a = Analysis(
    ["InstaSaver.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["numpy", "PIL", "matplotlib", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="InstaSaver",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon="InstaSaver.ico",
)
