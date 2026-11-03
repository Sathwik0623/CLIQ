"""
Lab start/stop lifecycle (M1.1 Parts B+E, polling fix).

Starts or stops a controller lab using ONLY SDK-verified operations —
``Lab.start(wait=False)`` (PUT ``labs/{id}/start``) and
``Lab.stop(wait=False)`` (PUT ``labs/{id}/stop``) — then polls a verified
read snapshot until the controller confirms the result.

Completion contract (live-controller evidence + SDK semantics):
- START completes ⟺ ``/state`` reads ``STARTED`` (the SDK ``is_active``
  marker; live CML 2.9 returns the JSON string ``"STARTED"`` for running
  labs, matching the dashboard ON indicator) OR node evidence: converged
  with EVERY node in the SDK active set (``STARTED``/``QUEUED``/``BOOTED``).
  Running is therefore never declared on convergence alone — a defined lab
  (converged, zero active nodes) and a pre-transition read right after the
  PUT both keep polling instead of completing.
- STOP completes ⟺ ``check_if_converged`` is true AND ``/state`` is not
  ``STARTED``. The stopped string itself is never asserted.
- Pre-checks use the same running evidence so a running lab is never
  re-started (no duplicate PUT) and a defined lab is never reported
  running.

State mapping (honest by construction):
- ``Running`` ⟺ ``/state`` is ``STARTED``, or a start op completed, or
  node evidence (converged + all nodes active) on a fresh read.
- ``Defined`` ⟺ a state was read and is not ``STARTED``. The raw controller
  string is always preserved alongside — CLIQ never invents or rewrites it.
- Boot progress (``nodes_booted``/``nodes_total``) is a SEPARATE,
  device-level signal: a partially booted lab is Running but not fully
  ready, and console access stays gated on per-device ``BOOTED``.
- ``Starting`` / ``Stopping`` ⟺ OUR in-flight tracked operations, which win
  over any live read (a ``STARTED`` sample mid-stop is "still stopping",
  never Running). ``Failed`` overlays only non-``STARTED`` reads.
- ``Unknown`` ⟺ state could not be read (the failure is preserved as the
  lab/operation error — never converted to Defined).

Hard exclusions (also enforced by tests): no wipe/remove/delete calls, no
device configuration, no automatic retries of start/stop PUTs. Each PUT is
issued at most once per user-confirmed request; in-flight duplicates return
the existing operation and cross-kind overlap is refused with 409.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from cliq.labs.discovery import validate_lab_id
from cliq.labs.models import STATE_STARTED

log = logging.getLogger("cliq.labs.lifecycle")

#: Wall-clock budget for one operation before it times out.
START_TIMEOUT_SECONDS = 600.0

#: Per-session cap for retained operations.
MAX_OPS_PER_SESSION = 50


class LifecycleError(RuntimeError):
    """Raised for expected lifecycle failures (invalid, refused, failed)."""


def _truncate(text: str, limit: int = 300) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass
class LabSnapshot:
    """One verified read cycle: state + convergence + node boot progress."""

    raw_state: str = ""
    converged: Optional[bool] = None
    booted: Optional[int] = None
    active: Optional[int] = None
    total: Optional[int] = None

    def is_started_state(self) -> bool:
        return (self.raw_state or "").strip().upper() == STATE_STARTED

    def node_evidence_running(self) -> bool:
        """Converged with every known node in the SDK active set
        (``STARTED``/``QUEUED``/``BOOTED``).

        Defined labs are excluded: converged with zero active nodes must
        never read as running, and neither may convergence alone. Boot
        phase (``booted``) is deliberately NOT part of this check — it is
        the separate device-readiness signal.
        """
        return bool(
            self.converged is True
            and self.total is not None and self.total > 0
            and self.active == self.total
        )

    def evidence_running(self) -> bool:
        return self.is_started_state() or self.node_evidence_running()

    def confident_stopped(self) -> bool:
        """True only when the lab is provably at rest: readable non-STARTED
        state plus a readable element list with zero active nodes. Anything
        unreadable or active biases toward acting (a stop PUT on a stopped
        lab is a harmless no-op that completes immediately)."""
        return bool(
            (self.raw_state or "").strip()
            and not self.is_started_state()
            and self.active == 0
        )


def display_state(
    raw_state: str = "",
    operation_status: Optional[str] = None,
    *,
    converged: Optional[bool] = None,
    booted: Optional[int] = None,
    active: Optional[int] = None,
    total: Optional[int] = None,
) -> str:
    """Map controller reads + tracked operation to a display label.

    An in-flight operation (``starting``/``stopping``) always wins: a
    ``STARTED`` read mid-stop means "still up, stop in progress", not
    Running. Otherwise a live ``STARTED`` reading wins (the controller is
    the authority); terminal operations report their verified outcome;
    node evidence (converged + every node active) covers controllers whose
    ``/state`` does not flip; otherwise raw presence decides Defined vs
    Unknown. ``booted`` is accepted for device-level display but never
    decides Running (lab-level) on its own.
    """
    cleaned = (raw_state or "").strip().upper()
    if operation_status == "starting":
        return "Starting"
    if operation_status == "stopping":
        return "Stopping"
    if cleaned == STATE_STARTED:
        return "Running"
    if operation_status == "running":
        return "Running"
    if operation_status == "stopped":
        return "Defined"
    if operation_status == "failed":
        return "Failed"
    snapshot = LabSnapshot(raw_state=raw_state, converged=converged,
                           booted=booted, active=active, total=total)
    if snapshot.node_evidence_running():
        return "Running"
    if cleaned:
        return "Defined"
    return "Unknown"


@dataclass
class LifecycleOperation:
    op_id: str
    lab_id: str
    lab_title: str
    kind: str = "start"  # start | stop
    status: str = "starting"  # starting | stopping | running | stopped | failed
    error: str = ""
    raw_state: str = ""
    converged: Optional[bool] = None
    booted: Optional[int] = None
    active: Optional[int] = None
    total: Optional[int] = None
    detail: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "lab_id": self.lab_id,
            "lab_title": self.lab_title,
            "kind": self.kind,
            "status": self.status,
            "state_display": display_state(
                self.raw_state, self.status, converged=self.converged,
                booted=self.booted, active=self.active, total=self.total),
            "error": self.error,
            "raw_state": self.raw_state,
            "converged": self.converged,
            "nodes_booted": self.booted,
            "nodes_active": self.active,
            "nodes_total": self.total,
            "detail": self.detail,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


#: Backwards-compatible alias (op payloads always carried the same shape).
StartOperation = LifecycleOperation

#: Operation statuses the dashboard may overlay on fresh reads. Completed
#: operations never override: once terminal, live reads govern again.
DASHBOARD_OP_STATUSES = ("starting", "stopping", "failed")


#: Reentrant: refresh_operation intentionally holds this across _store /
#: get_operation (which lock internally) to keep each poll's read-modify-write
#: atomic. A plain Lock here self-deadlocks every poll and eventually
#: exhausts the server's worker threads.
_LOCK = threading.RLock()
_OPS: dict[tuple[str, str], LifecycleOperation] = {}
_ACTIVE: dict[tuple[str, str], str] = {}
_ACTIVE_STATUSES = ("starting", "stopping")


def _check_active(token: str, safe_id: str, kind: str) -> Optional[LifecycleOperation]:
    """Return the in-flight op for same-kind repeats, else refuse overlap."""
    with _LOCK:
        active_id = _ACTIVE.get((token, safe_id))
        if active_id is None:
            return None
        existing = _OPS.get((token, active_id))
        if existing is None or existing.status not in _ACTIVE_STATUSES:
            return None
        if existing.kind == kind:
            return existing
        raise LifecycleError(
            f"a {existing.kind} operation is already in progress for this lab")


def _terminal(
    token: str,
    safe_id: str,
    lab_title: str,
    kind: str,
    status: str,
    snapshot: Optional[LabSnapshot] = None,
    error: str = "",
    detail: str = "",
) -> tuple[LifecycleOperation, bool]:
    snapshot = snapshot or LabSnapshot()
    op = LifecycleOperation(
        op_id=f"{kind}-{uuid.uuid4().hex[:12]}", lab_id=safe_id,
        lab_title=lab_title, kind=kind, status=status,
        error=_truncate(error), raw_state=snapshot.raw_state,
        converged=snapshot.converged, booted=snapshot.booted,
        active=snapshot.active, total=snapshot.total,
        detail=_truncate(detail, 160),
    )
    _store(token, op, active=False)
    stored = get_operation(token, op.op_id)
    assert stored is not None
    return stored, True


def _store(token: str, op: LifecycleOperation, active: bool) -> None:
    op.updated_at = time.time()
    with _LOCK:
        owned = sorted(
            (k for k in _OPS if k[0] == token),
            key=lambda k: _OPS[k].created_at,
        )
        while len(owned) >= MAX_OPS_PER_SESSION:
            _OPS.pop(owned.pop(0), None)
        _OPS[(token, op.op_id)] = op
        if active:
            _ACTIVE[(token, op.lab_id)] = op.op_id
        elif _ACTIVE.get((token, op.lab_id)) == op.op_id:
            _ACTIVE.pop((token, op.lab_id), None)


def latest_operation_for_lab(token: str, lab_id: str) -> Optional[LifecycleOperation]:
    """Most recent lifecycle operation for a lab, if any (for list display)."""
    with _LOCK:
        candidates = [op for (tok, _), op in _OPS.items()
                      if tok == token and op.lab_id == lab_id]
    if not candidates:
        return None
    return max(candidates, key=lambda op: op.created_at)


def get_operation(token: str, op_id: str) -> Optional[LifecycleOperation]:
    with _LOCK:
        op = _OPS.get((token, op_id))
        if op is None:
            return None
        return LifecycleOperation(
            op_id=op.op_id, lab_id=op.lab_id, lab_title=op.lab_title,
            kind=op.kind, status=op.status, error=op.error,
            raw_state=op.raw_state, converged=op.converged,
            booted=op.booted, active=op.active, total=op.total,
            detail=op.detail,
            created_at=op.created_at, updated_at=op.updated_at,
        )


def start_lab(
    token: str,
    lab_id: str,
    lab_title: str,
    *,
    client_factory: Callable[[str, str, str], Any],
    session_host: str,
    session_username: str,
    session_password: str,
    get_snapshot: Callable[[], LabSnapshot],
) -> tuple[LifecycleOperation, bool]:
    """Issue ONE start PUT for a user-confirmed request.

    Returns ``(operation, created)``. ``created`` is False when an in-flight
    operation is returned instead of issuing a duplicate PUT. Labs already
    showing running evidence short-circuit with no PUT at all.
    """
    safe_id = validate_lab_id(lab_id)
    repeat = _check_active(token, safe_id, "start")
    if repeat is not None:
        snapshot = get_operation(token, repeat.op_id)
        assert snapshot is not None
        return snapshot, False

    try:
        pre = get_snapshot()
    except Exception as exc:
        return _terminal(token, safe_id, lab_title, "start",
                         "failed", error=f"could not read lab state: {exc}")

    if pre.evidence_running():
        return _terminal(token, safe_id, lab_title, "start", "running",
                         snapshot=pre, detail="already running")

    try:
        client = client_factory(session_host, session_username, session_password)
    except Exception as exc:
        return _terminal(token, safe_id, lab_title, "start", "failed",
                         snapshot=pre,
                         error=f"controller connection failed: {exc}")

    try:
        lab = client.join_existing_lab(safe_id, sync_lab=False)
        lab.start(wait=False)
    except Exception as exc:
        return _terminal(token, safe_id, lab_title, "start", "failed",
                         snapshot=pre, error=f"lab start failed: {exc}")

    op = LifecycleOperation(
        op_id=f"start-{uuid.uuid4().hex[:12]}", lab_id=safe_id,
        lab_title=lab_title, kind="start", status="starting",
        raw_state=pre.raw_state, converged=pre.converged,
        booted=pre.booted, active=pre.active, total=pre.total,
        detail="start accepted; polling controller state",
    )
    _store(token, op, active=True)
    snapshot = get_operation(token, op.op_id)
    assert snapshot is not None
    return snapshot, True


def stop_lab(
    token: str,
    lab_id: str,
    lab_title: str,
    *,
    client_factory: Callable[[str, str, str], Any],
    session_host: str,
    session_username: str,
    session_password: str,
    get_snapshot: Callable[[], LabSnapshot],
) -> tuple[LifecycleOperation, bool]:
    """Issue ONE stop PUT for a user-confirmed request.

    Mirrors :func:`start_lab`. Provably-at-rest labs short-circuit with
    no PUT; success is declared only when the controller confirms
    (converged, not ``STARTED``).
    """
    safe_id = validate_lab_id(lab_id)
    repeat = _check_active(token, safe_id, "stop")
    if repeat is not None:
        snapshot = get_operation(token, repeat.op_id)
        assert snapshot is not None
        return snapshot, False

    try:
        pre = get_snapshot()
    except Exception as exc:
        return _terminal(token, safe_id, lab_title, "stop",
                         "failed", error=f"could not read lab state: {exc}")

    if pre.confident_stopped():
        return _terminal(token, safe_id, lab_title, "stop", "stopped",
                         snapshot=pre, detail="already stopped")

    try:
        client = client_factory(session_host, session_username, session_password)
    except Exception as exc:
        return _terminal(token, safe_id, lab_title, "stop", "failed",
                         snapshot=pre,
                         error=f"controller connection failed: {exc}")

    try:
        lab = client.join_existing_lab(safe_id, sync_lab=False)
        lab.stop(wait=False)
    except Exception as exc:
        return _terminal(token, safe_id, lab_title, "stop", "failed",
                         snapshot=pre, error=f"lab stop failed: {exc}")

    op = LifecycleOperation(
        op_id=f"stop-{uuid.uuid4().hex[:12]}", lab_id=safe_id,
        lab_title=lab_title, kind="stop", status="stopping",
        raw_state=pre.raw_state, converged=pre.converged,
        booted=pre.booted, active=pre.active, total=pre.total,
        detail="stop accepted; polling controller state",
    )
    _store(token, op, active=True)
    snapshot = get_operation(token, op.op_id)
    assert snapshot is not None
    return snapshot, True


def refresh_operation(
    token: str,
    op_id: str,
    get_snapshot: Callable[[], LabSnapshot],
) -> Optional[LifecycleOperation]:
    """Poll the controller snapshot for an operation (read-only refresh).

    Read failures never fail the operation (transient blips ride through);
    only the wall-clock timeout or an explicit terminal state ends it.
    Snapshot reads are inherently safe to repeat — the start/stop PUT
    itself is never re-issued here. Success requires controller
    confirmation: ``STARTED``, or converged with every node active, for
    start; converged and not ``STARTED`` for stop.
    """
    terminal_statuses = ("running", "stopped", "failed")
    with _LOCK:
        op = _OPS.get((token, op_id))
        if op is None or op.status in terminal_statuses:
            return get_operation(token, op_id)
        kind = op.kind
        age = time.time() - op.created_at
        if age > START_TIMEOUT_SECONDS:
            op.status = "failed"
            want = "RUNNING" if kind == "start" else "STOPPED"
            op.error = _truncate(
                f"timed out after {int(START_TIMEOUT_SECONDS)}s waiting for {want}")
            _store(token, op, active=False)
            return get_operation(token, op_id)
    try:
        snap = get_snapshot()
    except Exception as exc:
        with _LOCK:
            live = _OPS.get((token, op_id))
            if live is not None and live.status in _ACTIVE_STATUSES:
                live.detail = _truncate(f"last poll failed: {exc}", 160)
                _store(token, live, active=True)
        return get_operation(token, op_id)
    with _LOCK:
        live = _OPS.get((token, op_id))
        if live is None or live.status not in _ACTIVE_STATUSES:
            return get_operation(token, op_id)
        live.raw_state = snap.raw_state
        live.converged = snap.converged
        live.booted = snap.booted
        live.active = snap.active
        live.total = snap.total
        if live.kind == "start" and (snap.is_started_state()
                                     or snap.node_evidence_running()):
            live.status = "running"
            live.detail = "controller confirms lab running"
            live.error = ""
            _store(token, live, active=False)
        elif (live.kind == "stop" and snap.converged is True
              and not snap.is_started_state()):
            live.status = "stopped"
            live.detail = f"controller reports {snap.raw_state or 'not STARTED'}"
            live.error = ""
            _store(token, live, active=False)
        else:
            verb = "start" if live.kind == "start" else "stop"
            live.detail = f"{verb} accepted; polling controller state"
            _store(token, live, active=True)
    return get_operation(token, op_id)
