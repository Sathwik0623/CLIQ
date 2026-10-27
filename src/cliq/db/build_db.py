#!/usr/bin/env python3
"""
build_db.py — Compile the curated YAML Command Database into SQLite.

Module entry point so `python -m cliq.db.build_db` runs the build script
located at the repository root.
"""

import os
import runpy
import sys


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))


def main():
    root = _repo_root()
    script = os.path.join(root, "build_db.py")
    if not os.path.exists(script):
        sys.stderr.write(f"build_db.py not found at {script}\n")
        sys.exit(1)
    # Run from repo root so relative paths in the script resolve correctly.
    cwd = os.getcwd()
    os.chdir(root)
    try:
        runpy.run_path(script, run_name="__main__")
    finally:
        os.chdir(cwd)


if __name__ == "__main__":
    main()
