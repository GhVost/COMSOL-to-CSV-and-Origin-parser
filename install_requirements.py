"""
Install the Python packages required by COMSOLExtractor.py.

Usage:
    python install_requirements.py

Installs into whichever Python environment runs this script (e.g. activate
a virtual environment first if you want one). Installs requirements.txt
(required), then requirements-origin.txt and requirements-dev.txt as optional
sets. If an optional set fails to install, this is reported as a warning
rather than an error, since --comsol-only use does not need either.
"""

import subprocess
import sys
from pathlib import Path

REQUIREMENTS_FILE = Path(__file__).resolve().parent / 'requirements.txt'
OPTIONAL_REQUIREMENTS = [
    ('OriginLab integration', Path(__file__).resolve().parent / 'requirements-origin.txt'),
    ('development/build tools', Path(__file__).resolve().parent / 'requirements-dev.txt'),
]


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

    print("\nInstalling required packages...")
    if not pip_install('-r', str(REQUIREMENTS_FILE)):
        sys.exit("\nERROR: Failed to install required packages.")

    for label, path in OPTIONAL_REQUIREMENTS:
        if not path.exists():
            continue
        print(f"\nInstalling optional {label}...")
        if not pip_install('-r', str(path)):
            print(f"[!] Failed to install optional {label}; COMSOL-only use "
                  f"is unaffected.")

    print("\nDone.")


if __name__ == '__main__':
    main()
