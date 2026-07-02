# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for a single-file Windows executable. Build with:
#   .venv\Scripts\pyinstaller COMSOLExtractor.spec
#
# COMSOL Multiphysics and/or OriginLab must still be installed and licensed
# on the machine running the resulting .exe - this only bundles the Python
# side (MPh/JPype, pandas, originpro, PySide6, ...).

from pathlib import Path
import mph

mph_dir = Path(mph.__file__).parent

a = Analysis(
    ['COMSOLExtractor.py'],
    pathex=[],
    binaries=[],
    # mph reads its COMSOL feature/tag lookup table from this JSON file at
    # runtime (mph/node.py), so it must be bundled alongside the mph package.
    datas=[(str(mph_dir / 'tags.json'), 'mph')],
    hiddenimports=['originpro', 'OriginExt'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='COMSOLExtractor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    version='version_info.txt',
)
