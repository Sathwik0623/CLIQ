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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from cliq.ai.attachments import get_attachment_service
from cliq.ai.conversations import ConversationStore
from cliq.ai.network_context import get_network_context_service
from cliq.ai.orchestrator import get_copilot_orchestrator
from cliq.ai.registry import list_models, list_providers

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
    conv = store.create_conversation(
        title=payload.title,
        model=payload.model or "muse-spark-1.3",
        provider=payload.provider,
        session_token=token,
        metadata=payload.metadata,
    )
    return conv.to_dict(include_messages=True)


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: str,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Retrieve full conversation and message history."""
    conv = store.get_conversation(conversation_id, session_token=token)
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
    success = store.rename_conversation(conversation_id, payload.title, session_token=token)
    if not success:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"success": True, "title": payload.title.strip()}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Delete conversation and its associated message history."""
    success = store.delete_conversation(conversation_id, session_token=token)
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
    if not payload.content.strip():
        raise HTTPException(status_code=422, detail="Message content cannot be empty")

    async def sse_generator():
        try:
            async for event in orchestrator.handle_user_message_stream(
                conversation_id=conversation_id,
                user_text=payload.content,
                session_token=token,
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
def upload_attachment(payload: UploadAttachmentRequest) -> Dict[str, Any]:
    """Safely validate, sanitize, and store an uploaded configuration/log file."""
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
    devices = context_service.list_devices(session_token=token)
    return {"devices": devices}


@router.get("/context/query")
def query_device_context(
    device_id: str,
    context_type: str = Query("interfaces", pattern="^(interfaces|routes|config|neighbors|history|terminal_history|device)$"),
    token: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Extract read-only, secret-redacted state for a specific device context chip."""
    data = ""
    if context_type == "interfaces":
        data = context_service.get_interfaces(token or "", device_id)
    elif context_type == "routes":
        data = context_service.get_routing_table(token or "", device_id)
    elif context_type == "config":
        data = context_service.get_running_config(token or "", device_id)
    elif context_type == "neighbors":
        data = context_service.get_neighbors(token or "", device_id)
    elif context_type in ("history", "terminal_history"):
        hist = context_service.get_recent_commands(token or "", device_id)
        data = "\n".join(hist)
    elif context_type == "device":
        info = context_service.get_device_info(token or "", device_id)
        intfs = context_service.get_interfaces(token or "", device_id)
        data = f"Status: {info.get('status')}\nMode: {info.get('mode')}\n\nInterfaces:\n{intfs}"

    return {
        "device_id": device_id,
        "context_type": context_type,
        "data": data,
    }
