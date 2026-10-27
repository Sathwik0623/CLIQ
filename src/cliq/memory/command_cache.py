#!/usr/bin/env python3
"""
command_cache.py — Persistent, device-scoped command + ?-help cache (Part 1).

Core principle hierarchy enforced by CLIQ:

    live device (current UUID + mode)  >  this persistent learned cache  >  static KB

This store is populated ONLY from device-confirmed truth: a command the live
device ACCEPTED (no ``^`` / ``% Invalid``) or an option it returned via
``?``-help. The static KB is NEVER written here — it is enrichment only.

Two-tier keying
---------------
* **Device-type tier** (``router`` / ``l2-switch`` / ``l3-switch`` / ``nx-os``)
  — Platform-Independent (PI) commands are cached here so suggestions are shared
  cross-user and cross-device for the same kind of box.
* **Platform/UUID tier** — Platform-Dependent (PD) commands (e.g. ``qfp`` on a
  C8000v vs ``fed`` on a Cat9k) are stored at platform grain so a PD command
  learned on one platform is NEVER suggested on another.

Survives hard refresh and restart (DB-backed under ``.cliq/memory``), so a
captured ``show ip ospf ?`` is remembered for the session and beyond — no
re-learning, and another user on a same-type device benefits immediately.

Fully offline; the backing SQLite file never leaves the machine.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

# Default TTL for a verified entry. After this, the entry is considered stale
# and must be re-confirmed by the device before it is served again.
DEFAULT_TTL_S = 30 * 24 * 3600  # 30 days

DEFAULT_CACHE_DIR = ".cliq/memory"

# Platform-Dependent subsystem tokens. A command containing one of these is
# hardware/platform specific and MUST be stored at platform grain so it is never
# suggested on a different platform (e.g. never suggest 'qfp' on a Cat9k, nor
# 'fed' on a C8000v).
_PD_TOKENS: frozenset[str] = frozenset({
    "qfp",            # C8000v / ASR1k forwarding processor
    "fed",            # Cat9k forwarding engine driver
    "npu",            # network processing unit (various)
    "asic",           # ASIC-level platform internals
    "iomd",           # IOMd platform process
    "fpga",
    "punt",           # punt/inject path is platform-specific
})


def classify_device_type(node_type: str) -> str:
    """Map a CML/IOS node type to a coarse device-type tier for PI sharing.

    Returns one of ``router`` / ``l2-switch`` / ``l3-switch`` / ``nx-os``.
    Unknown types fall back to ``l3-switch`` (route+switch), the most permissive
    bucket, so the device — not this table — remains the source of truth.
    """
    p = (node_type or "").lower().strip()
    if not p:
        return "l3-switch"
    if "nexus" in p or "nx-os" in p or "nxos" in p or "n9k" in p:
        return "nx-os"
    if "iosvl2" in p or "cat2" in p or "cat3" in p:
        return "l2-switch"
    if "cat9" in p or "cat8" in p or "c8000" in p or "csr" in p or "isr" in p:
        return "l3-switch"
    if "iosxr" in p or "asr9" in p or p == "iosv" or p.startswith("iosv-") or p.startswith("iosv_"):
        return "router"
    return "l3-switch"


def is_platform_dependent(command_or_option: str) -> bool:
    """True when the command/option references a platform-specific subsystem.

    PD entries are stored at platform grain so they never bleed across
    platforms. Token-based so it is robust to abbreviation and ordering.
    """
    tokens = (command_or_option or "").lower().split()
    return any(t in _PD_TOKENS for t in tokens)


class PersistentCommandCache:
    """SQLite-backed, thread-safe two-tier cache of device-verified commands.

    All writes are device-confirmed (``source='device'``). Reads are
    TTL-filtered. PD entries are scoped by ``platform``; PI entries use an empty
    platform string and are shared across every device of the same device-type.
    """

    SCHEMA_VERSION = 1

    def __init__(self, cache_dir: str | None = None, ttl_s: float = DEFAULT_TTL_S) -> None:
        self.cache_dir = Path(cache_dir or DEFAULT_CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.cache_dir / "command_cache.db"
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS verified_commands (
                    command_path  TEXT NOT NULL,
                    device_type   TEXT NOT NULL,
                    platform      TEXT NOT NULL DEFAULT '',
                    mode          TEXT NOT NULL DEFAULT 'exec',
                    source        TEXT NOT NULL DEFAULT 'device',
                    is_pd         INTEGER NOT NULL DEFAULT 0,
                    last_verified REAL NOT NULL,
                    PRIMARY KEY (command_path, device_type, platform, mode)
                );
                CREATE TABLE IF NOT EXISTS help_options (
                    prefix        TEXT NOT NULL,
                    option        TEXT NOT NULL,
                    device_type   TEXT NOT NULL,
                    platform      TEXT NOT NULL DEFAULT '',
                    mode          TEXT NOT NULL DEFAULT 'exec',
                    source        TEXT NOT NULL DEFAULT 'device',
                    is_pd         INTEGER NOT NULL DEFAULT 0,
                    last_verified REAL NOT NULL,
                    PRIMARY KEY (prefix, option, device_type, platform, mode)
                );
                CREATE INDEX IF NOT EXISTS idx_cmd_lookup
                    ON verified_commands(device_type, platform, mode);
                CREATE INDEX IF NOT EXISTS idx_help_lookup
                    ON help_options(prefix, device_type, platform, mode);
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Writes (device-confirmed only)
    # ------------------------------------------------------------------
    @staticmethod
    def _norm(text: str) -> str:
        return " ".join((text or "").split()).lower()

    def record_command(
        self, command: str, node_type: str, mode: str = "exec",
    ) -> None:
        """Persist a device-ACCEPTED command. PD commands are stored at platform
        grain (``platform=node_type``); PI commands at device-type grain."""
        cmd = self._norm(command)
        if not cmd:
            return
        device_type = classify_device_type(node_type)
        pd = is_platform_dependent(cmd)
        platform = (node_type or "").lower().strip() if pd else ""
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO verified_commands
                  (command_path, device_type, platform, mode, source, is_pd, last_verified)
                VALUES (?, ?, ?, ?, 'device', ?, ?)
                ON CONFLICT(command_path, device_type, platform, mode)
                DO UPDATE SET last_verified = excluded.last_verified
                """,
                (cmd, device_type, platform, mode, 1 if pd else 0, now),
            )
            self._conn.commit()

    def record_help_options(
        self, prefix: str, options: list[str], node_type: str, mode: str = "exec",
    ) -> None:
        """Persist device ``?``-help options for ``prefix``. Each option is PD-
        scoped independently so a PD option never bleeds to another platform."""
        head = self._norm(prefix)
        device_type = classify_device_type(node_type)
        now = time.time()
        rows = []
        for opt in options:
            o = (opt or "").strip()
            if not o:
                continue
            pd = is_platform_dependent(o) or is_platform_dependent(head)
            platform = (node_type or "").lower().strip() if pd else ""
            rows.append((head, o, device_type, platform, mode, 1 if pd else 0, now))
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                """
                INSERT INTO help_options
                  (prefix, option, device_type, platform, mode, source, is_pd, last_verified)
                VALUES (?, ?, ?, ?, ?, 'device', ?, ?)
                ON CONFLICT(prefix, option, device_type, platform, mode)
                DO UPDATE SET last_verified = excluded.last_verified
                """,
                rows,
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Reads (TTL-filtered, PD-correct)
    # ------------------------------------------------------------------
    def get_help_options(
        self, prefix: str, node_type: str, mode: str = "exec",
    ) -> list[str]:
        """Return cached device-verified options for ``prefix`` valid for THIS
        device. Includes device-type-tier (PI) rows plus this platform's PD rows;
        never another platform's PD rows. TTL-stale rows are excluded."""
        head = self._norm(prefix)
        device_type = classify_device_type(node_type)
        platform = (node_type or "").lower().strip()
        cutoff = time.time() - self.ttl_s
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT option FROM help_options
                WHERE prefix = ? AND device_type = ? AND mode = ?
                  AND last_verified >= ?
                  AND (is_pd = 0 OR platform = ?)
                ORDER BY option
                """,
                (head, device_type, mode, cutoff, platform),
            ).fetchall()
        seen: set[str] = set()
        out: list[str] = []
        for r in rows:
            low = r["option"].lower()
            if low not in seen:
                seen.add(low)
                out.append(r["option"])
        return out

    def known_commands(
        self, node_type: str, mode: str | None = None,
    ) -> list[str]:
        """Return device-verified command paths valid for THIS device (PI rows +
        this platform's PD rows), TTL-filtered. Used to seed the ghost index."""
        device_type = classify_device_type(node_type)
        platform = (node_type or "").lower().strip()
        cutoff = time.time() - self.ttl_s
        query = (
            "SELECT DISTINCT command_path FROM verified_commands "
            "WHERE device_type = ? AND last_verified >= ? "
            "AND (is_pd = 0 OR platform = ?)"
        )
        params: list = [device_type, cutoff, platform]
        if mode:
            query += " AND mode = ?"
            params.append(mode)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [r["command_path"] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
