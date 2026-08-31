# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for InstaSaver.

Info.plist keys are set here rather than patched afterwards. Editing the plist
of a finished bundle invalidates its signature, and macOS refuses to open an app
whose signature is broken, with no Open Anyway button to rescue it.
"""
import os
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("instaloader", "browser_cookie3"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += [
    "keyring.backends.macOS",
    "Cryptodome",
    "Cryptodome.Cipher.AES",
]

icon = "InstaSaver.icns" if os.path.exists("InstaSaver.icns") else None

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
    [],
    exclude_binaries=True,
    name="InstaSaver",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=os.environ.get("DEVELOPER_ID") or None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="InstaSaver",
)

app = BUNDLE(
    coll,
    name="InstaSaver.app",
    icon=icon,
    bundle_identifier="studio.gast.instasaver",
    version="1.1",
    info_plist={
        "CFBundleName": "InstaSaver",
        "CFBundleDisplayName": "InstaSaver",
        "CFBundleShortVersionString": "1.1",
        "CFBundleVersion": "1.1",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,
        "LSMinimumSystemVersion": "11.0",
        "LSApplicationCategoryType": "public.app-category.utilities",
        "NSHumanReadableCopyright": "Archives the posts you saved on Instagram.",
    },
)
