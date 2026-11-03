"""
test_phase_v3_rag_activation.py — Unit tests for CLIQ Phase V.3 RAG Activation.

Verifies:
1. Knowledge base and vector database loading and record counts.
2. Embedding dimension compatibility and mismatch protection.
3. Exact, semantic, and hybrid retrieval.
4. Relevance classification for retrieval gating (skip chit-chat, trigger on networking).
5. Copilot orchestrator RAG integration, prompt augmentation, and source citation preservation.
6. Graceful fallback when RAG is disabled or encounters an error.
"""

import os
import pytest
import sqlite3
from typing import AsyncGenerator, Dict, List, Optional
from unittest.mock import MagicMock, patch

from cliq.ingest.storage.embeddings import LocalHashingEmbedder, get_embedder
from cliq.ingest.storage.sqlite_store import CommandStore
from cliq.ingest.storage.vector_store import SQLiteVectorStore
from cliq.retrieval.query import CommandRetriever, get_shared_retriever
from cliq.retrieval.schema import RetrievalQuery, RetrievalResult, RetrievalCandidate
from cliq.ai.orchestrator import CopilotOrchestrator
from cliq.ai.provider import AIProvider, ChatMessage, ModelInfo, StreamChunk, ChatResponse
from cliq.ai.conversations import ConversationStore
from cliq.validation.validator import SafetyValidator
from cliq.validation.schema import ValidationContext, VerdictType
from cliq.retrieval.query import validate_citation
from cliq.utils.observability import redact



# Paths to staging databases
KB_PATH = os.path.abspath("kb/cliq_kb.sqlite")
VEC_PATH = os.path.abspath("kb/cliq_vec.sqlite")


class MockStreamingProvider(AIProvider):
    """Mock provider for testing prompt augmentation without external API calls."""

    @property
    def provider_id(self) -> str:
        return "mock"

    @property
    def display_name(self) -> str:
        return "Mock Provider"

    def __init__(self):
        self.received_messages: List[ChatMessage] = []

    def list_models(self) -> List[ModelInfo]:
        return [ModelInfo(id="mock-model", name="Mock Model", provider="mock", description="Mock Model")]

    def get_default_model(self) -> str:
        return "mock-model"

    def is_configured(self) -> bool:
        return True

    async def chat_stream(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_seconds: float = 60.0,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.received_messages = list(messages)
        yield StreamChunk(delta="Mock response grounded in: ")
        yield StreamChunk(delta="Cisco IOS-XE documentation.")

    async def chat(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout_seconds: float = 60.0,
    ) -> ChatResponse:
        self.received_messages = list(messages)
        return ChatResponse(content="Mock response", model="mock-model", provider="mock")


@pytest.fixture
def retriever():
    """Shared CommandRetriever fixture connected to staging KB."""
    if not os.path.exists(KB_PATH) or not os.path.exists(VEC_PATH):
        pytest.skip("Staging knowledge base files not present.")
    return get_shared_retriever(KB_PATH, VEC_PATH)


class TestRAGDatabaseIntegrity:
    """1. Database recovery and integrity verification."""

    def test_database_files_exist(self):
        assert os.path.exists(KB_PATH), f"KB database missing at {KB_PATH}"
        assert os.path.exists(VEC_PATH), f"Vector database missing at {VEC_PATH}"

    def test_record_and_vector_counts(self):
        kb_conn = sqlite3.connect(KB_PATH)
        cmd_count = kb_conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
        kb_conn.close()

        vec_conn = sqlite3.connect(VEC_PATH)
        vec_count = vec_conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        vec_dim = vec_conn.execute("SELECT dim FROM vectors LIMIT 1").fetchone()[0]
        vec_conn.close()

        assert cmd_count == 4877, f"Expected 4877 command records, got {cmd_count}"
        assert vec_count == 4877, f"Expected 4877 vectors, got {vec_count}"
        assert vec_dim == 384, f"Expected vector dimension 384, got {vec_dim}"

    def test_database_sqlite_integrity(self):
        for path in (KB_PATH, VEC_PATH):
            conn = sqlite3.connect(path)
            res = conn.execute("PRAGMA integrity_check;").fetchall()
            conn.close()
            assert res == [("ok",)], f"Integrity check failed for {path}: {res}"


class TestEmbeddingCompatibility:
    """2. Embedding dimension & model compatibility checks."""

    def test_vector_store_dimension_check(self):
        vec_store = SQLiteVectorStore(VEC_PATH)
        assert vec_store.get_dimension() == 384

        # Querying with incompatible 256-dim vector must raise ValueError
        incompatible_vec = [0.1] * 256
        with pytest.raises(ValueError, match="Vector dimension mismatch"):
            vec_store.query(incompatible_vec)

    def test_retriever_dimension_and_model_validation(self):
        kb_store = CommandStore(KB_PATH)
        vec_store = SQLiteVectorStore(VEC_PATH)

        # 1. 256-dim hashing embedder must be rejected
        hashing_embedder_256 = LocalHashingEmbedder(dim=256)
        with pytest.raises(ValueError, match="Incompatible embedder"):
            CommandRetriever(kb_store, vec_store, embedder=hashing_embedder_256)

        # 2. 384-dim hashing embedder on neural index must be rejected
        hashing_embedder_384 = LocalHashingEmbedder(dim=384)
        with pytest.raises(ValueError, match="Cannot query neural 384-dimensional vector index"):
            CommandRetriever(kb_store, vec_store, embedder=hashing_embedder_384)


class TestRetrievalQueries:
    """3. Exact, semantic, and hybrid retrieval tests."""

    def test_exact_command_retrieval(self, retriever):
        q = RetrievalQuery(input_text="show ip interface brief", mode="exact", top_k=3)
        res = retriever.query(q)
        assert len(res.candidates) > 0
        top = res.candidates[0]
        assert top.command == "show ip interface brief"
        assert top.match_type == "exact"
        assert top.confidence >= 0.90
        assert len(top.doc_source) > 0

    def test_semantic_intent_retrieval(self, retriever):
        q = RetrievalQuery(
            input_text="what command displays the routing table",
            mode="intent",
            top_k=3,
            min_confidence=0.40,
        )
        res = retriever.query(q)
        assert len(res.candidates) > 0
        cmd_names = [c.command for c in res.candidates]
        assert any("show ip route" in c for c in cmd_names)

    def test_platform_filtering(self, retriever):
        q = RetrievalQuery(
            input_text="show interfaces status",
            mode="auto",
            platform="iosxe",
            top_k=5,
            min_confidence=0.40,
        )
        res = retriever.query(q)
        assert len(res.candidates) > 0
        for c in res.candidates:
            assert c.platform in ("iosxe", "")


class TestCopilotOrchestratorRAG:
    """4. Copilot orchestrator RAG integration, gating, and fallback."""

    def test_relevance_gating(self):
        # Greetings & chit-chat should NOT trigger retrieval
        assert not CopilotOrchestrator.should_retrieve_for_query("hello")
        assert not CopilotOrchestrator.should_retrieve_for_query("hi there!")
        assert not CopilotOrchestrator.should_retrieve_for_query("Good morning")
        assert not CopilotOrchestrator.should_retrieve_for_query("thanks a lot")
        assert not CopilotOrchestrator.should_retrieve_for_query("who are you?")

        # Substantive networking questions SHOULD trigger retrieval
        assert CopilotOrchestrator.should_retrieve_for_query("What does show ip interface brief do?")
        assert CopilotOrchestrator.should_retrieve_for_query("How do I troubleshoot an interface that is down?")
        assert CopilotOrchestrator.should_retrieve_for_query("Explain OSPF neighbor states.")
        assert CopilotOrchestrator.should_retrieve_for_query("What is the difference between access and trunk ports?")
        assert CopilotOrchestrator.should_retrieve_for_query("Which command displays the routing table?")

    def test_context_assembly_includes_retrieved_knowledge(self, retriever, tmp_path):
        conv_store = ConversationStore(db_path=str(tmp_path / "conv1.sqlite"))
        orchestrator = CopilotOrchestrator(store=conv_store, retriever=retriever)

        context_text = orchestrator._assemble_context_text(
            session_token="test-token",
            context_chips=[],
            attachments=[],
            user_text="What does show ip interface brief do?",
        )

        assert "--- AUTHORITATIVE NETWORK KNOWLEDGE (OFFICIAL DOCUMENTATION) ---" in context_text
        assert "show ip interface brief" in context_text
        assert "Source:" in context_text

    @pytest.mark.anyio
    async def test_retrieved_context_reaches_provider(self, retriever, tmp_path):
        conv_store = ConversationStore(db_path=str(tmp_path / "conv2.sqlite"))
        orchestrator = CopilotOrchestrator(store=conv_store, retriever=retriever)
        mock_provider = MockStreamingProvider()

        with patch("cliq.ai.orchestrator.get_provider", return_value=mock_provider):
            conv = conv_store.create_conversation("RAG Test")
            chunks = []
            async for chunk in orchestrator.handle_user_message_stream(
                conversation_id=conv.id,
                user_text="Explain show ip interface brief.",
                provider_id="mock",
            ):
                if chunk.get("type") == "chunk":
                    chunks.append(chunk.get("delta", ""))

            # Verify prompt received by mock provider
            assert len(mock_provider.received_messages) >= 2
            user_msg = mock_provider.received_messages[-1]
            assert "--- AUTHORITATIVE NETWORK KNOWLEDGE (OFFICIAL DOCUMENTATION) ---" in user_msg.content
            assert "show ip interface brief" in user_msg.content

    def test_rag_disabled_mode(self, retriever, monkeypatch, tmp_path):
        monkeypatch.setenv("CLIQ_RAG_ENABLED", "0")
        conv_store = ConversationStore(db_path=str(tmp_path / "conv3.sqlite"))
        orchestrator = CopilotOrchestrator(store=conv_store, retriever=retriever)

        context_text = orchestrator._assemble_context_text(
            session_token="test-token",
            context_chips=[],
            attachments=[],
            user_text="What does show ip interface brief do?",
        )

        assert "--- AUTHORITATIVE NETWORK KNOWLEDGE (OFFICIAL DOCUMENTATION) ---" not in context_text

    def test_retrieval_failure_does_not_crash_copilot(self, tmp_path):
        conv_store = ConversationStore(db_path=str(tmp_path / "conv4.sqlite"))
        failing_retriever = MagicMock()
        failing_retriever.query.side_effect = RuntimeError("Database locked or disk error")

        orchestrator = CopilotOrchestrator(store=conv_store, retriever=failing_retriever)

        # Context assembly should catch the exception and return cleanly
        context_text = orchestrator._assemble_context_text(
            session_token="test-token",
            context_chips=[],
            attachments=[],
            user_text="What does show ip interface brief do?",
        )
        assert isinstance(context_text, str)
        assert "--- AUTHORITATIVE NETWORK KNOWLEDGE (OFFICIAL DOCUMENTATION) ---" not in context_text


class TestRAGAcceptanceCorrections:
    """5. RAG Acceptance Corrections & Device-Aware Targeting Tests."""

    def test_cisco_router_receiving_switchport_suggestions(self):
        """Cisco router must reject switchport configuration commands and explain mismatch."""
        validator = SafetyValidator()
        ctx = ValidationContext(
            command="switchport mode access",
            device_platform="iosxe",
            vendor="cisco",
            device_role="router",
            device_id="R1",
            current_mode="config-if",
        )
        verdict = validator.validate(ctx)
        assert not verdict.valid
        assert verdict.verdict_type == VerdictType.WRONG_DEVICE
        assert "Switchport configuration commands" in verdict.reason
        assert "not supported on router" in verdict.reason

    def test_cisco_switch_receiving_valid_switchport_suggestions(self):
        """Cisco switch must accept valid switchport configuration commands."""
        validator = SafetyValidator()
        ctx = ValidationContext(
            command="switchport mode access",
            device_platform="iosvl2",
            vendor="cisco",
            device_role="switch",
            device_id="SW1",
            current_mode="config-if",
        )
        verdict = validator.validate(ctx)
        assert verdict.valid
        assert verdict.verdict_type == VerdictType.OK

    def test_juniper_device_receiving_cisco_only_syntax(self):
        """Juniper device must reject Cisco IOS syntax and explain vendor mismatch."""
        validator = SafetyValidator()
        ctx = ValidationContext(
            command="show ip interface brief",
            device_platform="junos",
            vendor="juniper",
            device_role="router",
            device_id="JUNIPER-1",
            current_mode="exec",
        )
        verdict = validator.validate(ctx)
        assert not verdict.valid
        assert verdict.verdict_type == VerdictType.WRONG_DEVICE
        assert "Cisco IOS-XE syntax and is incompatible with Juniper" in verdict.reason
        assert "show interfaces terse" in verdict.suggested_action

    def test_incorrect_cli_mode(self):
        """Interface configuration command executed in exec mode must be flagged WRONG_MODE."""
        validator = SafetyValidator()
        ctx = ValidationContext(
            command="switchport mode access",
            device_platform="iosvl2",
            vendor="cisco",
            device_role="switch",
            device_id="SW1",
            current_mode="exec",
        )
        verdict = validator.validate(ctx)
        assert not verdict.valid
        assert verdict.verdict_type == VerdictType.WRONG_MODE
        assert "interface configuration command" in verdict.reason

    def test_unknown_platform_or_missing_device_metadata(self):
        """Unknown or missing device metadata must be rejected with UNKNOWN_COMMAND."""
        validator = SafetyValidator()
        ctx = ValidationContext(
            command="show ip route",
            device_platform="unknown",
            current_mode="exec",
        )
        verdict = validator.validate(ctx)
        assert not verdict.valid
        assert verdict.verdict_type == VerdictType.UNKNOWN_COMMAND
        assert "missing/unknown" in verdict.reason

    def test_citation_source_mismatch(self):
        """Candidate citation must be validated against genuine sources and semantic alignment."""
        # 1. Valid citation
        valid_candidate = RetrievalCandidate(
            command="show ip interface brief",
            description="Display a summary of interface status and configuration.",
            platform="iosxe",
            cmd_mode="exec",
            doc_source="https://www.cisco.com/c/en/us/td/docs/switches/lan/catalyst9500/cr/interface.html",
        )
        ok, msg = validate_citation(valid_candidate)
        assert ok, f"Expected valid citation, got: {msg}"

        # 2. Fabricated domain
        fake_candidate = RetrievalCandidate(
            command="show ip interface brief",
            description="Display a summary of interface status and configuration.",
            platform="iosxe",
            cmd_mode="exec",
            doc_source="https://fabricated-untrusted-site.com/docs/cr.html",
        )
        ok, msg = validate_citation(fake_candidate)
        assert not ok
        assert "Fabricated or untrusted" in msg

        # 3. Source mismatch (OSPF command citing BGP reference)
        mismatch_candidate = RetrievalCandidate(
            command="router ospf 1",
            description="Configure OSPF routing process.",
            platform="iosxe",
            cmd_mode="config",
            doc_source="bgp_command_reference.pdf",
        )
        ok, msg = validate_citation(mismatch_candidate)
        assert not ok
        assert "Citation source mismatch" in msg

    def test_rag_unavailable_or_database_missing(self):
        """Retriever must handle missing databases gracefully without raising unhandled exceptions."""
        res = get_shared_retriever(kb_path="nonexistent_kb.sqlite", vec_path="nonexistent_vec.sqlite", force_reload=True)
        assert res is None

    def test_embedding_dimension_mismatch(self):
        """Querying 384-dim vector store with mismatched dimension must raise ValueError."""
        vec_store = SQLiteVectorStore(VEC_PATH)
        assert vec_store.get_dimension() == 384
        with pytest.raises(ValueError, match="Vector dimension mismatch"):
            vec_store.query([0.1] * 128)

    def test_sentence_transformer_single_load_and_caching(self):
        """SentenceTransformer weights and embedder instances must be cached and loaded only once."""
        e1 = get_embedder("sentence-transformers")
        e2 = get_embedder("sentence-transformers")
        assert e1 is e2, "get_embedder should return shared singleton instance"
        assert hasattr(e1, "_model")
        assert e1._model is e2._model, "Underlying SentenceTransformer model should be shared"

    def test_authentication_token_redaction(self):
        """Sensitive tokens in WebSocket URLs and application log strings must be scrubbed."""
        raw_url = "ws://127.0.0.1:8000/ws?token=secret-auth-token-12345&host=10.10.20.161"
        redacted_url = redact(raw_url)
        assert "secret-auth-token-12345" not in redacted_url
        assert "token=<redacted>" in redacted_url
        assert "host=10.10.20.161" in redacted_url
