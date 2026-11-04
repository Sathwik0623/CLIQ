"""
CLIQ M1.4 — Plan approval and multi-device ordered execution.

Workflow: prepare (review) -> approve (explicit) -> execute (ordered).

Approval binds the exact ``(session, plan, version, fingerprint)`` tuple:
fingerprint covers lab + devices + commands + order, so any plan modification
invalidates outstanding tickets and tokens. Tokens are single-use and expire.

Execution reuses the M1.2 pathway exclusively: identical ownership, platform,
and command validation, then :func:`cliq.ai.device_exec._run_validated_command`
(the same runtime lock + console driver the terminal uses). No parallel
terminal implementation exists here.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from cliq.ai.config_plan import ConfigPlan, fingerprint_plan, get_plan
from cliq.ai.device_exec import (
    PLATFORM_CISCO_IOS,
    _run_validated_command,
    resolve_owned_device,
    validate_command_for_platform,
)

log = logging.getLogger("cliq.ai.config_exec")

PLAN_TICKET_TTL_SECONDS = 10 * 60
PLAN_TOKEN_TTL_SECONDS = 5 * 60
DEFAULT_STEP_TIMEOUT = 30.0

_lock = threading.Lock()
_plan_tickets: Dict[str, Dict[str, Any]] = {}
_plan_tokens: Dict[str, Dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def _prune_locked() -> None:
    now = _now()
    for tid in [k for k, v in _plan_tickets.items() if v["expires_at"] <= now]:
        _plan_tickets.pop(tid, None)
    for tok in [k for k, v in _plan_tokens.items() if v["expires_at"] <= now]:
        _plan_tokens.pop(tok, None)


# ── Prepare / approve ────────────────────────────────────────────────────────

_L2_SWITCH_COMMAND_RE = re.compile(r"(?i)^\s*(vlan\s|switchport\b|name\s|spanning-tree\b)")


def _plan_problems(plan: ConfigPlan) -> List[str]:
    """Static pre-approval checks. Empty list means reviewable."""
    problems: List[str] = []
    for q in plan.questions:
        if q.blocking and not q.resolved:
            problems.append(f"blocking question open: {q.message[:120]}")
    if not any(d.steps for d in plan.devices):
        problems.append("plan contains no executable steps")
    for dp in plan.devices:
        if dp.platform == PLATFORM_CISCO_IOS and dp.role != "switch":
            for step in dp.steps:
                if _L2_SWITCH_COMMAND_RE.match(step.command or ""):
                    problems.append(
                        f"{dp.device_label} is a {dp.role}; Layer-2 switch "
                        f"commands are not allowed on it: '{step.command}'")
                    break
        for step in dp.steps:
            verdict = validate_command_for_platform(step.command, dp.platform)
            if not verdict.ok:
                problems.append(
                    f"{dp.device_label} '{step.command}': {verdict.message}")
    return problems


def prepare_plan(session_token: str, plan_id: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Validate a plan for review and mint an approval ticket. No execution."""
    plan = get_plan(plan_id)
    if plan is None:
        return None, "not_found"
    if plan.owner_token != session_token:
        return None, "forbidden"
    if plan.status in ("executing", "completed"):
        return None, "locked"
    problems = _plan_problems(plan)
    if problems:
        return None, "blocked:" + " | ".join(problems[:5])
    current = fingerprint_plan(plan.lab_id, plan.devices)
    plan.fingerprint = current
    ticket_id = f"cfg-{uuid.uuid4().hex[:12]}"
    with _lock:
        _prune_locked()
        _plan_tickets[ticket_id] = {
            "ticket_id": ticket_id,
            "session_token": session_token,
            "plan_id": plan.plan_id,
            "version": plan.version,
            "fingerprint": current,
            "lab_id": plan.lab_id,
            "created_at": _now(),
            "expires_at": _now() + PLAN_TICKET_TTL_SECONDS,
        }
        plan.status = "pending_approval"
    return {k: v for k, v in _plan_tickets[ticket_id].items() if k != "session_token"}, ""


def approve_plan(session_token: str, ticket_id: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Exchange an explicit operator approval for a single-use plan token."""
    with _lock:
        _prune_locked()
        ticket = _plan_tickets.get(ticket_id)
        if not ticket or ticket["session_token"] != session_token:
            return None, "invalid_ticket"
        plan = get_plan(ticket["plan_id"])
        if plan is None:
            _plan_tickets.pop(ticket_id, None)
            return None, "not_found"
        if plan.owner_token != session_token:
            _plan_tickets.pop(ticket_id, None)
            return None, "forbidden"
        current = fingerprint_plan(plan.lab_id, plan.devices)
        if plan.version != ticket["version"] or current != ticket["fingerprint"]:
            _plan_tickets.pop(ticket_id, None)
            return None, "plan_modified"
        problems = _plan_problems(plan)
        if problems:
            _plan_tickets.pop(ticket_id, None)
            return None, "blocked:" + " | ".join(problems[:5])
        _plan_tickets.pop(ticket_id, None)
        token = f"cfgappr-{uuid.uuid4().hex}"
        _plan_tokens[token] = {
            "approval_token": token,
            "session_token": session_token,
            "plan_id": plan.plan_id,
            "version": plan.version,
            "fingerprint": current,
            "lab_id": plan.lab_id,
            "created_at": _now(),
            "expires_at": _now() + PLAN_TOKEN_TTL_SECONDS,
            "state": "valid",
        }
        plan.status = "approved"
        return {k: v for k, v in _plan_tokens[token].items() if k != "session_token"}, ""


def _claim_plan_token(session_token: str, plan_id: str,
                      approval_token: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Validate a plan token for one execution and move it to in_use."""
    with _lock:
        _prune_locked()
        rec = _plan_tokens.get(approval_token or "")
        if not rec or rec["session_token"] != session_token:
            return None, "invalid_token"
        if rec["plan_id"] != plan_id:
            return None, "wrong_plan"
        if rec.get("state") != "valid":
            return None, "token_reused"
        plan = get_plan(plan_id)
        if plan is None:
            return None, "not_found"
        if plan.owner_token != session_token:
            return None, "forbidden"
        current = fingerprint_plan(plan.lab_id, plan.devices)
        if plan.version != rec["version"] or current != rec["fingerprint"]:
            return None, "plan_modified"
        if plan.status == "executing":
            return None, "token_reused"
        rec["state"] = "in_use"
        plan.status = "executing"
        return dict(rec), ""


def _release_plan_token(approval_token: str) -> None:
    with _lock:
        rec = _plan_tokens.get(approval_token or "")
        if rec:
            rec["state"] = "used"


# ── Ordered execution ────────────────────────────────────────────────────────

@dataclass
class StepResult:
    step_id: str = ""
    device_id: str = ""
    device_label: str = ""
    command: str = ""
    send_command: str = ""
    purpose: str = ""
    status: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_status: int = 0
    error: str = ""
    error_code: str = ""
    latency_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PlanExecution:
    plan_id: str = ""
    version: int = 0
    fingerprint: str = ""
    lab_id: str = ""
    status: str = ""
    steps: List[StepResult] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "lab_id": self.lab_id,
            "status": self.status,
            "steps": [s.to_dict() for s in self.steps],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "summary": self.summary,
        }


def _ordered_steps(plan: ConfigPlan) -> List[Tuple[str, Any]]:
    """Global step order: device order, then step order within each device."""
    ordered: List[Tuple[str, Any]] = []
    for dp in plan.devices:
        for step in dp.steps:
            ordered.append((dp.device_id, step))
    return ordered


async def execute_plan(session_token: str, plan_id: str,
                       approval_token: str,
                       timeout: float = DEFAULT_STEP_TIMEOUT,
                       ) -> AsyncGenerator[Dict[str, Any], None]:
    """Execute an approved plan step-by-step, yielding SSE-style events.

    Events: ``plan_started``, ``step_started``, ``step_result``,
    ``plan_finished`` (carries the full execution record), or a single
    ``plan_rejected`` when approval/ownership checks fail before any device I/O.
    """
    plan = get_plan(plan_id)
    if plan is None:
        yield {"type": "plan_rejected", "error_code": "not_found",
               "message": f"Plan '{plan_id}' not found."}
        return
    rec, err = _claim_plan_token(session_token, plan_id, approval_token)
    if err:
        messages = {
            "invalid_token": "Invalid, expired, or foreign plan approval token.",
            "forbidden": "Plan belongs to a different session.",
            "wrong_plan": "Approval token was issued for a different plan.",
            "token_reused": "Approval token was already used (single-use).",
            "not_found": f"Plan '{plan_id}' not found.",
            "plan_modified": ("Plan was modified after approval (version/fingerprint "
                              "mismatch). Re-review and re-approve."),
        }
        yield {"type": "plan_rejected", "error_code": err,
               "message": messages.get(err, err)}
        return
    assert rec is not None
    execution = PlanExecution(plan_id=plan.plan_id, version=plan.version,
                              fingerprint=plan.fingerprint, lab_id=plan.lab_id,
                              status="executing", started_at=_now())
    plan.status = "executing"
    terminated = False
    try:
        ordered = _ordered_steps(plan)
        yield {"type": "plan_started", "plan_id": plan.plan_id,
               "version": plan.version, "lab_id": plan.lab_id,
               "total_steps": len(ordered)}
        by_id: Dict[str, StepResult] = {}
        for device_id, step in ordered:
            unmet = [d for d in step.depends_on
                     if d not in by_id or by_id[d].status != "success"]
            if unmet:
                skipped = StepResult(
                    step_id=step.step_id, device_id=device_id,
                    device_label=_label_of(plan, device_id),
                    command=step.command, purpose=step.purpose,
                    status="skipped",
                    error=f"Skipped: prerequisite step(s) {', '.join(unmet)} did not succeed.",
                    error_code="dependency_failed")
                by_id[step.step_id] = skipped
                execution.steps.append(skipped)
                yield {"type": "step_result", "step": skipped.to_dict()}
                continue
            yield {"type": "step_started", "step_id": step.step_id,
                   "device_id": device_id,
                   "device_label": _label_of(plan, device_id),
                   "command": step.command, "purpose": step.purpose}
            result = await _execute_approved_step(
                session_token, plan, device_id, step, timeout)
            by_id[step.step_id] = result
            execution.steps.append(result)
            yield {"type": "step_result", "step": result.to_dict()}
        succeeded = sum(1 for s in execution.steps if s.status == "success")
        failed = sum(1 for s in execution.steps if s.status == "failed")
        skipped = sum(1 for s in execution.steps if s.status == "skipped")
        total = len(execution.steps)
        if total and succeeded == total:
            execution.status = "completed"
            plan.status = "completed"
            execution.summary = f"All {total} steps succeeded."
        else:
            execution.status = "failed"
            plan.status = "failed"
            execution.summary = (f"{succeeded}/{total} steps succeeded, "
                                 f"{failed} failed, {skipped} skipped. "
                                 "Plan did NOT complete successfully.")
        execution.finished_at = _now()
        yield {"type": "plan_finished", "execution": execution.to_dict()}
        terminated = True
    finally:
        _release_plan_token(approval_token)
        if not terminated:
            plan.status = "failed"


def _label_of(plan: ConfigPlan, device_id: str) -> str:
    for dp in plan.devices:
        if dp.device_id == device_id:
            return dp.device_label
    return device_id


async def _execute_approved_step(session_token: str, plan: ConfigPlan,
                                 device_id: str, step: Any,
                                 timeout: float) -> StepResult:
    """Run one approved step through the shared validated-run pathway."""
    label = _label_of(plan, device_id)
    fail = lambda code, msg: StepResult(  # noqa: E731
        step_id=step.step_id, device_id=device_id, device_label=label,
        command=step.command, purpose=step.purpose, status="failed",
        error=msg, error_code=code)
    meta, err = resolve_owned_device(session_token, plan.lab_id, device_id)
    if err:
        return fail(err, {"unauthorized": "Invalid session.",
                          "device_not_found": f"Device '{device_id}' is not in this session.",
                          "wrong_lab": f"Device '{device_id}' is not in lab '{plan.lab_id}'."}[err])
    assert meta is not None
    dp = next((d for d in plan.devices if d.device_id == device_id), None)
    if dp is None:
        return fail("not_in_plan", f"Device '{device_id}' is not in the approved plan.")
    verdict = validate_command_for_platform(step.command, dp.platform)
    if not verdict.ok:
        return fail(verdict.error_code, verdict.message)
    res = await _run_validated_command(
        session_token, plan.lab_id, device_id, label, dp.platform,
        step.command, verdict.send_command, timeout, time.perf_counter(),
        actor="copilot-plan", plan_id=plan.plan_id)
    if res.success:
        return StepResult(step_id=step.step_id, device_id=device_id,
                          device_label=label, command=step.command,
                          send_command=res.send_command, purpose=step.purpose,
                          status="success", stdout=res.stdout, stderr=res.stderr,
                          exit_status=res.exit_status, latency_ms=res.latency_ms)
    return StepResult(step_id=step.step_id, device_id=device_id,
                      device_label=label, command=step.command,
                      send_command=res.send_command, purpose=step.purpose,
                      status="failed", stdout=res.stdout, stderr=res.stderr,
                      exit_status=res.exit_status, error=res.error,
                      error_code=res.error_code, latency_ms=res.latency_ms)
