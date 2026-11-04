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

def test_fastapi_copilot_routes(monkeypatch):
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from cliq.web.server import SESSIONS, app

    monkeypatch.setitem(SESSIONS, "tok-T", SimpleNamespace(host="cml", auth=object()))
    client = TestClient(app)
    Q = "?token=tok-T"

    # 0. Unauthenticated conversation access is rejected.
    assert client.get("/api/copilot/conversations").status_code == 401
    assert client.post("/api/copilot/conversations", json={"title": "x"}).status_code == 401

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
        "/api/copilot/conversations" + Q,
        json={"title": "Test REST Conversation", "model": "Llama-3.3-70B-Instruct", "provider": "meta"},
    )
    assert res_create.status_code == 200
    conv_data = res_create.json()
    conv_id = conv_data["id"]
    assert conv_data["title"] == "Test REST Conversation"

    # 3. GET /api/copilot/conversations/{id}
    res_get = client.get(f"/api/copilot/conversations/{conv_id}" + Q)
    assert res_get.status_code == 200
    assert res_get.json()["id"] == conv_id

    # 4. PATCH /api/copilot/conversations/{id}
    res_rename = client.patch(
        f"/api/copilot/conversations/{conv_id}" + Q,
        json={"title": "Renamed Title"},
    )
    assert res_rename.status_code == 200
    assert res_rename.json()["title"] == "Renamed Title"

    # 5. POST /api/copilot/upload
    res_upload = client.post(
        "/api/copilot/upload" + Q,
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
            f"/api/copilot/conversations/{conv_id}/messages" + Q,
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
    res_del = client.delete(f"/api/copilot/conversations/{conv_id}" + Q)
    assert res_del.status_code == 200
    assert res_del.json()["success"] is True

    # 8. Confirm deleted
    res_get_deleted = client.get(f"/api/copilot/conversations/{conv_id}" + Q)
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

# -----------------------------------------------------------------------------
# 8. Silent-response regression (stream shapes, empty completions, RAG outage)
# -----------------------------------------------------------------------------

def test_parse_sse_data_line_shapes():
    from cliq.ai.providers.meta import parse_sse_data_line

    chunk = parse_sse_data_line(
        '{"choices": [{"delta": {"content": "hello"}, "finish_reason": null}],'
        ' "model": "muse-spark-1.3"}',
        "muse-spark-1.3",
    )
    assert chunk is not None and chunk.delta == "hello"
    assert chunk.error is None and chunk.model == "muse-spark-1.3"

    # Non-streaming message shape is accepted, not dropped.
    chunk = parse_sse_data_line(
        '{"choices": [{"message": {"role": "assistant", "content": "full answer"}}]}',
        "muse-spark-1.3",
    )
    assert chunk is not None and chunk.delta == "full answer"

    # In-stream API errors surface (never swallowed).
    chunk = parse_sse_data_line(
        '{"error": {"message": "credits exhausted", "code": 402}}',
        "muse-spark-1.3",
    )
    assert chunk is not None and chunk.error is not None
    assert "credits exhausted" in chunk.error
    chunk = parse_sse_data_line('{"error": "boom"}', "muse-spark-1.3")
    assert chunk is not None and "boom" in (chunk.error or "")

    # [DONE] marker.
    chunk = parse_sse_data_line("[DONE]", "muse-spark-1.3")
    assert chunk is not None and chunk.is_finished and not chunk.delta

    # Skippable frames.
    assert parse_sse_data_line('{"choices": []}', "m") is None
    assert parse_sse_data_line("not json", "m") is None
    assert parse_sse_data_line("[1, 2]", "m") is None
    assert parse_sse_data_line("", "m") is None

    # Usage-only frame is kept (telemetry without content).
    chunk = parse_sse_data_line(
        '{"choices": [{"delta": {}, "finish_reason": null}],'
        ' "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}}',
        "m",
    )
    assert chunk is not None and chunk.usage is not None
    assert chunk.usage.total_tokens == 8 and chunk.delta == ""


def test_interpret_non_stream_body():
    from cliq.ai.providers.meta import interpret_non_stream_body

    chunk = interpret_non_stream_body(
        '{"choices": [{"message": {"role": "assistant", "content": "hi"}}]}',
        200,
        "muse-spark-1.3",
    )
    assert chunk.delta == "hi" and chunk.error is None

    chunk = interpret_non_stream_body(
        '{"error": {"message": "bad key"}}', 200, "m")
    assert chunk.error is not None and "bad key" in chunk.error

    chunk = interpret_non_stream_body("<html>proxy page</html>", 200, "m")
    assert chunk.error is not None and "non-streaming" in chunk.error

    chunk = interpret_non_stream_body("", 200, "m")
    assert chunk.error is not None


@pytest.mark.anyio
async def test_orchestrator_empty_completion_is_visible_error():
    """Zero chunks + zero errors must fail visibly: an error event, no
    silent done, and no empty assistant message persisted."""
    import tempfile

    from cliq.ai.orchestrator import CopilotOrchestrator

    class _SilentProvider(MetaProvider):
        def __init__(self):
            super().__init__(api_key=None, mock_mode=True)

        async def chat_stream(self, messages, model=None, **kwargs):
            yield StreamChunk(delta="", is_finished=True,
                              finish_reason="stop", model=model)

    with tempfile.TemporaryDirectory() as tmpdir:
        store = ConversationStore(
            db_path=os.path.join(tmpdir, "empty_test.sqlite"))
        conv = store.create_conversation("Empty Case")
        with patch("cliq.ai.orchestrator.get_provider",
                   return_value=_SilentProvider()):
            orchestrator = CopilotOrchestrator(store=store)
            events = [ev async for ev in orchestrator.handle_user_message_stream(
                conversation_id=conv.id,
                user_text="Hello?",
                model="muse-spark-1.3",
                provider_id="meta",
            )]
        kinds = [e["type"] for e in events]
        assert "error" in kinds
        assert "done" not in kinds
        errors = [e for e in events if e["type"] == "error"]
        assert "empty response" in errors[0]["message"].lower()
        msgs = store.get_messages(conv.id)
        assert [m.role for m in msgs] == ["user"]


@pytest.mark.anyio
async def test_orchestrator_surfaces_provider_error():
    """A provider error chunk becomes a visible error event; the failed
    turn persists only the user message."""
    import tempfile

    from cliq.ai.orchestrator import CopilotOrchestrator

    class _FailingProvider(MetaProvider):
        def __init__(self):
            super().__init__(api_key=None, mock_mode=True)

        async def chat_stream(self, messages, model=None, **kwargs):
            yield StreamChunk(error="credits exhausted")

    with tempfile.TemporaryDirectory() as tmpdir:
        store = ConversationStore(
            db_path=os.path.join(tmpdir, "error_test.sqlite"))
        conv = store.create_conversation("Error Case")
        with patch("cliq.ai.orchestrator.get_provider",
                   return_value=_FailingProvider()):
            orchestrator = CopilotOrchestrator(store=store)
            events = [ev async for ev in orchestrator.handle_user_message_stream(
                conversation_id=conv.id,
                user_text="Hello?",
                model="muse-spark-1.3",
                provider_id="meta",
            )]
        errors = [e for e in events if e["type"] == "error"]
        assert errors and "credits exhausted" in errors[0]["message"]
        assert "done" not in [e["type"] for e in events]
        msgs = store.get_messages(conv.id)
        assert [m.role for m in msgs] == ["user"]


@pytest.mark.anyio
async def test_orchestrator_survives_rag_init_failure():
    """Broken/missing embeddings degrade RAG only: chunks still flow and
    the assistant reply is persisted."""
    import tempfile

    from cliq.ai.orchestrator import CopilotOrchestrator

    mock_prov = MetaProvider(api_key=None, mock_mode=True)
    retriever_factory = MagicMock(
        side_effect=RuntimeError(
            "SentenceTransformerEmbedder requires 'sentence-transformers'"))
    with tempfile.TemporaryDirectory() as tmpdir:
        store = ConversationStore(
            db_path=os.path.join(tmpdir, "rag_test.sqlite"))
        conv = store.create_conversation("RAG Outage")
        # NOTE: the orchestrator imports get_shared_retriever lazily from
        # cliq.retrieval.query on every get_retriever() call, so the
        # definition site is the correct patch target.
        with patch("cliq.ai.orchestrator.get_provider", return_value=mock_prov), \
                patch("cliq.retrieval.query.get_shared_retriever",
                      retriever_factory):
            orchestrator = CopilotOrchestrator(store=store)
            events = [ev async for ev in orchestrator.handle_user_message_stream(
                conversation_id=conv.id,
                user_text="Explain show ip interface brief",
                model="muse-spark-1.3",
                provider_id="meta",
            )]
        assert retriever_factory.called, "RAG path was never attempted"
        chunks = [e for e in events if e["type"] == "chunk"]
        assert chunks, "no chunks flowed despite RAG outage"
        assert "".join(c["delta"] for c in chunks).strip() != ""
        assert events[-1]["type"] == "done"
        msgs = store.get_messages(conv.id)
        assert [m.role for m in msgs] == ["user", "assistant"]
        assert msgs[1].content.strip() != ""


@pytest.mark.anyio
async def test_meta_worker_surfaces_instream_error():
    """Thread/queue wiring with canned SSE (no real HTTP): partial content
    flows, then the in-stream API error surfaces as an error chunk."""
    from cliq.ai.providers import meta as meta_mod

    class _CannedResp:
        status_code = 200
        ok = True
        headers = {"content-type": "text/event-stream"}
        lines = [
            b'data: {"choices": [{"delta": {"content": "partial "}}]}',
            b'data: {"error": {"message": "credits exhausted"}}',
        ]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_lines(self):
            return iter(self.lines)

    prov = MetaProvider(api_key="TEST-KEY", mock_mode=False)
    with patch.object(meta_mod.requests, "post", return_value=_CannedResp()):
        chunks = [c async for c in prov.chat_stream(
            [ChatMessage(role=ChatRole.USER, content="hi")],
            model="muse-spark-1.3",
        )]
    assert chunks[0].delta == "partial " and chunks[0].error is None
    assert chunks[-1].error is not None
    assert "credits exhausted" in chunks[-1].error


@pytest.mark.anyio
async def test_meta_worker_parses_non_stream_body():
    """A 200 non-SSE JSON body carrying a full answer becomes content."""
    from cliq.ai.providers import meta as meta_mod

    class _CannedResp:
        status_code = 200
        ok = True
        headers = {"content-type": "application/json"}
        text = '{"choices": [{"message": {"role": "assistant", "content": "whole answer"}}]}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_lines(self):
            raise AssertionError("non-SSE body must not be line-scanned")

    prov = MetaProvider(api_key="TEST-KEY", mock_mode=False)
    with patch.object(meta_mod.requests, "post", return_value=_CannedResp()):
        chunks = [c async for c in prov.chat_stream(
            [ChatMessage(role=ChatRole.USER, content="hi")],
            model="muse-spark-1.3",
        )]
    assert len(chunks) == 1 and chunks[0].delta == "whole answer"
