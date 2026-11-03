"""
Unit tests for CLIQ Phase V — AI Networking Copilot Foundation, Meta Integration and UI.

Verifies:
1. Meta AI provider adapter (SSE streaming, request formatting, mock fallback, token counting).
2. Provider Registry and model cataloging.
3. Persistent SQLite conversation store (CRUD, search, session isolation, cascade delete).
4. Attachment service (file extension safety, path traversal prevention, size validation, text extraction).
5. Network context service (secret/credential redaction, read-only commands).
6. Copilot Orchestrator (system prompt structure, context chip injection, attachments).
7. FastAPI Copilot REST & SSE endpoints.
"""

import os
import json
import tempfile
from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock

from cliq.ai.provider import (
    ChatMessage,
    ChatRole,
    ChatUsage,
    StreamChunk,
    ModelInfo,
)
from cliq.ai.providers.meta import MetaProvider
from cliq.ai.registry import ProviderRegistry
from cliq.ai.conversations import ConversationStore
from cliq.ai.attachments import AttachmentService
from cliq.ai.network_context import redact_config_secrets, NetworkContextService
from cliq.ai.orchestrator import CopilotOrchestrator


# -----------------------------------------------------------------------------
# 1. Meta Provider & Registry Tests
# -----------------------------------------------------------------------------

def test_meta_provider_init():
    prov = MetaProvider(api_key="test_key_abc", default_model="muse-spark-1.3")
    assert prov.provider_id == "meta"
    assert prov.is_configured() is True
    assert prov.get_default_model() == "muse-spark-1.3"

    models = prov.list_models()
    assert len(models) >= 3
    model_ids = [m.id for m in models]
    assert "muse-spark-1.3" in model_ids
    assert "muse-wonder-1.0" in model_ids
    assert "muse-spark-1.2" in model_ids
    assert "Llama-3.3-70B-Instruct" not in model_ids


def test_meta_provider_unconfigured():
    with patch.dict(os.environ, {"CLIQ_AI_MOCK": ""}, clear=True):
        prov = MetaProvider(api_key=None, mock_mode=False)
        assert prov.is_configured() is False


@pytest.mark.anyio
async def test_meta_provider_mock_stream():
    """Verify offline mock streaming without burning user API credits."""
    prov = MetaProvider(api_key=None, mock_mode=True)
    messages = [
        ChatMessage(role=ChatRole.USER, content="How do I verify BGP routes on R1?")
    ]
    chunks = []
    async for chunk in prov.chat_stream(messages, model="Llama-3.3-70B-Instruct"):
        chunks.append(chunk)

    assert len(chunks) > 0
    full_text = "".join(c.delta for c in chunks)
    assert len(full_text) > 20
    last_chunk = chunks[-1]
    assert last_chunk.finish_reason == "stop"
    assert last_chunk.usage is not None
    assert last_chunk.usage.total_tokens > 0


def test_provider_registry():
    registry = ProviderRegistry.get_instance()
    meta = registry.get_provider("meta")
    assert meta is not None
    assert meta.provider_id == "meta"

    default_prov = registry.get_provider()
    assert default_prov.provider_id == "meta"

    all_models = registry.list_all_models()
    assert any(m.id == "muse-spark-1.3" for m in all_models)
    assert not any(m.id == "Llama-3.3-70B-Instruct" for m in all_models)


# -----------------------------------------------------------------------------
# 2. SQLite Conversation Store Tests
# -----------------------------------------------------------------------------

def test_conversation_store_crud():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_copilot.sqlite")
        store = ConversationStore(db_path=db_path)

        # Create
        conv = store.create_conversation(
            title="Troubleshoot OSPF Area 0",
            model="Llama-3.3-70B-Instruct",
            provider="meta",
            session_token="token_user_1",
        )
        assert conv.id is not None
        assert conv.title == "Troubleshoot OSPF Area 0"

        # Read
        fetched = store.get_conversation(conv.id, session_token="token_user_1")
        assert fetched is not None
        assert fetched.id == conv.id

        # Add message
        msg_user = store.add_message(
            conversation_id=conv.id,
            role="user",
            content="Check why R1 cannot form neighbor with R2",
            metadata={"test": 123},
        )
        assert msg_user.id is not None

        msg_asst = store.add_message(
            conversation_id=conv.id,
            role="assistant",
            content="Check MTU mismatch and authentication key.",
            metadata={"token_usage": {"prompt_tokens": 10, "completion_tokens": 15, "total_tokens": 25}},
        )
        assert msg_asst.id is not None

        # Verify messages loaded
        conv_with_msgs = store.get_conversation(conv.id, session_token="token_user_1")
        assert len(conv_with_msgs.messages) == 2
        assert conv_with_msgs.messages[0].role == "user"
        assert conv_with_msgs.messages[1].role == "assistant"

        # Rename
        store.rename_conversation(conv.id, "OSPF Area 0 Resolved", session_token="token_user_1")
        renamed = store.get_conversation(conv.id, session_token="token_user_1")
        assert renamed.title == "OSPF Area 0 Resolved"

        # Search
        results = store.search_conversations("Resolved", session_token="token_user_1")
        assert len(results) == 1
        assert results[0].id == conv.id

        no_results = store.search_conversations("NonExistentPrefix", session_token="token_user_1")
        assert len(no_results) == 0

        # Session isolation test: User 2 should NOT see User 1's conversation
        assert store.get_conversation(conv.id, session_token="token_user_2") is None

        # Delete
        store.delete_conversation(conv.id, session_token="token_user_1")
        assert store.get_conversation(conv.id, session_token="token_user_1") is None
        msgs_after_del = store.get_messages(conv.id)
        assert len(msgs_after_del) == 0


# -----------------------------------------------------------------------------
# 3. Attachment Service Tests
# -----------------------------------------------------------------------------

def test_attachment_service():
    with tempfile.TemporaryDirectory() as tmpdir:
        svc = AttachmentService(base_dir=Path(tmpdir))

        # 1. Allowed file: config
        att = svc.save_attachment(
            conversation_id="conv_100",
            filename="r1_running_config.cfg",
            content_bytes=b"hostname R1\ninterface GigabitEthernet0/0\n ip address 10.0.0.1 255.255.255.0\n",
        )
        assert att.id is not None
        assert att.filename == "r1_running_config.cfg"
        assert att.extension == ".cfg"
        assert att.line_count >= 1
        assert "hostname R1" in att.text_content

        # 2. Path traversal attack protection
        att_traversal = svc.save_attachment(
            conversation_id="conv_100",
            filename="../../etc/passwd.txt",
            content_bytes=b"fake root entry\n",
        )
        assert ".." not in att_traversal.filename
        assert att_traversal.filename.endswith("passwd.txt")

        # 3. Disallowed extension
        with pytest.raises(ValueError, match="Unsupported file type"):
            svc.save_attachment(
                conversation_id="conv_100",
                filename="malicious_payload.exe",
                content_bytes=b"MZ...",
            )

        # 4. Oversized content rejection (>10MB)
        huge_bytes = b"X" * (11 * 1024 * 1024)
        with pytest.raises(ValueError, match="exceeds maximum size"):
            svc.save_attachment(
                conversation_id="conv_100",
                filename="huge_log.log",
                content_bytes=huge_bytes,
            )


# -----------------------------------------------------------------------------
# 4. Network Context & Secret Redaction Tests
# -----------------------------------------------------------------------------

def test_redact_config_secrets():
    raw_config = """
    hostname Core-R1
    enable secret 5 $1$mERr$hx5rVt7rPNoS4wqbXKX7m0
    enable password cisco_super_secret
    username admin secret 9 $9$mERr$hx5rVt7rPNoS4wqbXKX7m0
    username operator password plain_operator_pass
    snmp-server community private RW
    snmp-server community public RO
    interface GigabitEthernet0/0
     ip address 10.10.10.1 255.255.255.0
    """

    redacted = redact_config_secrets(raw_config)

    # Confirm secrets are removed
    assert "hx5rVt7rPNoS4wqbXKX7m0" not in redacted
    assert "cisco_super_secret" not in redacted
    assert "plain_operator_pass" not in redacted

    # Confirm network structure is preserved
    assert "hostname Core-R1" in redacted
    assert "GigabitEthernet0/0" in redacted
    assert "10.10.10.1 255.255.255.0" in redacted
    assert "REDACTED" in redacted


# -----------------------------------------------------------------------------
# 5. Copilot Orchestrator Tests
# -----------------------------------------------------------------------------

@pytest.mark.anyio
async def test_copilot_orchestrator():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "orchestrator_test.sqlite")
        store = ConversationStore(db_path=db_path)

        mock_prov = MetaProvider(api_key=None, mock_mode=True)

        with patch("cliq.ai.orchestrator.get_provider", return_value=mock_prov):
            orchestrator = CopilotOrchestrator(store=store)

            conv = store.create_conversation("BGP Debug Session")

            context_chips = [
                {
                    "id": "chip_1",
                    "type": "interfaces",
                    "label": "@R1 Interfaces",
                    "deviceId": "r1_node",
                }
            ]

            events = []
            async for ev in orchestrator.handle_user_message_stream(
                conversation_id=conv.id,
                user_text="Why is interface Gi0/1 down?",
                model="Llama-3.3-70B-Instruct",
                provider_id="meta",
                context_chips=context_chips,
            ):
                events.append(ev)

            assert len(events) > 0
            conv_msgs = store.get_messages(conv.id)
            assert len(conv_msgs) == 2
            assert conv_msgs[0].role == "user"
            assert conv_msgs[1].role == "assistant"


# -----------------------------------------------------------------------------
# 6. FastAPI Copilot Router Endpoints
# -----------------------------------------------------------------------------

def test_fastapi_copilot_routes():
    from fastapi.testclient import TestClient
    from cliq.web.server import app

    client = TestClient(app)

    # 1. GET /api/copilot/providers
    res = client.get("/api/copilot/providers")
    assert res.status_code == 200
    data = res.json()
    assert "providers" in data
    assert "models" in data
    assert any(m["id"] == "muse-spark-1.3" for m in data["models"])
    assert not any(m["id"] == "Llama-3.3-70B-Instruct" for m in data["models"])

    # 2. POST /api/copilot/conversations
    res_create = client.post(
        "/api/copilot/conversations",
        json={"title": "Test REST Conversation", "model": "Llama-3.3-70B-Instruct", "provider": "meta"},
    )
    assert res_create.status_code == 200
    conv_data = res_create.json()
    conv_id = conv_data["id"]
    assert conv_data["title"] == "Test REST Conversation"

    # 3. GET /api/copilot/conversations/{id}
    res_get = client.get(f"/api/copilot/conversations/{conv_id}")
    assert res_get.status_code == 200
    assert res_get.json()["id"] == conv_id

    # 4. PATCH /api/copilot/conversations/{id}
    res_rename = client.patch(
        f"/api/copilot/conversations/{conv_id}",
        json={"title": "Renamed Title"},
    )
    assert res_rename.status_code == 200
    assert res_rename.json()["title"] == "Renamed Title"

    # 5. POST /api/copilot/upload
    res_upload = client.post(
        "/api/copilot/upload",
        json={
            "conversation_id": conv_id,
            "filename": "ospf_debug.log",
            "content": "OSPF-100: Received Hello packet on Gi0/0\n",
        },
    )
    assert res_upload.status_code == 200
    att_data = res_upload.json()
    assert att_data["success"] is True
    assert att_data["attachment"]["filename"] == "ospf_debug.log"
    assert att_data["attachment"]["line_count"] >= 1

    # 6. POST /api/copilot/conversations/{id}/messages (SSE stream in mock mode)
    with patch.dict(os.environ, {"CLIQ_AI_MOCK": "1"}):
        res_stream = client.post(
            f"/api/copilot/conversations/{conv_id}/messages",
            json={
                "content": "Explain OSPF hello dead intervals",
                "model": "Llama-3.3-70B-Instruct",
                "provider": "meta",
            },
        )
        assert res_stream.status_code == 200
        assert "text/event-stream" in res_stream.headers["content-type"]
        body = res_stream.text
        assert "data:" in body
        assert "[DONE]" in body

    # 7. DELETE /api/copilot/conversations/{id}
    res_del = client.delete(f"/api/copilot/conversations/{conv_id}")
    assert res_del.status_code == 200
    assert res_del.json()["success"] is True

    # 8. Confirm deleted
    res_get_deleted = client.get(f"/api/copilot/conversations/{conv_id}")
    assert res_get_deleted.status_code == 404


@pytest.mark.anyio
async def test_copilot_safe_model_fallback():
    """Verify that orchestrator safely falls back on availability errors (404/503) without clutter."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "fallback_test.sqlite")
        store = ConversationStore(db_path=db_path)

        class MockFailingProvider(MetaProvider):
            def __init__(self):
                super().__init__(api_key=None, mock_mode=True)
                self.call_count = 0

            async def chat_stream(self, messages, model=None, **kwargs):
                self.call_count += 1
                if model == "unavailable-model-404":
                    yield StreamChunk(error="Meta API error (HTTP 404): model_not_found")
                    return
                # Successful fallback stream
                yield StreamChunk(delta="Hello from fallback model")
                yield StreamChunk(is_finished=True, finish_reason="stop", usage=ChatUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10))

        mock_prov = MockFailingProvider()
        with patch("cliq.ai.orchestrator.get_provider", return_value=mock_prov):
            orchestrator = CopilotOrchestrator(store=store)
            conv = store.create_conversation("Fallback Session")

            events = []
            async for ev in orchestrator.handle_user_message_stream(
                conversation_id=conv.id,
                user_text="Test prompt",
                model="unavailable-model-404",
            ):
                events.append(ev)

            # Confirm fallback notice chunk and answer chunk were delivered
            assert any(ev.get("type") == "chunk" and "was unavailable. Switched to" in ev.get("delta", "") for ev in events)
            assert any(ev.get("type") == "chunk" and "Hello from fallback model" in ev.get("delta", "") for ev in events)
            done_ev = next(ev for ev in events if ev.get("type") == "done")
            assert done_ev["model"] != "unavailable-model-404"

            # Confirm conversation history has only 2 messages (1 user, 1 assistant) - no duplicate clutter
            msgs = store.get_messages(conv.id)
            assert len(msgs) == 2
            assert msgs[1].metadata.get("fallback_from") == "unavailable-model-404"


def test_orchestrator_multi_command_code_block_rules():
    from cliq.ai.orchestrator import SYSTEM_PROMPT

    # 1. Multi-command sequential block rule is present
    assert "Multi-Command Sequential Code Blocks & Platform Separation" in SYSTEM_PROMPT
    assert "generate ONE coherent fenced code block containing all related commands" in SYSTEM_PROMPT

    # 2. Obsolete rule is removed
    assert "Individual Command Presentation (No Monolithic Code Blocks)" not in SYSTEM_PROMPT

    # 3. Platform separation mandatory
    assert "```cisco" in SYSTEM_PROMPT
    assert "```bash" in SYSTEM_PROMPT
    assert "NEVER mix Cisco and Linux commands in the same code block" in SYSTEM_PROMPT


def test_base_ai_provider_and_gemini_integration():
    from cliq.ai.provider import BaseAIProvider, AIProvider
    from cliq.ai.providers.gemini import GeminiProvider
    from cliq.ai.registry import ProviderRegistry

    # 1. BaseAIProvider alias is identical to AIProvider
    assert BaseAIProvider is AIProvider

    # 2. GeminiProvider inherits from BaseAIProvider
    assert issubclass(GeminiProvider, BaseAIProvider)

    # 3. ProviderRegistry lists Meta as active default and Gemini as future provider
    reg = ProviderRegistry.get_instance()
    providers = reg.list_providers()
    prov_ids = [p["id"] for p in providers]

    assert "meta" in prov_ids
    assert "gemini" in prov_ids

    meta_entry = next(p for p in providers if p["id"] == "meta")
    assert meta_entry["is_default"] is True

    gemini_entry = next(p for p in providers if p["id"] == "gemini")
    assert gemini_entry["is_default"] is False
