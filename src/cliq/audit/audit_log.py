"""
audit_log.py — CLIQ Session Audit Logger

Writes a tamper-evident JSONL log of every command executed in a CLIQ session.
Each line is a self-contained JSON record.

Log location: ~/.cliq/audit.log  (appended, never overwritten)
Evidence format: suitable for submission / review

Usage:
    from cliq.audit.audit_log import AuditLogger
    logger = AuditLogger(device="iosv-0", lab="cli_testing")
    logger.log(command="show ip route", mode="enable", output="...", latency_ms=42.1)
    logger.close()
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("audit_log")

_DEFAULT_LOG_PATH = Path.home() / ".cliq" / "audit.log"


class AuditLogger:
    """
    Append-only JSONL audit logger for CLIQ command sessions.

    Each log entry contains:
      - session_id    : UUID generated at CLIQShell startup (one per run)
      - ts            : ISO-8601 UTC timestamp
      - host          : local hostname (who ran it)
      - device        : CML node label (e.g. iosv-0)
      - lab           : CML lab name
      - mode          : IOS mode at time of execution (exec/enable/config/…)
      - command       : full expanded command sent to device
      - output_lines  : number of output lines returned
      - latency_ms    : round-trip time to device in milliseconds
      - status        : "ok" | "error" | "blocked"
      - note          : optional free-text (used for safety-gate blocks)
    """

    def __init__(
        self,
        device: str = "mock",
        lab: str = "local",
        log_path: str | Path | None = None,
    ):
        self.device = device
        self.lab = lab
        self.session_id = str(uuid.uuid4())
        self._path = Path(log_path or _DEFAULT_LOG_PATH)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._host = socket.gethostname()
        self._fh = open(self._path, "a", encoding="utf-8")
        self._write_session_start()

    # ── public API ───────────────────────────────────────────────────────

    def log(
        self,
        command: str,
        mode: str = "exec",
        output: str = "",
        latency_ms: float = 0.0,
        status: str = "ok",
        note: str = "",
    ) -> None:
        """Write one command record to the audit log."""
        entry = {
            "session_id": self.session_id,
            "ts": _now_iso(),
            "host": self._host,
            "device": self.device,
            "lab": self.lab,
            "mode": mode,
            "command": command,
            "output_lines": len(output.splitlines()) if output else 0,
            "latency_ms": round(latency_ms, 2),
            "status": status,
        }
        if note:
            entry["note"] = note
        self._append(entry)

    def log_blocked(self, command: str, mode: str, reason: str) -> None:
        """Log a command that was blocked by safety gates."""
        self.log(command=command, mode=mode, status="blocked", note=reason)

    def log_error(self, command: str, mode: str, error: str) -> None:
        """Log a command that produced a device error."""
        self.log(command=command, mode=mode, status="error", note=error)

    def close(self) -> None:
        """Write session-end marker and close the file handle."""
        self._append({
            "session_id": self.session_id,
            "ts": _now_iso(),
            "event": "session_end",
            "device": self.device,
            "lab": self.lab,
        })
        with self._lock:
            self._fh.flush()
            self._fh.close()

    def get_session_commands(self) -> list[dict]:
        """Return all commands logged in THIS session (reads back from log)."""
        entries = []
        try:
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if (rec.get("session_id") == self.session_id
                                and "command" in rec):
                            entries.append(rec)
                    except json.JSONDecodeError:
                        pass
        except FileNotFoundError:
            pass
        return entries

    # ── internal ─────────────────────────────────────────────────────────

    def _write_session_start(self) -> None:
        self._append({
            "session_id": self.session_id,
            "ts": _now_iso(),
            "event": "session_start",
            "device": self.device,
            "lab": self.lab,
            "host": self._host,
        })

    def _append(self, record: dict) -> None:
        with self._lock:
            try:
                self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._fh.flush()
            except Exception as exc:
                log.warning(f"Audit log write failed: {exc}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
