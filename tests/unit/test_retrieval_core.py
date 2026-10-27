#!/usr/bin/env python3
"""Unit tests for retrieval layer (intent classifier, ranker, schema)."""

import pytest

from cliq.retrieval.assistant import IntelligentCommandAssistant
from cliq.retrieval.intent_classifier import classify_input
from cliq.retrieval.ranker import apply_filters, deduplicate_and_rank
from cliq.retrieval.schema import (
    IntelligentRetrievalConfig,
    RetrievalCandidate,
    RetrievalQuery,
    RetrievalResult,
)


class TestIntentClassifier:
    """Test intent vs. exact mode detection."""

    def test_exact_known_verbs(self):
        """Known CLI verbs classify as 'exact'."""
        assert classify_input("show ip route") == "exact"
        assert classify_input("display interface") == "exact"
        assert classify_input("configure terminal") == "exact"
        assert classify_input("no ip ospf") == "exact"

    def test_exact_short_tokens(self):
        """Short alphabetic tokens classify as 'exact'."""
        assert classify_input("show") == "exact"
        assert classify_input("end") == "exact"
        assert classify_input("exit") == "exact"

    def test_intent_natural_language(self):
        """Natural language queries classify as 'intent'."""
        assert classify_input("how to enable multicast") == "intent"
        assert classify_input("what is the routing table") == "intent"
        assert classify_input("can you show me the interfaces") == "intent"

    def test_intent_question_mark(self):
        """Input with question marks classify as 'intent'."""
        assert classify_input("how do i configure ospf?") == "intent"
        assert classify_input("what commands are available?") == "intent"

    def test_exact_structured_input(self):
        """Structured input with brackets/hyphens classify as 'exact'."""
        assert classify_input("access-list 1 permit 192.168.1.0") == "exact"
        assert classify_input("router ospf [process-id]") == "exact"

    def test_ambiguous_default_intent(self):
        """Ambiguous input defaults to 'intent'."""
        assert classify_input("multicast routing") == "intent"
        assert classify_input("ospf neighbors") == "intent"


class TestRanker:
    """Test hybrid ranking and deduplication."""

    def test_dedup_exact_over_semantic(self):
        """Exact matches are preferred over semantic ones with same ID."""
        exact = RetrievalCandidate(
            command="show ip pim",
            description="Show PIM info",
            sqlite_id=1,
            match_type="exact",
            confidence=0.9,
        )
        semantic = RetrievalCandidate(
            command="show ip pim",
            description="Show PIM info",
            sqlite_id=1,
            match_type="semantic",
            confidence=0.85,
        )
        result = deduplicate_and_rank([exact], [semantic], boost_exact=False)
        assert len(result) == 1
        assert result[0].match_type == "exact"

    def test_dedup_different_ids(self):
        """Different IDs are not deduplicated."""
        cand1 = RetrievalCandidate(
            command="show ip pim",
            description="",
            sqlite_id=1,
            match_type="exact",
            confidence=0.9,
        )
        cand2 = RetrievalCandidate(
            command="show ip pim neighbors",
            description="",
            sqlite_id=2,
            match_type="semantic",
            confidence=0.8,
        )
        result = deduplicate_and_rank([cand1], [cand2], boost_exact=False)
        assert len(result) == 2

    def test_boost_exact_confidence(self):
        """Exact matches are boosted to >= 0.9 confidence."""
        exact = RetrievalCandidate(
            command="show ip",
            description="",
            match_type="exact",
            confidence=0.5,
        )
        semantic = RetrievalCandidate(
            command="display routing",
            description="",
            match_type="semantic",
            confidence=0.7,
        )
        result = deduplicate_and_rank([exact], [semantic], boost_exact=True)
        # Exact should be boosted and sorted first.
        assert result[0].match_type == "exact"
        assert result[0].confidence >= 0.9

    def test_sort_by_confidence_then_match_type(self):
        """Results sorted by confidence desc, then match type, then command name."""
        candidates = [
            RetrievalCandidate(
                command="beta", description="", match_type="semantic", confidence=0.8
            ),
            RetrievalCandidate(
                command="alpha", description="", match_type="exact", confidence=0.7
            ),
            RetrievalCandidate(
                command="charlie", description="", match_type="prefix", confidence=0.9
            ),
        ]
        result = deduplicate_and_rank([], candidates, boost_exact=False)
        assert result[0].command == "charlie"  # 0.9 confidence
        assert result[1].command == "beta"  # 0.8 confidence
        assert result[2].command == "alpha"  # 0.7 confidence

    def test_filter_by_platform(self):
        """Filter candidates by platform."""
        candidates = [
            RetrievalCandidate(
                command="show pim", description="", platform="iosxe", confidence=0.9
            ),
            RetrievalCandidate(
                command="show pim", description="", platform="iosxr", confidence=0.85
            ),
        ]
        filtered, reason = apply_filters(candidates, platform="iosxe", cmd_mode=None)
        assert len(filtered) == 1
        assert filtered[0].platform == "iosxe"
        assert reason is None

    def test_filter_by_mode(self):
        """Filter candidates by command mode."""
        candidates = [
            RetrievalCandidate(
                command="show pim", description="", cmd_mode="exec", confidence=0.9
            ),
            RetrievalCandidate(
                command="pim sparse-mode", description="", cmd_mode="config-if", confidence=0.85
            ),
        ]
        filtered, reason = apply_filters(candidates, platform=None, cmd_mode="exec")
        assert len(filtered) == 1
        assert filtered[0].cmd_mode == "exec"

    def test_filter_no_matches(self):
        """Return empty list with reason when no candidates match filters."""
        candidates = [
            RetrievalCandidate(
                command="show pim", description="", platform="iosxe", cmd_mode="exec"
            ),
        ]
        filtered, reason = apply_filters(candidates, platform="iosxr", cmd_mode=None)
        assert len(filtered) == 0
        assert "platform_mismatch" in reason

    def test_dedup_ranks_trusted_above_candidate_on_confidence_tie(self):
        """product_cr 'candidate' rows rank below trusted/validated KB rows on a tie."""
        trusted = RetrievalCandidate(
            command="qos share-buffer", description="", sqlite_id=1,
            match_type="prefix", confidence=0.9, source="kb", candidate_status="",
        )
        candidate = RetrievalCandidate(
            command="qos queue-limit", description="", sqlite_id=2,
            match_type="prefix", confidence=0.9, source="product_cr",
            candidate_status="candidate",
        )
        result = deduplicate_and_rank([trusted, candidate], [], boost_exact=False)
        assert [c.command for c in result] == ["qos share-buffer", "qos queue-limit"]
        assert result[0].candidate_status == ""
        assert result[1].candidate_status == "candidate"

    def test_dedup_does_not_exclude_candidates(self):
        """Candidates must never be silently dropped by ranking/dedup."""
        candidate = RetrievalCandidate(
            command="auto qos classify", description="", sqlite_id=1,
            match_type="semantic", confidence=0.55, source="product_cr",
            candidate_status="candidate",
        )
        result = deduplicate_and_rank([], [candidate], boost_exact=False)
        assert len(result) == 1
        assert result[0].command == "auto qos classify"

    def test_higher_confidence_candidate_still_outranks_lower_confidence_trusted(self):
        """A more relevant candidate can still outrank a barely-relevant trusted row."""
        trusted_low = RetrievalCandidate(
            command="show tech-support qos", description="", sqlite_id=1,
            match_type="semantic", confidence=0.4, source="kb", candidate_status="",
        )
        candidate_high = RetrievalCandidate(
            command="auto qos classify", description="", sqlite_id=2,
            match_type="semantic", confidence=0.6, source="product_cr",
            candidate_status="candidate",
        )
        result = deduplicate_and_rank([], [trusted_low, candidate_high], boost_exact=False)
        assert result[0].command == "auto qos classify"


class TestRetrievalQuery:
    """Test RetrievalQuery schema."""

    def test_valid_query(self):
        """Valid queries do not raise."""
        q = RetrievalQuery(input_text="show ip route")
        q.validate()  # Should not raise.

    def test_empty_input_invalid(self):
        """Empty input is invalid."""
        q = RetrievalQuery(input_text="")
        with pytest.raises(ValueError, match="input_text cannot be empty"):
            q.validate()

    def test_invalid_mode(self):
        """Invalid mode raises ValueError."""
        q = RetrievalQuery(input_text="show ip", mode="invalid")  # type: ignore
        with pytest.raises(ValueError, match="mode must be"):
            q.validate()

    def test_invalid_confidence(self):
        """Confidence outside [0,1] is invalid."""
        q = RetrievalQuery(input_text="show ip", min_confidence=1.5)
        with pytest.raises(ValueError, match="min_confidence must be"):
            q.validate()

    def test_invalid_top_k(self):
        """top_k < 1 is invalid."""
        q = RetrievalQuery(input_text="show ip", top_k=0)
        with pytest.raises(ValueError, match="top_k must be"):
            q.validate()


class TestRetrievalCandidate:
    """Test RetrievalCandidate schema."""

    def test_sort_by_confidence(self):
        """Candidates sort by confidence descending."""
        cand1 = RetrievalCandidate(command="alpha", description="", confidence=0.5)
        cand2 = RetrievalCandidate(command="beta", description="", confidence=0.9)
        sorted_list = sorted([cand1, cand2])
        assert sorted_list[0] == cand2  # 0.9 first
        assert sorted_list[1] == cand1  # 0.5 second

    def test_sort_ties_by_command(self):
        """When confidence ties, sort by command name."""
        cand1 = RetrievalCandidate(command="zebra", description="", confidence=0.8)
        cand2 = RetrievalCandidate(command="alpha", description="", confidence=0.8)
        sorted_list = sorted([cand1, cand2])
        assert sorted_list[0].command == "alpha"
        assert sorted_list[1].command == "zebra"


class _FakeRetriever:
    def __init__(self, result: RetrievalResult):
        self._result = result
        self.sqlite_store = type("Store", (), {"db_path": ":memory:"})()

    def query(self, q: RetrievalQuery) -> RetrievalResult:
        return self._result


class TestIntelligentCommandAssistant:
    def test_exact_tier_returns_direct_answer(self):
        candidate = RetrievalCandidate(
            command="show ip ospf neighbor",
            description="Display OSPF neighbor information.",
            platform="iosxe",
            cmd_mode="enable",
            doc_source="ospf.pdf",
            confidence=0.95,
            match_type="exact",
        )
        retriever = _FakeRetriever(
            RetrievalResult(
                query=RetrievalQuery(input_text="show ip ospf neighbor"),
                candidates=[candidate],
                raw_candidates=[candidate],
                query_mode_used="exact",
            )
        )
        assistant = IntelligentCommandAssistant(retriever)

        response = assistant.respond(RetrievalQuery(input_text="show ip ospf neighbor"))

        assert response.response_tier == "exact"
        assert response.results[0].command == "show ip ospf neighbor"
        assert response.results[0].source_attribution == "ospf.pdf"

    def test_exact_tier_surfaces_labeled_candidate_below_trusted_match(self):
        """A trusted exact/prefix match must not hide relevant product_cr candidates.

        Regression test for the 'qos' bug: candidates were technically included
        in raw SQL results but never carried into RetrievalCandidate/labeled, and
        the exact-tier short-circuit (max_exact_results=1) dropped everything else.
        """
        trusted = RetrievalCandidate(
            command="qos share-buffer",
            description="Configure QoS shared buffer.",
            platform="cat9500",
            cmd_mode="config",
            doc_source="lan_switching_cmd_ref.pdf",
            confidence=0.95,
            match_type="prefix",
            sqlite_id=1,
            source="kb",
            candidate_status="",
        )
        web_candidate = RetrievalCandidate(
            command="auto qos classify [police]",
            description="Auto QoS classify.",
            platform="cat9500",
            cmd_mode="config",
            doc_source="https://www.example.com/qos-cr",
            confidence=0.55,
            match_type="semantic",
            sqlite_id=2,
            source="product_cr",
            candidate_status="candidate",
        )
        retriever = _FakeRetriever(
            RetrievalResult(
                query=RetrievalQuery(input_text="qos share buffer settings"),
                candidates=[trusted],
                raw_candidates=[trusted, web_candidate],
                query_mode_used="exact",
            )
        )
        assistant = IntelligentCommandAssistant(retriever)

        response = assistant.respond(RetrievalQuery(input_text="qos share buffer settings"))

        assert response.response_tier == "exact"
        assert response.results[0].command == "qos share-buffer"
        assert response.results[0].is_candidate is False

        candidate_results = [r for r in response.results if r.command == "auto qos classify [police]"]
        assert candidate_results, "product_cr candidate must surface, not be silently dropped"
        candidate_result = candidate_results[0]
        assert candidate_result.is_candidate is True
        assert "verify on platform" in candidate_result.source_attribution.lower()
        # Trusted match ranks ahead of the labeled candidate.
        assert response.results.index(candidate_result) > 0

    def test_typo_query_returns_did_you_mean_suggestion(self):
        retriever = _FakeRetriever(
            RetrievalResult(
                query=RetrievalQuery(input_text="shwo ip rote"),
                candidates=[],
                raw_candidates=[],
                query_mode_used="intent",
                reason_empty="below_threshold",
            )
        )
        assistant = IntelligentCommandAssistant(retriever)
        corpus_candidate = RetrievalCandidate(
            command="show ip route",
            description="Display the IP routing table.",
            platform="iosxe",
            cmd_mode="exec",
            doc_source="routing.pdf",
            confidence=0.0,
            match_type="semantic",
        )
        assistant._load_command_corpus = lambda platform, cmd_mode: (corpus_candidate,)  # type: ignore[attr-defined]

        response = assistant.respond(RetrievalQuery(input_text="shwo ip rote"))

        assert response.response_tier == "suggestion"
        assert response.message.startswith("Not an exact match")
        assert response.results[0].command == "show ip route"
        assert response.results[0].confidence_score >= assistant.config.fuzzy_threshold

    def test_explore_query_returns_grouped_related_results(self):
        candidates = [
            RetrievalCandidate(
                command="show ip ospf neighbor",
                description="Display OSPF neighbors.",
                platform="iosxe",
                cmd_mode="enable",
                doc_source="ospf.pdf",
                confidence=0.64,
                match_type="semantic",
            ),
            RetrievalCandidate(
                command="show ip ospf interface",
                description="Display OSPF interface state.",
                platform="iosxe",
                cmd_mode="enable",
                doc_source="ospf.pdf",
                confidence=0.61,
                match_type="semantic",
            ),
        ]
        retriever = _FakeRetriever(
            RetrievalResult(
                query=RetrievalQuery(input_text="what commands for ospf"),
                candidates=candidates,
                raw_candidates=candidates,
                query_mode_used="intent",
            )
        )
        assistant = IntelligentCommandAssistant(retriever)

        response = assistant.respond(RetrievalQuery(input_text="what commands for ospf"))

        assert response.response_tier == "related"
        assert response.grouped_results
        assert response.grouped_results[0].label == "OSPF commands"

    def test_nonsense_query_returns_fallback(self):
        retriever = _FakeRetriever(
            RetrievalResult(
                query=RetrievalQuery(input_text="asdfqwerty"),
                candidates=[],
                raw_candidates=[],
                query_mode_used="intent",
                reason_empty="no_match",
            )
        )
        assistant = IntelligentCommandAssistant(retriever)
        assistant._load_command_corpus = lambda platform, cmd_mode: tuple()  # type: ignore[attr-defined]

        response = assistant.respond(RetrievalQuery(input_text="asdfqwerty"))

        assert response.response_tier == "fallback"
        assert "Try rephrasing" in response.message
