#!/usr/bin/env python3
"""
session_health.py — Transport-agnostic device-session death detection for CLIQ.

A lab console reservation (CML/NetSim) can be reclaimed while its console socket
stays *temporarily* attachable, so CLIQ keeps re-attaching and streaming from a
session that is officially gone. When the node is finally released, an unguarded
device path hangs, streams empty/garbled output, or fails silently.

This module adds a small health layer that runs over the
:class:`cliq.connection.transport.DeviceTransport` protocol — the SAME structural
surface both :class:`~cliq.connection.live_device.CMLConsoleDevice` and
:class:`~cliq.connection.ssh_console_device.SSHConsoleDevice` expose — so the
detection is identical on either wire with no transport-specific fork beyond the
detection primitives (``_check_connection`` / ``reattach``) each backend already
implements.

The keepalive path calls :func:`probe_session_health`. It classifies a session as
``alive`` (optionally after a silent recovery re-attach) or ``dead`` with one of a
fixed set of reasons:

* ``socket_close``       — the channel is closed / reset and cannot be re-attached.
* ``keepalive_timeout``  — no response within the configured probe window.
* ``auth_loss``          — the device now demands (re-)authentication on re-attach.
* ``empty_reads``        — persistent empty reads where prompt/output was expected
                           (observed by the command path and fed in as a strike
                           count).

Everything here is best-effort and never mutates the device beyond the existing
``_check_connection`` poke and a single bounded ``reattach`` recovery attempt.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum

from cliq.connection.live_device import (
    ConnectionLostError,
    InteractiveAuthRequired,
)

# Default number of consecutive "empty read where output was expected" strikes the
# command path may observe before the session is treated as dead. Kept small so a
# genuinely reclaimed node is caught quickly, but > 1 so a single output-free
# command (many config commands legitimately return nothing) never false-fires.
EMPTY_READ_LIMIT = 3

# Default bounded window (seconds) for a single liveness poke or re-attach. Must be
# short enough that a hung socket is declared dead within a keepalive tick, yet long
# enough to tolerate a slow-but-alive terminal server.
PROBE_TIMEOUT_S = 8.0


class HealthState(str, Enum):
    """Outcome of a session-health probe."""

    ALIVE = "alive"
    DEAD = "dead"


class DeathReason(str, Enum):
    """Why a session was classified dead (fixed, redaction-safe vocabulary)."""

    SOCKET_CLOSE = "socket_close"
    KEEPALIVE_TIMEOUT = "keepalive_timeout"
    AUTH_LOSS = "auth_loss"
    EMPTY_READS = "empty_reads"


@dataclass
class HealthResult:
    """Classified result of :func:`probe_session_health`.

    ``reason`` is a :class:`DeathReason` (empty string when alive). ``recovered``
    is True when the session had dropped but a silent re-attach brought it back —
    the caller emits only a quiet ``device_status`` in that case, never a
    ``session_expired`` notice. ``prompt_last_seen`` is the last device prompt
    observed, surfaced in the UI notice so the operator knows where the session
    died.
    """

    state: HealthState
    reason: str = ""
    prompt_last_seen: str = ""
    recovered: bool = False

    @property
    def is_dead(self) -> bool:
        return self.state is HealthState.DEAD


def _safe_prompt(device) -> str:
    """Best-effort read of the device's last-seen prompt (never raises)."""
    try:
        return getattr(device, "current_prompt", "") or ""
    except Exception:  # pragma: no cover - defensive
        return ""


def _is_auth_error(exc: BaseException) -> bool:
    """True when ``exc`` indicates the session lost authentication / must re-login.

    A re-attach against a reclaimed reservation frequently comes back demanding a
    fresh login (``InteractiveAuthRequired``) or surfaces an auth/permission
    failure from the gateway — distinct from a plain socket close.
    """
    if isinstance(exc, InteractiveAuthRequired):
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in ("auth", "password", "login", "denied", "permission", "credential")
    )


def _reason_for(exc: BaseException) -> DeathReason:
    """Map a connection failure to a death reason (auth-loss vs socket-close)."""
    return DeathReason.AUTH_LOSS if _is_auth_error(exc) else DeathReason.SOCKET_CLOSE


async def _bounded(coro_fn, timeout_s: float):
    """Run a blocking device primitive in a thread under a hard timeout."""
    return await asyncio.wait_for(asyncio.to_thread(coro_fn), timeout=timeout_s)


async def probe_session_health(
    device,
    *,
    timeout_s: float = PROBE_TIMEOUT_S,
    empty_read_strikes: int = 0,
    empty_read_limit: int = EMPTY_READ_LIMIT,
    allow_reattach: bool = True,
) -> HealthResult:
    """Classify the health of ``device`` over the ``DeviceTransport`` surface.

    Uses only the two detection primitives every backend implements —
    ``_check_connection`` (liveness poke) and ``reattach`` (silent recovery) — so
    CML and SSH sessions feed the identical health check.

    Order of detection:

    1. If the command path has already observed persistent empty reads
       (``empty_read_strikes >= empty_read_limit``), the session is dead
       (``empty_reads``) — do not keep poking a socket that is not answering.
    2. Poke the channel with a bounded ``_check_connection``. Success → alive.
       Timeout → ``keepalive_timeout``. Auth error → ``auth_loss``.
    3. On a lost-connection error, attempt ONE bounded silent ``reattach``
       (recovery). A re-attach that raises classifies the death; one that succeeds
       is then re-verified with a second bounded ``_check_connection`` before the
       session is declared alive-recovered (guards the *temporarily* attachable
       reclaimed-lab case).
    """
    prompt_last = _safe_prompt(device)

    # (1) Persistent empty reads already seen by the command path.
    if empty_read_strikes >= empty_read_limit:
        return HealthResult(HealthState.DEAD, DeathReason.EMPTY_READS, prompt_last)

    # (2) Liveness poke.
    try:
        await _bounded(device._check_connection, timeout_s)
        return HealthResult(HealthState.ALIVE, "", prompt_last)
    except asyncio.TimeoutError:
        return HealthResult(HealthState.DEAD, DeathReason.KEEPALIVE_TIMEOUT, prompt_last)
    except InteractiveAuthRequired:
        return HealthResult(HealthState.DEAD, DeathReason.AUTH_LOSS, prompt_last)
    except (ConnectionLostError, ConnectionError, OSError) as exc:
        first_exc: BaseException = exc
    except Exception as exc:  # pragma: no cover - defensive: treat as a drop
        first_exc = exc

    if not allow_reattach:
        return HealthResult(HealthState.DEAD, _reason_for(first_exc), prompt_last)

    # (3) One silent recovery re-attach.
    try:
        await _bounded(device.reattach, timeout_s)
    except asyncio.TimeoutError:
        return HealthResult(HealthState.DEAD, DeathReason.KEEPALIVE_TIMEOUT, prompt_last)
    except InteractiveAuthRequired:
        return HealthResult(HealthState.DEAD, DeathReason.AUTH_LOSS, prompt_last)
    except Exception as exc:
        return HealthResult(HealthState.DEAD, _reason_for(exc), prompt_last)

    # (3b) Verify the re-attached session actually answers (reclaimed-lab guard).
    try:
        await _bounded(device._check_connection, timeout_s)
    except asyncio.TimeoutError:
        return HealthResult(HealthState.DEAD, DeathReason.KEEPALIVE_TIMEOUT, prompt_last)
    except InteractiveAuthRequired:
        return HealthResult(HealthState.DEAD, DeathReason.AUTH_LOSS, prompt_last)
    except Exception as exc:
        return HealthResult(HealthState.DEAD, _reason_for(exc), prompt_last)

    return HealthResult(HealthState.ALIVE, "", _safe_prompt(device), recovered=True)
