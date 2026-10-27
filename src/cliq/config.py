"""Environment / configuration bootstrap.

Loads a local ``.env`` file (if present) into ``os.environ`` so per-developer
secrets — most importantly ``CLIQ_NETSIM_API_KEY`` (rotates every 30 days) — do
not have to be exported by hand on every run. The file is git-ignored and
personal to each machine; nothing here is ever committed or logged.

Deliberately dependency-free (no ``python-dotenv``) to preserve the project's
offline, deterministic posture. Precedence: a variable ALREADY set in the real
environment WINS over the ``.env`` file, so CI / explicit ``$env:`` exports are
never silently overridden.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: Cache so repeated imports don't re-read the file.
_LOADED = False


def _find_dotenv(start: Optional[Path] = None) -> Optional[Path]:
    """Walk upward from ``start`` (or CWD) looking for a ``.env`` file."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def _parse_line(line: str) -> Optional[tuple[str, str]]:
    """Parse a single ``KEY=VALUE`` line, or return None to skip it."""
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    # Support an optional leading ``export `` (copy-pasted from a shell).
    if text.lower().startswith("export "):
        text = text[len("export "):].lstrip()
    if "=" not in text:
        return None
    key, _, value = text.partition("=")
    key = key.strip()
    if not key:
        return None
    value = value.strip()
    # Strip a single matching pair of surrounding quotes (but keep inner chars).
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return key, value


def load_env_file(path: Optional[Path] = None, *, override: bool = False) -> bool:
    """Load ``.env`` into ``os.environ`` once. Returns True if a file was read.

    A real, already-set environment variable takes precedence unless
    ``override`` is True. Malformed lines are skipped silently — a bad ``.env``
    must never crash startup. Values are never logged (they may be secrets).
    """
    global _LOADED
    if _LOADED and path is None:
        return False

    dotenv = path or _find_dotenv()
    if dotenv is None or not dotenv.is_file():
        if path is None:
            _LOADED = True
        return False

    try:
        raw = dotenv.read_text(encoding="utf-8")
    except OSError:
        if path is None:
            _LOADED = True
        return False

    for line in raw.splitlines():
        parsed = _parse_line(line)
        if parsed is None:
            continue
        key, value = parsed
        if override or key not in os.environ:
            os.environ[key] = value

    if path is None:
        _LOADED = True
    return True
