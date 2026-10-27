#!/usr/bin/env python3
"""
observability.py — CLIQ unified structured-logging / latency-observability layer.

Phase A foundation. This generalizes the JSON-line schema that the NetSim API
layer (``netsim_api.log``) already used into ONE shared helper that EVERY layer
emits through (NetSim, CML, terminal, command exec, usage analytics).

Each record is a single JSON line with the canonical schema::

    {
      "ts":          UTC ISO-8601 timestamp,
      "layer":       "netsim" | "cml" | "terminal" | "command" | "usage" | ...,
      "op":          high-level operation (e.g. "connect", "run_command",
                     "get_projects", "ghost"),
      "stage":       sub-stage (e.g. "auth", "spin_up", "node_boot",
                     "first_byte", "complete"),
      "outcome":     "success" | "error" | "timeout" | "start" | ...,
      "status_code": HTTP/exit status or null,
      "elapsed_ms":  milliseconds for this stage or null,
      "detail":      short, REDACTED human detail (NEVER secrets/keys/passwords)
    }

Design guarantees (Phase A constraints):
  * Additive + non-fatal: logging NEVER raises into the caller. A logging
    failure is swallowed so it can never affect the terminal / connection path.
  * Offline-first: pure stdlib, no network, writes only under ``~/.cliq/``.
  * Secret-safe: every ``detail`` string and every extra value is passed through
    :func:`redact` which scrubs key/token/password patterns. This preserves the
    Phase 0.5 guarantee across ALL layers.
  * Size-capped: each per-layer log self-rotates at ``_MAX_BYTES`` keeping a
    small number of ``.1`` / ``.2`` backups, so logs never bloat the home dir.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

# ──────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────

#: Local CLIQ state dir. All observability logs live here (offline, per-user).
CLIQ_HOME = Path(os.path.expanduser("~")) / ".cliq"

#: Per-layer file names. The NetSim layer keeps its original file name so the
#: existing ``netsim_api.log`` is preserved; every other layer gets its own.
_LAYER_FILES: dict[str, str] = {
    "netsim": "netsim_api.log",
    "cml": "cml.log",
    "terminal": "terminal.log",
    "command": "command.log",
    "usage": "usage.log",
    "event": "cml_events.log",
    # Bug 7: interactive console authentication. Credential values NEVER reach
    # this log — only redacted outcomes (stage/success/failure) are recorded.
    "auth": "auth.log",
    # Destructive-action confirmation gate: records when the blocking y/n gate
    # is triggered, the operator's response, and whether the held command was
    # forwarded to the device or discarded.
    "safety": "safety.log",
    # Bugs 4/8/9 cross-cutting observability: prompt render-order decisions,
    # VLAN-capability caching, and silent idle reconnects.
    "renderer": "renderer.log",
    "connection": "connection.log",
}

#: Rotate a log once it exceeds this size (bytes). Keep ``_MAX_BACKUPS`` copies.
_MAX_BYTES = int(os.environ.get("CLIQ_LOG_MAX_BYTES", str(2 * 1024 * 1024)))  # 2 MiB
_MAX_BACKUPS = int(os.environ.get("CLIQ_LOG_MAX_BACKUPS", "3"))

#: Serializes file writes/rotation so concurrent device threads don't interleave
#: partial JSON lines into a log file.
_FILE_LOCK = threading.Lock()


def _layer_file(layer: str) -> Path:
    """Resolve the on-disk log path for ``layer`` (fallback: ``<layer>.log``)."""
    name = _LAYER_FILES.get(layer, f"{layer}.log")
    return CLIQ_HOME / name


# ──────────────────────────────────────────────────────────────────────────
# Redaction — NEVER let a secret reach a log file (extends Phase 0.5 guarantee)
# ──────────────────────────────────────────────────────────────────────────

#: Patterns that look like secrets in free-form detail text. Conservative —
#: when in doubt we redact. Order matters (key=value before bare tokens).
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # key/token/password/secret/apikey/authorization = <value>
    re.compile(
        r"(?i)\b(api[_-]?key|apikey|token|password|passwd|pwd|secret|"
        r"authorization|auth|x-api-key|bearer)\b\s*[:=]\s*\S+"
    ),
    # "Authorization: Token <value>" / "Bearer <value>" header forms
    re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._\-]{8,}"),
    # Long opaque hex/base64-ish blobs that are almost certainly a key/token.
    # 16+ hex chars; dashed UUIDs (lab/node ids) are NOT matched (dashes break
    # the run) so legitimate identifiers still log.
    re.compile(r"\b[A-Fa-f0-9]{16,}\b"),
)

_REDACTED = "<redacted>"


def redact(value: Any) -> Any:
    """Scrub secret-looking content from ``value`` before it is logged.

    Strings are pattern-scrubbed; dict/list are scrubbed recursively. Anything
    that is not a container or string is returned unchanged. This is a guard,
    NOT a substitute for never passing secrets in the first place.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value
        for pat in _SECRET_PATTERNS:
            text = pat.sub(lambda m: _redact_match(m), text)
        return text
    if isinstance(value, dict):
        return {k: (_REDACTED if _is_secret_key(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def _redact_match(match: re.Match[str]) -> str:
    """Keep the field name (if any) but replace its value with ``<redacted>``."""
    groups = match.groups()
    if groups and groups[0]:
        return f"{groups[0]}=<redacted>"
    return _REDACTED


def _is_secret_key(key: str) -> bool:
    # Match key NAMES that hold a secret value. Deliberately does NOT match
    # "auth" as a bare substring, so descriptive fields like ``auth_style``
    # (the header style, not the key) are preserved.
    return bool(
        re.search(
            r"(?i)(api[_-]?key|apikey|token|password|passwd|pwd|secret|"
            r"authorization|bearer)",
            str(key),
        )
    )


# ──────────────────────────────────────────────────────────────────────────
# Core emit
# ──────────────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(
    layer: str,
    op: str,
    stage: str,
    outcome: str,
    *,
    status_code: Optional[int] = None,
    elapsed_ms: Optional[float] = None,
    detail: Any = "",
    **extra: Any,
) -> dict[str, Any]:
    """Append one unified JSON-line record to the ``layer`` log.

    Returns the record dict (handy for tests/diagnostics). Never raises — a
    logging failure must never break a connection or a command.
    """
    record: dict[str, Any] = {
        "ts": _now_iso(),
        "layer": layer,
        "op": op,
        "stage": stage,
        "outcome": outcome,
        "status_code": status_code,
        "elapsed_ms": round(elapsed_ms, 1) if elapsed_ms is not None else None,
        "detail": redact(detail) if detail else "",
    }
    if extra:
        # Extra fields are redacted too so no secret can slip in via a kwarg.
        for k, v in extra.items():
            if k in record:
                continue
            record[k] = _REDACTED if _is_secret_key(k) else redact(v)

    try:
        _write_record(layer, record)
    except Exception:  # noqa: BLE001 - logging must never break the caller
        pass
    return record


def _write_record(layer: str, record: dict[str, Any]) -> None:
    path = _layer_file(layer)
    try:
        line = json.dumps(record, default=str) + "\n"
    except Exception:  # noqa: BLE001 - serialization must never break caller
        return
    try:
        with _FILE_LOCK:
            CLIQ_HOME.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed(path, len(line.encode("utf-8")))
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:  # noqa: BLE001 - logging is best-effort, never fatal
        pass


def _rotate_if_needed(path: Path, incoming_bytes: int) -> None:
    """Size-cap rotation: ``log`` → ``log.1`` → ``log.2`` … keeping backups."""
    try:
        if not path.exists():
            return
        if path.stat().st_size + incoming_bytes <= _MAX_BYTES:
            return
        # Drop the oldest, then shift each backup up by one.
        oldest = path.with_suffix(path.suffix + f".{_MAX_BACKUPS}")
        if oldest.exists():
            oldest.unlink()
        for i in range(_MAX_BACKUPS - 1, 0, -1):
            src = path.with_suffix(path.suffix + f".{i}")
            if src.exists():
                src.rename(path.with_suffix(path.suffix + f".{i + 1}"))
        path.rename(path.with_suffix(path.suffix + ".1"))
    except Exception:  # noqa: BLE001 - rotation is best-effort
        pass


# ──────────────────────────────────────────────────────────────────────────
# StageTimer — per-stage cold-start / latency breakdown
# ──────────────────────────────────────────────────────────────────────────

class StageTimer:
    """Accumulating per-stage timer that emits one unified record per stage.

    Each :meth:`mark` logs the wall-time elapsed *since the previous mark* (or
    since construction for the first mark), so a sequence of marks produces a
    clean breakdown — exactly what the CML cold-start diagnosis needs::

        st = StageTimer("cml", "connect")
        ... do reservation check ...
        st.mark("reservation_check")
        ... do auth ...
        st.mark("auth")
        st.mark("complete", detail="version=2.9.0")

    ``total_ms`` returns the elapsed time since construction at any point.
    """

    def __init__(self, layer: str, op: str, *, detail: Any = ""):
        self.layer = layer
        self.op = op
        self._base_detail = detail
        self._t0 = time.monotonic()
        self._last = self._t0

    def mark(
        self,
        stage: str,
        outcome: str = "success",
        *,
        status_code: Optional[int] = None,
        detail: Any = "",
        **extra: Any,
    ) -> float:
        """Log ``stage`` with elapsed-since-last-mark. Returns that delta (ms)."""
        now = time.monotonic()
        elapsed_ms = (now - self._last) * 1000.0
        self._last = now
        log_event(
            self.layer,
            self.op,
            stage,
            outcome,
            status_code=status_code,
            elapsed_ms=elapsed_ms,
            detail=detail or self._base_detail,
            total_ms=round((now - self._t0) * 1000.0, 1),
            **extra,
        )
        return elapsed_ms

    def total_ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    def reset(self) -> None:
        self._t0 = self._last = time.monotonic()


@contextmanager
def timed(
    layer: str,
    op: str,
    stage: str = "complete",
    *,
    detail: Any = "",
    **extra: Any,
) -> Iterator[dict[str, Any]]:
    """Context manager that logs one record with the elapsed time of the block.

    Outcome is ``success`` unless the block raises, in which case ``error`` is
    logged (and the exception re-raised). A mutable dict is yielded so the body
    can stash extra detail::

        with timed("command", "run_command", detail=op) as ctx:
            ctx["status_code"] = 0
    """
    ctx: dict[str, Any] = {"status_code": None, "detail": detail, "extra": dict(extra)}
    start = time.monotonic()
    try:
        yield ctx
    except Exception as exc:  # noqa: BLE001 - we log then re-raise unchanged
        log_event(
            layer, op, stage, "error",
            status_code=ctx.get("status_code"),
            elapsed_ms=(time.monotonic() - start) * 1000.0,
            detail=f"{ctx.get('detail', '')}: {type(exc).__name__}",
            **ctx.get("extra", {}),
        )
        raise
    else:
        log_event(
            layer, op, stage, "success",
            status_code=ctx.get("status_code"),
            elapsed_ms=(time.monotonic() - start) * 1000.0,
            detail=ctx.get("detail", ""),
            **ctx.get("extra", {}),
        )


# ──────────────────────────────────────────────────────────────────────────
# Usage analytics (Phase H seed) — local-only, same schema, layer="usage"
# ──────────────────────────────────────────────────────────────────────────

def log_usage(op: str, *, outcome: str = "success", detail: Any = "", **extra: Any) -> None:
    """Emit a lightweight local usage-analytics event (Phase H seed).

    NO external sync, NO full command text by default — callers pass only an op
    label and safe metadata. Detail/extra are still redacted as a backstop.
    """
    log_event("usage", op, "event", outcome, detail=detail, **extra)


def log_validation(
    op: str,
    stage: str,
    outcome: str,
    *,
    elapsed_ms: Optional[float] = None,
    detail: Any = "",
    **extra: Any,
) -> None:
    """Emit one validation-decision record (schema layer="validator").

    Records each validator decision so the demo can show WHY a command was
    accepted/passed-through and from which source (allowlist vs device_help).
    Schema: ``{ts, layer:"validator", op, stage, outcome, elapsed_ms, detail}``.
    Best-effort and never raises.
    """
    log_event("validator", op, stage, outcome, elapsed_ms=elapsed_ms, detail=detail, **extra)



# ──────────────────────────────────────────────────────────────────────────
# Feature flags
# ──────────────────────────────────────────────────────────────────────────

def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def event_listener_enabled() -> bool:
    """Real-time CML ``EventListener`` is OPT-IN (``CLIQ_CML_EVENTS=1``).

    Default off so the validated polling/terminal path is the default and the
    listener can be A/B-tested or disabled instantly.
    """
    return _flag("CLIQ_CML_EVENTS", default=False)


__all__ = [
    "CLIQ_HOME",
    "log_event",
    "log_usage",
    "redact",
    "StageTimer",
    "timed",
    "event_listener_enabled",
]
