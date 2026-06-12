"""
Install the Python packages required by COMSOLExtractor.py.

Usage:
    python install_requirements.py

Installs into whichever Python environment runs this script (e.g. activate
a virtual environment first if you want one). Installs everything listed in
requirements.txt - MPh, pandas, numpy, psutil (required), and originpro and
pyinstaller (optional, for --origin and for building a standalone .exe
respectively). If an optional package fails to install, this is reported as
a warning rather than an error, since --comsol-only use doesn't need either.
"""

import subprocess
import sys
from pathlib import Path

REQUIREMENTS_FILE = Path(__file__).resolve().parent / 'requirements.txt'
OPTIONAL_PACKAGES = {'originpro', 'pyinstaller'}


def pip_install(*args: str) -> bool:
    result = subprocess.run([sys.executable, '-m', 'pip', 'install', *args])
    return result.returncode == 0


def main():
    if sys.version_info < (3, 10):
        sys.exit(f"ERROR: Python 3.10+ is required (found {sys.version.split()[0]}).")

    if not REQUIREMENTS_FILE.exists():
        sys.exit(f"ERROR: {REQUIREMENTS_FILE} not found.")

    print("Upgrading pip...")
    pip_install('--upgrade', 'pip')

    # Read requirements.txt, separating required from optional packages so
    # an optional package failing to install doesn't abort the rest.
    required, optional = [], []
    for line in REQUIREMENTS_FILE.read_text().splitlines():
        name = line.strip()
        if not name or name.startswith('#'):
            continue
        (optional if name.lower() in OPTIONAL_PACKAGES else required).append(name)

    print(f"\nInstalling required packages: {', '.join(required)}")
    if not pip_install(*required):
        sys.exit("\nERROR: Failed to install required packages.")

    for name in optional:
        print(f"\nInstalling optional package: {name}")
        if not pip_install(name):
            print(f"[!] Failed to install '{name}' - this is only needed for "
                  f"--origin or for building a standalone .exe, so running "
                  f"ComsolExtractor.py directly is unaffected.")

    print("\nDone.")


if __name__ == '__main__':
    main()
