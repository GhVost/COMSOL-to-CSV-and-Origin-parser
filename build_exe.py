"""
Build a standalone COMSOLExtractor.exe with PyInstaller.

Usage:
    .venv\\Scripts\\python build_exe.py

Requires the packages in requirements.txt plus 'pyinstaller' to be
installed in the active environment (e.g. `pip install pyinstaller`).
Output is written to dist/COMSOLExtractor.exe.

COMSOL Multiphysics and/or OriginLab must still be installed and licensed
on the machine running the resulting .exe - this only bundles the Python
side (MPh/JPype, pandas, originpro, PySide6, ...).
"""

import subprocess
import sys
from pathlib import Path

SPEC_FILE = Path(__file__).resolve().parent / 'COMSOLExtractor.spec'


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit("ERROR: PyInstaller not installed.\n  pip install pyinstaller")

    result = subprocess.run([
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm',
        str(SPEC_FILE),
    ])
    if result.returncode != 0:
        sys.exit(result.returncode)

    print("\nBuilt dist/COMSOLExtractor.exe")


if __name__ == '__main__':
    main()
