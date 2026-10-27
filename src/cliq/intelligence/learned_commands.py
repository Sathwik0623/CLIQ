#!/usr/bin/env python3
"""
learned_commands.py — Local, offline, accepted-only command learning cache.

CLIQ learns the commands an engineer actually uses on real devices so that ghost
text can suggest them later. The cardinal rule is **accepted-only learning**:

    A command is recorded (or reinforced) ONLY when the device accepted it —
    i.e. its response carried no IOS error marker such as ``% Invalid input``,
    ``% Incomplete command``, ``% Ambiguous command`` or ``% Unknown command``.

Rejected commands and typos are never learned. The store is a small local SQLite
file kept under ``.cliq/`` (git-ignored, never synced externally).

Schema (table ``learned``):
    command      TEXT PRIMARY KEY   -- normalized command string
    accept_count INTEGER            -- how many times the device accepted it
    mode         TEXT               -- last CLI mode it was accepted in
    platform     TEXT               -- last device platform it was accepted on
    first_seen   TEXT               -- ISO-8601 UTC of first acceptance
    last_seen    TEXT               -- ISO-8601 UTC of most recent acceptance
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("cliq.intelligence.learned")

# Default location: a local, git-ignored directory in the project/workspace.
DEFAULT_LEARNED_DB = ".cliq/learned_commands.sqlite"

# IOS rejection markers. If a device response contains ANY of these, the command
# was NOT accepted and must never be learned.
_ERROR_MARKERS = (
    "% invalid input",
    "% incomplete command",
    "% ambiguous command",
    "% unknown command",
    "% unrecognized command",
    "% bad mask",
    "% wrong number of args",
)
_GENERIC_ERROR_RE = re.compile(r"^\s*%\s*\S", re.MULTILINE)

# Never persist commands that could embed a credential.
_SECRET_RE = re.compile(r"\b(password|secret|key|md5|encryption)\b", re.IGNORECASE)


def is_accepted(output: str | None) -> bool:
    """Return True when a device response shows no rejection markers.

    Used by the caller BEFORE learning a command. An empty / whitespace-only
    response is treated as accepted (many valid config commands echo nothing).
    """
    if not output:
        return True
    low = output.lower()
    for marker in _ERROR_MARKERS:
        if marker in low:
            return False
    # Catch any other line that begins with a '% ...' IOS error.
    if _GENERIC_ERROR_RE.search(output):
        return False
    return True


def _normalize(command: str) -> str:
    """Collapse whitespace and lowercase a command for stable keying."""
    return re.sub(r"\s+", " ", (command or "").strip()).lower()


class LearnedCommandStore:
    """SQLite-backed, thread-safe store of accepted commands.

    Local and offline only — the backing file is created under ``.cliq/`` and is
    never transmitted anywhere.
    """

    def __init__(self, db_path: str = DEFAULT_LEARNED_DB) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS learned (
                    command      TEXT PRIMARY KEY,
                    accept_count INTEGER NOT NULL DEFAULT 1,
                    mode         TEXT,
                    platform     TEXT,
                    first_seen   TEXT,
                    last_seen    TEXT
                )
                """
            )
            self._conn.commit()

    def record_accepted(
        self,
        command: str,
        mode: str | None = None,
        platform: str | None = None,
    ) -> tuple[str, int] | None:
        """Record/reinforce an accepted command. Returns ``(command, count)``.

        Skips empty, trivial, or credential-bearing commands. The caller is
        responsible for verifying acceptance first (see :func:`is_accepted`).
        """
        norm = _normalize(command)
        if len(norm) < 3 or " " not in norm and len(norm) < 4:
            # Too short to be a useful, unambiguous learned completion.
            if len(norm) < 3:
                return None
        if _SECRET_RE.search(norm):
            log.debug("Refusing to learn credential-bearing command.")
            return None
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock:
            cur = self._conn.execute(
                "SELECT accept_count FROM learned WHERE command = ?", (norm,)
            )
            row = cur.fetchone()
            if row:
                count = int(row["accept_count"]) + 1
                self._conn.execute(
                    "UPDATE learned SET accept_count = ?, mode = ?, platform = ?, "
                    "last_seen = ? WHERE command = ?",
                    (count, mode, platform, now, norm),
                )
            else:
                count = 1
                self._conn.execute(
                    "INSERT INTO learned (command, accept_count, mode, platform, "
                    "first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                    (norm, count, mode, platform, now, now),
                )
            self._conn.commit()
        return norm, count

    def prefix_matches(self, prefix: str, limit: int = 10) -> list[tuple[str, int]]:
        """Return ``(command, accept_count)`` rows whose command starts with prefix."""
        norm = _normalize(prefix)
        if not norm:
            return []
        like = norm.replace("%", r"\%").replace("_", r"\_") + "%"
        with self._lock:
            cur = self._conn.execute(
                "SELECT command, accept_count FROM learned "
                "WHERE command LIKE ? ESCAPE '\\' ORDER BY accept_count DESC, command ASC "
                "LIMIT ?",
                (like, limit),
            )
            return [(r["command"], int(r["accept_count"])) for r in cur.fetchall()]

    def all_commands(self) -> list[tuple[str, int]]:
        """Return every learned ``(command, accept_count)`` for index building."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT command, accept_count FROM learned ORDER BY accept_count DESC"
            )
            return [(r["command"], int(r["accept_count"])) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
