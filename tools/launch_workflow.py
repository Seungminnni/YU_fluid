#!/usr/bin/env python3
"""Prepare a local Python >=3.12 environment, then open the CFD workflow."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    if sys.version_info < (3, 12):
        raise SystemExit("Python 3.12 or higher is required (3.13/3.14 are allowed).")
    root = Path(__file__).resolve().parents[1]
    executable = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    version_check = "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)"
    if executable.exists():
        if subprocess.run([str(executable), "-c", version_check], check=False).returncode:
            raise SystemExit("Existing .venv requires Python >=3.12; rename .venv and run again.")
    else:
        subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")], check=True)
    subprocess.run([str(executable), "-m", "pip", "install", "-e", str(root) + "[cad,results,media]"], check=True)
    return subprocess.call([str(executable), "-m", "cfd_twin.bootstrap", "--root", str(root), *sys.argv[1:]], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
