"""CLIQ package init.

Load a local ``.env`` (if present) as the VERY FIRST thing on import so that
every submodule — and any ``os.environ.get(...)`` evaluated at import time —
sees per-developer secrets like ``CLIQ_NETSIM_API_KEY`` without a manual
``$env:`` export on every run. Never fails startup; never logs values.
"""

from __future__ import annotations

from cliq.config import load_env_file

# Best-effort: a real environment variable always wins over the file.
load_env_file()
