#!/usr/bin/env python3
"""
store.py — Persistent CLIQ operational memory (session_log.sqlite).

Survives CLIQ restarts so a reconnecting user resumes with the same command
history, suggestion context, and typo statistics. State is keyed by
lab_id/node_id so each device on each lab has its own independent memory.

Schema
------
sessions(key PK, lab_id, node_id, hostname, mode, updated_at)
    One row per device. `mode` is the last CLI mode the device was in, used to
    restore the operational context after a reconnect.
history(id PK, key, command, ts)
    Append-only command history (most-recent retrievable in order).
stats(key PK, commands_run, typos_caught, updated_at)
    Running counters for the suggestion/typo engine.

USAGE
-----
    log = SessionLog()                     # default: ./session_log.sqlite
    key = SessionLog.make_key(lab_id, node_id)
    log.save_state(key, lab_id, node_id, hostname="R1", mode="config-if")
    log.append_command(key, "show ip int br")
    log.bump_typo(key)
    state = log.load_state(key)            # -> dict or None
    history = log.get_history(key)         # -> ["show ip int br", ...]
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("cliq.memory.store")

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
DEFAULT_LOG_DB = os.path.join(_PROJECT_ROOT, "session_log.sqlite")


@dataclass
class SessionState:
    key: str
    lab_id: str
    node_id: str
    hostname: str
    mode: str
    commands_run: int = 0
    typos_caught: int = 0


class SessionLog:
    """Thread-safe SQLite-backed store for CLIQ session memory."""

    def __init__(self, db_path: str = DEFAULT_LOG_DB):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ------------------------------------------------------------------
    @staticmethod
    def make_key(lab_id: str, node_id: str) -> str:
        """Composite key identifying a device across restarts."""
        return f"{lab_id}/{node_id}"

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    key        TEXT PRIMARY KEY,
                    lab_id     TEXT NOT NULL,
                    node_id    TEXT NOT NULL,
                    hostname   TEXT DEFAULT '',
                    mode       TEXT DEFAULT 'exec',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                    id      INTEGER PRIMARY KEY AUTOINCREMENT,
                    key     TEXT NOT NULL,
                    command TEXT NOT NULL,
                    ts      REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_history_key ON history(key);
                CREATE TABLE IF NOT EXISTS stats (
                    key          TEXT PRIMARY KEY,
                    commands_run INTEGER DEFAULT 0,
                    typos_caught INTEGER DEFAULT 0,
                    updated_at   REAL NOT NULL
                );
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def save_state(self, key: str, lab_id: str, node_id: str,
                   hostname: str = "", mode: str = "exec") -> None:
        """Upsert the device's last-known hostname + mode."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sessions (key, lab_id, node_id, hostname, mode, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    hostname=excluded.hostname,
                    mode=excluded.mode,
                    updated_at=excluded.updated_at
                """,
                (key, lab_id, node_id, hostname, mode, now),
            )
            self._conn.execute(
                """
                INSERT INTO stats (key, commands_run, typos_caught, updated_at)
                VALUES (?, 0, 0, ?)
                ON CONFLICT(key) DO NOTHING
                """,
                (key, now),
            )
            self._conn.commit()

    def append_command(self, key: str, command: str) -> None:
        """Record an executed command and bump the run counter."""
        command = (command or "").strip()
        if not command:
            return
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO history (key, command, ts) VALUES (?, ?, ?)",
                (key, command, now),
            )
            self._conn.execute(
                """
                INSERT INTO stats (key, commands_run, typos_caught, updated_at)
                VALUES (?, 1, 0, ?)
                ON CONFLICT(key) DO UPDATE SET
                    commands_run = commands_run + 1,
                    updated_at = excluded.updated_at
                """,
                (key, now),
            )
            self._conn.commit()

    def bump_typo(self, key: str) -> None:
        """Increment the typos-caught counter for this device."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO stats (key, commands_run, typos_caught, updated_at)
                VALUES (?, 0, 1, ?)
                ON CONFLICT(key) DO UPDATE SET
                    typos_caught = typos_caught + 1,
                    updated_at = excluded.updated_at
                """,
                (key, now),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def load_state(self, key: str) -> Optional[SessionState]:
        """Return the persisted state for a device, or None if never seen."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE key = ?", (key,)
            ).fetchone()
            if not row:
                return None
            stat = self._conn.execute(
                "SELECT commands_run, typos_caught FROM stats WHERE key = ?", (key,)
            ).fetchone()
        return SessionState(
            key=row["key"],
            lab_id=row["lab_id"],
            node_id=row["node_id"],
            hostname=row["hostname"],
            mode=row["mode"],
            commands_run=stat["commands_run"] if stat else 0,
            typos_caught=stat["typos_caught"] if stat else 0,
        )

    def get_history(self, key: str, limit: int = 200) -> list[str]:
        """Return commands for this device in chronological order (oldest first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT command FROM history WHERE key = ? ORDER BY id DESC LIMIT ?",
                (key, limit),
            ).fetchall()
        return [r["command"] for r in reversed(rows)]

    def clear(self, key: str) -> None:
        """Erase all stored memory for a device (history + state + stats)."""
        with self._lock:
            self._conn.execute("DELETE FROM history WHERE key = ?", (key,))
            self._conn.execute("DELETE FROM sessions WHERE key = ?", (key,))
            self._conn.execute("DELETE FROM stats WHERE key = ?", (key,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
