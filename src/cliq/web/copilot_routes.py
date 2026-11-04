"""
CLIQ AI Copilot REST & Streaming API Router (Phase V).

Provides HTTP and Server-Sent Events (SSE) endpoints for AI conversation management,
file uploads, network context extraction, and token-by-token streaming responses.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from cliq.ai.attachments import get_attachment_service
from cliq.ai.conversations import ConversationStore
from cliq.ai.network_context import get_network_context_service
from cliq.ai.orchestrator import get_copilot_orchestrator
from cliq.ai.registry import list_models, list_providers
from cliq.ai import device_exec as _device_exec
from cliq.ai import baseline as _baseline
from cliq.ai import config_exec as _config_exec
from cliq.ai import config_plan as _config_plan

log = logging.getLogger("cliq.web.copilot")

router = APIRouter(prefix="/api/copilot", tags=["copilot"])
store = ConversationStore()
orchestrator = get_copilot_orchestrator()
attachment_service = get_attachment_service()
context_service = get_network_context_service()


# ── Pydantic Request Models ───────────────────────────────────────────────────

class CreateConversationRequest(BaseModel):
    title: str = "New Conversation"
    model: Optional[str] = "muse-spark-1.3"
    provider: str = "meta"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RenameConversationRequest(BaseModel):
    title: str


class SendMessageRequest(BaseModel):
    content: str
    model: Optional[str] = None
    provider: str = "meta"
    context_chips: List[Dict[str, Any]] = Field(default_factory=list)
    attachments: List[Dict[str, Any]] = Field(default_factory=list)


class UploadAttachmentRequest(BaseModel):
    filename: str
    content: str  # raw text or base64 encoded
    conversation_id: str
    is_base64: bool = False


class ExecRequest(BaseModel):
    lab_id: str
    device_id: str
    command: str
    timeout: float = 30.0
    approval_token: Optional[str] = None


class ExecPrepareRequest(BaseModel):
    lab_id: str
    device_id: str
    command: str


class ExecConfirmRequest(BaseModel):
    ticket_id: str


class ConfigPlanRequest(BaseModel):
    prompt: str
    lab_id: Optional[str] = None
    device_ids: Optional[List[str]] = None


class ConfigAnswerRequest(BaseModel):
    answers: Dict[str, str] = Field(default_factory=dict)


class ConfigPrepareRequest(BaseModel):
    plan_id: str


class ConfigApproveRequest(BaseModel):
    ticket_id: str


class ConfigExecuteRequest(BaseModel):
    plan_id: str
    approval_token: str
    timeout: float = 30.0


class ConfigBaselineRequest(BaseModel):
    plan_id: Optional[str] = None
    lab_id: Optional[str] = None
    device_ids: Optional[List[str]] = None
    stp_vlans: Optional[List[int]] = None


# ── Provider & Model Metadata ────────────────────────────────────────────────

@router.get("/providers")
def get_providers_and_models() -> Dict[str, Any]:
    """List available AI providers and authoritative model catalogs."""
    return {
        "providers": list_providers(),
        "models": [
            {
                "id": m.id,
                "name": m.name,
                "provider": m.provider,
                "description": m.description,
                "context_window": m.context_window,
                "supports_streaming": m.supports_streaming,
                "supports_tools": m.supports_tools,
                "is_default": m.is_default,
                "available": getattr(m, "available", True),
            }
            for m in list_models()
        ],
    }


# ── Conversation Management ──────────────────────────────────────────────────

@router.get("/conversations")
def list_conversations(
    token: Optional[str] = Query(None),
    query: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """List or search previous conversations isolated by session token."""
    _require_copilot_session(token)
    if query and query.strip():
        convs = store.search_conversations(query.strip(), session_token=token)
    else:
        convs = store.list_conversations(session_token=token)
    return {
        "conversations": [c.to_dict(include_messages=False) for c in convs]
    }


@router.post("/conversations")
def create_conversation(
    payload: CreateConversationRequest,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Initialize a new conversation thread."""
    session_token = _require_copilot_session(token)
    conv = store.create_conversation(
        title=payload.title,
        model=payload.model or "muse-spark-1.3",
        provider=payload.provider,
        session_token=session_token,
        metadata=payload.metadata,
    )
    return conv.to_dict(include_messages=True)


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Retrieve full conversation and message history."""
    session_token = _require_copilot_session(token)
    conv = store.get_conversation(conversation_id, session_token=session_token)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv.to_dict(include_messages=True)


@router.patch("/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: str,
    payload: RenameConversationRequest,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Rename conversation title."""
    session_token = _require_copilot_session(token)
    success = store.rename_conversation(conversation_id, payload.title, session_token=session_token)
    if not success:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"success": True, "title": payload.title.strip()}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Delete conversation and its associated message history."""
    session_token = _require_copilot_session(token)
    success = store.delete_conversation(conversation_id, session_token=session_token)
    if not success:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"success": True, "id": conversation_id}


# ── Chat Streaming (Server-Sent Events) ──────────────────────────────────────

@router.post("/conversations/{conversation_id}/messages")
async def send_message_stream(
    conversation_id: str,
    payload: SendMessageRequest,
    token: Optional[str] = Query(None),
):
    """Send user message with optional context and stream AI reply via SSE."""
    session_token = _require_copilot_session(token)
    if not payload.content.strip():
        raise HTTPException(status_code=422, detail="Message content cannot be empty")

    async def sse_generator():
        try:
            async for event in orchestrator.handle_user_message_stream(
                conversation_id=conversation_id,
                user_text=payload.content,
                session_token=session_token,
                model=payload.model,
                provider_id=payload.provider,
                context_chips=payload.context_chips,
                attachments=payload.attachments,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as ex:
            log.error("SSE stream error: %s", ex)
            yield f"data: {json.dumps({'type': 'error', 'message': str(ex)[:100]})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── File Attachments ─────────────────────────────────────────────────────────

@router.post("/upload")
def upload_attachment(
    payload: UploadAttachmentRequest,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Safely validate, sanitize, and store an uploaded configuration/log file."""
    session_token = _require_copilot_session(token)
    if not store.get_conversation(payload.conversation_id, session_token=session_token):
        raise HTTPException(status_code=404, detail="Conversation not found")
    try:
        if payload.is_base64:
            raw_bytes = base64.b64decode(payload.content)
        else:
            raw_bytes = payload.content.encode("utf-8")

        info = attachment_service.save_attachment(
            conversation_id=payload.conversation_id,
            filename=payload.filename,
            content_bytes=raw_bytes,
        )
        return {"success": True, "attachment": info.to_dict(include_text=True)}
    except ValueError as val_err:
        raise HTTPException(status_code=400, detail=str(val_err))
    except Exception as ex:
        log.error("Attachment upload failure: %s", ex)
        raise HTTPException(status_code=500, detail=f"Failed to process file: {str(ex)[:80]}")


# ── Network Context Endpoints ────────────────────────────────────────────────

@router.get("/context/devices")
def get_context_devices(token: Optional[str] = Query(None)) -> Dict[str, Any]:
    """Enumerate accessible network devices for the Add Context dialog."""
    session_token = _require_copilot_session(token)
    devices = context_service.list_devices(session_token=session_token)
    return {"devices": devices}


@router.get("/context/query")
def query_device_context(
    device_id: str,
    context_type: str = Query("interfaces", pattern="^(interfaces|routes|config|neighbors|history|terminal_history|device)$"),
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Extract read-only, secret-redacted state for a specific device context chip."""
    session_token = _require_copilot_session(token)
    data = ""
    if context_type == "interfaces":
        data = context_service.get_interfaces(session_token, device_id)
    elif context_type == "routes":
        data = context_service.get_routing_table(session_token, device_id)
    elif context_type == "config":
        data = context_service.get_running_config(session_token, device_id)
    elif context_type == "neighbors":
        data = context_service.get_neighbors(session_token, device_id)
    elif context_type in ("history", "terminal_history"):
        hist = context_service.get_recent_commands(session_token, device_id)
        data = "\n".join(hist)
    elif context_type == "device":
        info = context_service.get_device_info(session_token, device_id)
        intfs = context_service.get_interfaces(session_token, device_id)
        data = f"Status: {info.get('status')}\nMode: {info.get('mode')}\n\nInterfaces:\n{intfs}"

    return {
        "device_id": device_id,
        "context_type": context_type,
        "data": data,
    }
# -- Autonomous Device Command Execution (M1.2) --------------------------------

def _require_copilot_session(token: Optional[str]) -> str:
    """Fail-closed session check for exec endpoints (401 on bad token)."""
    from cliq.web import server
    if not token or token not in server.SESSIONS:
        raise HTTPException(status_code=401, detail="Authentication required")
    return token


_EXEC_STATUS_BY_CODE = {
    "device_not_found": 404,
    "wrong_lab": 403,
    "unsupported_platform": 422,
    "platform_mismatch": 422,
    "blocked_destructive": 403,
    "approval_required": 403,
    "device_disconnected": 409,
    "timeout": 504,
    "execution_error": 502,
    "empty_command": 422,
    "multi_command": 422,
    "command_too_long": 422,
    "unsupported_command": 422,
}


@router.get("/exec/tool-spec")
def exec_tool_spec() -> Dict[str, Any]:
    """Return the autonomous execution tool contract (platforms, parameters)."""
    return dict(_device_exec.TOOL_SPEC)


@router.post("/exec/prepare")
def exec_prepare(
    payload: ExecPrepareRequest,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Validate a command and mint an approval ticket when required. No execution."""
    session_token = _require_copilot_session(token)
    lab_id = payload.lab_id.strip()
    device_id = payload.device_id.strip()
    meta, err = _device_exec.resolve_owned_device(session_token, lab_id, device_id)
    if err == "unauthorized":
        raise HTTPException(status_code=401, detail="Authentication required")
    if err == "device_not_found":
        raise HTTPException(status_code=404, detail=f"Device '{device_id}' is not in this session.")
    if err == "wrong_lab":
        raise HTTPException(status_code=403, detail=f"Device '{device_id}' does not belong to lab '{lab_id}'.")
    assert meta is not None
    label = str(meta.get("label") or device_id)
    platform = _device_exec.classify_platform(
        str(meta.get("type") or ""), str(meta.get("ssh_device_type") or ""))
    if not platform:
        raise HTTPException(
            status_code=422,
            detail=f"Device '{label}' has unsupported platform '{meta.get('type')}' for Copilot execution.")
    verdict = _device_exec.validate_command_for_platform(payload.command, platform)
    if not verdict.ok:
        raise HTTPException(
            status_code=_EXEC_STATUS_BY_CODE.get(verdict.error_code, 422),
            detail=verdict.message)
    if verdict.read_only:
        return {
            "approval_required": False,
            "read_only": True,
            "platform": platform,
            "device_id": device_id,
            "device_label": label,
            "lab_id": lab_id,
            "send_command": verdict.send_command,
            "message": f"'{payload.command.strip()}' is read-only and may execute immediately.",
        }
    ticket = _device_exec.prepare_approval(
        session_token, lab_id, device_id, label, platform, payload.command)
    ticket = {k: v for k, v in ticket.items() if k != "session_token"}
    return {
        "approval_required": True,
        "read_only": False,
        "platform": platform,
        "device_id": device_id,
        "device_label": label,
        "lab_id": lab_id,
        "ticket": ticket,
        "message": (f"'{payload.command.strip()}' may change configuration on {label}; "
                    "explicit approval is required before execution."),
    }


@router.post("/exec/confirm")
def exec_confirm(
    payload: ExecConfirmRequest,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Exchange an approved ticket for a single-use approval token."""
    session_token = _require_copilot_session(token)
    rec = _device_exec.confirm_approval(session_token, payload.ticket_id.strip())
    if not rec:
        raise HTTPException(
            status_code=403,
            detail="Invalid, expired, or foreign approval ticket.")
    return {
        "approval_token": rec["approval_token"],
        "expires_at": rec["expires_at"],
        "lab_id": rec["lab_id"],
        "device_id": rec["device_id"],
        "command": rec["command"],
    }


@router.post("/exec")
async def exec_command(
    payload: ExecRequest,
    token: Optional[str] = Query(None),
):
    """Execute one command on a device via the existing console session.

    Read-only diagnostics execute immediately within the authorized lab scope;
    configuration-changing commands require a single-use ``approval_token``
    from ``/exec/prepare`` + ``/exec/confirm``. Critically destructive commands
    are always rejected.
    """
    session_token = _require_copilot_session(token)
    result = await _device_exec.execute_device_command(
        session_token,
        payload.lab_id.strip(),
        payload.device_id.strip(),
        payload.command,
        timeout=payload.timeout,
        approval_token=payload.approval_token,
    )
    if result.success or result.error_code == "device_error":
        # Transport succeeded (device_error still carries actual stdout).
        return result.to_dict()
    status = _EXEC_STATUS_BY_CODE.get(result.error_code, 500)
    if result.error_code == "unauthorized":
        status = 401
    return JSONResponse(status_code=status, content=result.to_dict())
# -- Autonomous Network Configuration (M1.4) -----------------------------------

@router.post("/config/plan")
async def config_plan(
    payload: ConfigPlanRequest,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Build a reviewable multi-device configuration plan (read-only evidence)."""
    session_token = _require_copilot_session(token)
    if not payload.prompt.strip():
        raise HTTPException(status_code=422, detail="Prompt cannot be empty")
    plan, err = await _config_plan.build_plan(
        session_token, payload.prompt,
        lab_id=(payload.lab_id or "").strip() or None,
        device_ids=payload.device_ids)
    if err == "unauthorized":
        raise HTTPException(status_code=401, detail="Authentication required")
    if err == "no_devices":
        raise HTTPException(status_code=404, detail="No devices in this session.")
    if err == "wrong_lab":
        raise HTTPException(status_code=403, detail="Devices do not belong to the given lab.")
    assert plan is not None
    return plan.to_dict()


@router.get("/config/plan/{plan_id}")
def config_plan_get(
    plan_id: str,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    session_token = _require_copilot_session(token)
    plan = _config_plan.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    if plan.owner_token != session_token:
        raise HTTPException(status_code=403, detail="Plan belongs to a different session.")
    return plan.to_dict()


@router.post("/config/plan/{plan_id}/answer")
def config_plan_answer(
    plan_id: str,
    payload: ConfigAnswerRequest,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Answer plan questions; any change bumps the version (invalidates approvals)."""
    session_token = _require_copilot_session(token)
    plan, err = _config_plan.apply_answers(plan_id, payload.answers or {}, session_token)
    if err == "not_found":
        raise HTTPException(status_code=404, detail="Plan not found")
    if err == "forbidden":
        raise HTTPException(status_code=403, detail="Plan belongs to a different session.")
    if err == "locked":
        raise HTTPException(status_code=409, detail="Plan is executing or completed.")
    assert plan is not None
    return plan.to_dict()


@router.post("/config/prepare")
def config_prepare(
    payload: ConfigPrepareRequest,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Review a plan and mint an approval ticket. No execution."""
    session_token = _require_copilot_session(token)
    ticket, err = _config_exec.prepare_plan(session_token, payload.plan_id.strip())
    if err == "not_found":
        raise HTTPException(status_code=404, detail="Plan not found")
    if err == "forbidden":
        raise HTTPException(status_code=403, detail="Plan belongs to a different session.")
    if err == "locked":
        raise HTTPException(status_code=409, detail="Plan is executing or completed.")
    if err.startswith("blocked:"):
        raise HTTPException(status_code=422, detail=err[len("blocked:"):])
    assert ticket is not None
    return {"approval_required": True, "ticket": ticket}


@router.post("/config/approve")
def config_approve(
    payload: ConfigApproveRequest,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Record explicit operator approval; returns a single-use plan token."""
    session_token = _require_copilot_session(token)
    rec, err = _config_exec.approve_plan(session_token, payload.ticket_id.strip())
    if err == "forbidden":
        raise HTTPException(status_code=403, detail="Plan belongs to a different session.")
    if err == "invalid_ticket":
        raise HTTPException(status_code=403, detail="Invalid, expired, or foreign approval ticket.")
    if err == "not_found":
        raise HTTPException(status_code=404, detail="Plan not found")
    if err == "plan_modified":
        raise HTTPException(status_code=409, detail="Plan was modified after review. Re-review and re-approve.")
    if err.startswith("blocked:"):
        raise HTTPException(status_code=422, detail=err[len("blocked:"):])
    assert rec is not None
    return rec


@router.post("/config/execute")
async def config_execute(
    payload: ConfigExecuteRequest,
    token: Optional[str] = Query(None),
):
    """Execute an approved plan step-by-step (SSE progress stream)."""
    session_token = _require_copilot_session(token)

    async def sse_generator():
        try:
            async for event in _config_exec.execute_plan(
                    session_token, payload.plan_id.strip(),
                    payload.approval_token, timeout=payload.timeout):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as ex:
            log.error("config execute stream error: %s", ex)
            yield f"data: {json.dumps({'type': 'error', 'message': str(ex)[:200]})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


@router.post("/config/baseline")
async def config_baseline(
    payload: ConfigBaselineRequest,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Collect fresh evidence and render the baseline health verdict (read-only)."""
    session_token = _require_copilot_session(token)
    lab_id = (payload.lab_id or "").strip()
    device_ids = list(payload.device_ids or [])
    if payload.plan_id:
        plan = _config_plan.get_plan(payload.plan_id.strip())
        if plan is None:
            raise HTTPException(status_code=404, detail="Plan not found")
        if plan.owner_token != session_token:
            raise HTTPException(status_code=403, detail="Plan belongs to a different session.")
        lab_id = lab_id or plan.lab_id
        if not device_ids:
            device_ids = [d.device_id for d in plan.devices]
    if not lab_id or not device_ids:
        raise HTTPException(status_code=422, detail="lab_id and device_ids (or plan_id) are required")
    record, err = await _baseline.collect_baseline(
        session_token, lab_id, device_ids,
        plan_id=(payload.plan_id or "").strip(),
        stp_vlans=payload.stp_vlans)
    if err == "unauthorized":
        raise HTTPException(status_code=401, detail="Authentication required")
    if err == "no_devices":
        raise HTTPException(status_code=404, detail="No owned devices matched.")
    return record


@router.get("/config/baseline/{baseline_id}")
def config_baseline_get(
    baseline_id: str,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    session_token = _require_copilot_session(token)
    record = _baseline.get_baseline(baseline_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Baseline not found")
    owner = _baseline.get_baseline_owner(baseline_id)
    if owner is not None and owner != session_token:
        raise HTTPException(status_code=403, detail="Baseline belongs to a different session.")
    return record
