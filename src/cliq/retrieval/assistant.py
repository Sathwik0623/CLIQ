#!/usr/bin/env python3
"""
assistant.py — Intelligent suggestion and discovery layer for retrieval.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import replace
from functools import lru_cache
from typing import TYPE_CHECKING, Iterable, Optional

from cliq.intelligence.typo_correction import levenshtein

from cliq.retrieval.query import CommandRetriever
from cliq.retrieval.schema import (
    IntelligentCommandResult,
    IntelligentResultGroup,
    IntelligentRetrievalConfig,
    IntelligentRetrievalResponse,
    RetrievalCandidate,
    RetrievalQuery,
)

if TYPE_CHECKING:
    from cliq.memory.operational_memory import OperationalMemoryStore

logger = logging.getLogger(__name__)

_EXPLORE_PATTERNS = (
    "what commands",
    "show me",
    "available for",
    "commands for",
    "something with",
    "what can i do",
    "what exists",
    "explore",
    "browse",
    "list",
)
_INTENT_PATTERNS = (
    "how do i",
    "how to",
    "how can i",
    "check",
    "verify",
    "view",
    "see",
    "inspect",
)
_STOPWORDS = {
    "a",
    "an",
    "and",
    "available",
    "can",
    "check",
    "commands",
    "do",
    "for",
    "how",
    "i",
    "is",
    "me",
    "show",
    "something",
    "the",
    "to",
    "view",
    "what",
    "with",
}


class IntelligentCommandAssistant:
    """Wraps CommandRetriever with tiered guidance, discovery behavior, and contextual memory."""

    def __init__(
        self,
        retriever: CommandRetriever,
        config: IntelligentRetrievalConfig | None = None,
        memory_dir: Optional[str] = None,
        enable_memory: bool = True,
    ):
        self.retriever = retriever
        self.config = config or IntelligentRetrievalConfig()
        self.config.validate()
        
        # Initialize operational memory (Phase 5) — lazy import to avoid circular dependency
        self.enable_memory = enable_memory
        self.memory_store: Optional[OperationalMemoryStore] = None
        if enable_memory:
            try:
                from cliq.memory.operational_memory import OperationalMemoryStore
                self.memory_store = OperationalMemoryStore(memory_dir=memory_dir)
            except Exception as e:
                logger.warning(f"Failed to initialize memory store: {e}")
                self.memory_store = None

    def respond(self, q: RetrievalQuery) -> IntelligentRetrievalResponse:
        """Return a tiered, user-facing response for the query."""
        q.validate()

        # Deterministic exact-first lookup: always check SQLite exact match first.
        exact_matches = self._lookup_exact_matches(q)
        if exact_matches:
            results = [
                self._to_command_result(candidate, "exact")
                for candidate in exact_matches[: self.config.max_exact_results]
            ]
            best = results[0]
            response = IntelligentRetrievalResponse(
                query=q,
                response_tier="exact",
                message=f"Here is the command for that request: {best.command}",
                results=results,
                confidence_score=best.confidence_score,
                query_mode_used="exact",
                total_candidates_considered=len(exact_matches),
                execution_time_ms=0.0,
                reason=None,
            )
            # A literal exact match must not hide other relevant results (e.g.
            # unvalidated product-CR web candidates) — they still surface here,
            # ranked below the deterministic top answer and labeled distinctly.
            self._augment_with_related_candidates(response, q, results)
            self._augment_with_memories(response, q)
            return response

        retrieval_query = replace(
            q,
            top_k=max(q.top_k, self.config.retrieval_pool_size),
            min_confidence=min(q.min_confidence, self.config.related_threshold),
        )
        retrieval_result = self.retriever.query(retrieval_query)
        raw_candidates = retrieval_result.raw_candidates[: self.config.retrieval_pool_size]

        if self._is_explore_query(q.input_text):
            explore_response = self._build_related_response(
                q=q,
                retrieval_result=retrieval_result,
                candidates=raw_candidates,
                explore_mode=True,
            )
            if explore_response is not None:
                return explore_response

        retriever_exact_matches = [
            candidate
            for candidate in raw_candidates
            if candidate.match_type in {"exact", "prefix"}
            and candidate.confidence >= self.config.exact_threshold
        ]
        if retriever_exact_matches:
            results = [
                self._to_command_result(candidate, "exact")
                for candidate in retriever_exact_matches[: self.config.max_exact_results]
            ]
            best = results[0]
            response = IntelligentRetrievalResponse(
                query=q,
                response_tier="exact",
                message=f"Here is the command for that request: {best.command}",
                results=results,
                confidence_score=best.confidence_score,
                query_mode_used=retrieval_result.query_mode_used,
                total_candidates_considered=retrieval_result.total_candidates_considered,
                execution_time_ms=retrieval_result.execution_time_ms,
                reason=retrieval_result.reason_empty,
            )
            self._augment_with_related_candidates(response, q, results, pool=raw_candidates)
            # Augment with past resolutions
            self._augment_with_memories(response, q)
            return response

        suggestion_candidates = self._build_suggestion_candidates(q, raw_candidates)
        if suggestion_candidates:
            results = [
                self._to_command_result(candidate, "suggestion")
                for candidate in suggestion_candidates[: self.config.max_suggestions]
            ]
            response = IntelligentRetrievalResponse(
                query=q,
                response_tier="suggestion",
                message="Not an exact match. Did you mean one of these?",
                results=results,
                confidence_score=results[0].confidence_score,
                query_mode_used=retrieval_result.query_mode_used,
                total_candidates_considered=retrieval_result.total_candidates_considered,
                execution_time_ms=retrieval_result.execution_time_ms,
                reason=retrieval_result.reason_empty,
            )
            # Augment with past resolutions
            self._augment_with_memories(response, q)
            return response

        related_response = self._build_related_response(
            q=q,
            retrieval_result=retrieval_result,
            candidates=raw_candidates,
            explore_mode=False,
        )
        if related_response is not None:
            # Augment with past resolutions
            self._augment_with_memories(related_response, q)
            return related_response

        response = IntelligentRetrievalResponse(
            query=q,
            response_tier="fallback",
            message=(
                "No matching command found. Try rephrasing the request, adding a platform or mode, "
                "or explore commands by topic."
            ),
            confidence_score=0.0,
            query_mode_used=retrieval_result.query_mode_used,
            total_candidates_considered=retrieval_result.total_candidates_considered,
            execution_time_ms=retrieval_result.execution_time_ms,
            reason=retrieval_result.reason_empty or "no_match",
        )
        # Augment with past resolutions
        self._augment_with_memories(response, q)
        return response

    def _lookup_exact_matches(self, q: RetrievalQuery) -> list[RetrievalCandidate]:
        normalized_query = _normalize_text(q.input_text)
        if not normalized_query:
            return []

        try:
            con = sqlite3.connect(self.retriever.sqlite_store.db_path)
            con.row_factory = sqlite3.Row
            try:
                sql = (
                    "SELECT id, command, description, syntax_variants, mode, platform, doc_source, "
                    "source, candidate_status "
                    "FROM commands WHERE lower(trim(command)) = ?"
                )
                params: list[str] = [normalized_query]
                if q.platform:
                    sql += " AND platform = ?"
                    params.append(q.platform)
                if q.cmd_mode:
                    sql += " AND mode = ?"
                    params.append(q.cmd_mode)

                rows = con.execute(sql, params).fetchall()
            finally:
                con.close()
        except sqlite3.Error:
            # Unit tests may use a minimal in-memory retriever without a commands table,
            # or an older schema that predates the source/candidate_status columns.
            try:
                con = sqlite3.connect(self.retriever.sqlite_store.db_path)
                con.row_factory = sqlite3.Row
                try:
                    sql = (
                        "SELECT id, command, description, syntax_variants, mode, platform, doc_source "
                        "FROM commands WHERE lower(trim(command)) = ?"
                    )
                    params = [normalized_query]
                    if q.platform:
                        sql += " AND platform = ?"
                        params.append(q.platform)
                    if q.cmd_mode:
                        sql += " AND mode = ?"
                        params.append(q.cmd_mode)
                    rows = con.execute(sql, params).fetchall()
                finally:
                    con.close()
            except sqlite3.Error:
                return []

        return [
            RetrievalCandidate(
                command=row["command"],
                description=row["description"] or "",
                syntax_variants=_parse_json_list(row["syntax_variants"]),
                platform=row["platform"],
                cmd_mode=row["mode"],
                doc_source=row["doc_source"] or "",
                confidence=0.99,
                match_type="exact",
                sqlite_id=row["id"],
                source=(row["source"] if "source" in row.keys() else "") or "",
                candidate_status=(row["candidate_status"] if "candidate_status" in row.keys() else "") or "",
            )
            for row in rows
        ]

    def _augment_with_related_candidates(
        self,
        response: IntelligentRetrievalResponse,
        q: RetrievalQuery,
        primary_results: list[IntelligentCommandResult],
        pool: list[RetrievalCandidate] | None = None,
    ) -> None:
        """Append other relevant candidates below the deterministic top answer.

        A literal exact-command match (e.g. a single trusted PDF row) must not
        silently hide other relevant matches — in particular unvalidated
        product-CR web candidates for the same topic. Those still surface here,
        ranked below the primary result(s) and labeled distinctly, capped to
        ``config.max_suggestions``.
        """
        if pool is None:
            try:
                pool_query = replace(
                    q,
                    top_k=self.config.retrieval_pool_size,
                    min_confidence=self.config.related_threshold,
                )
                pool = self.retriever.query(pool_query).raw_candidates
            except Exception:
                pool = []

        shown = {(r.command, r.platform) for r in primary_results}
        extras: list[RetrievalCandidate] = []
        extra_keys: set[tuple[str, str]] = set()
        for candidate in pool:
            key = (candidate.command, candidate.platform)
            if key in shown or key in extra_keys:
                continue
            if candidate.confidence < self.config.related_threshold:
                continue
            extra_keys.add(key)
            extras.append(candidate)

        # Confidence drives relevance first; trust tier only breaks ties, so a
        # genuinely more-relevant candidate can still out-rank a barely-relevant
        # trusted result, while equally-relevant candidates rank below trusted.
        extras.sort(
            key=lambda c: (-c.confidence, c.candidate_status == "candidate", c.command)
        )
        for candidate in extras[: self.config.max_related]:
            response.results.append(self._to_command_result(candidate, response.response_tier))

    def _augment_with_memories(
        self,
        response: IntelligentRetrievalResponse,
        q: RetrievalQuery,
    ) -> None:
        """Augment response with recalled past resolutions (Phase 5)."""
        if not self.enable_memory or not self.memory_store:
            return
        
        try:
            # Recall similar past resolutions
            past_resolutions = self.memory_store.recall_similar(
                query_text=q.input_text,
                top_k=2,  # Show top 2 past resolutions
                min_similarity=0.6,
                platform_filter=q.platform,  # Prefer same platform but don't exclude others
                outcome_filter="resolved" if q.platform else None,  # Prefer resolved outcomes
            )
            
            if past_resolutions:
                response.past_resolutions = past_resolutions
                logger.debug(f"Recalled {len(past_resolutions)} past resolutions for query")
        except Exception as e:
            logger.warning(f"Failed to recall memories: {e}")
            # Continue without memories; they're optional

    def _build_suggestion_candidates(
        self,
        q: RetrievalQuery,
        raw_candidates: list[RetrievalCandidate],
    ) -> list[RetrievalCandidate]:
        scored: list[tuple[float, RetrievalCandidate]] = []
        seen: set[tuple[str, str, str]] = set()

        for candidate in raw_candidates:
            if candidate.confidence < self.config.suggestion_threshold:
                continue
            key = (candidate.command, candidate.platform, candidate.cmd_mode)
            if key in seen:
                continue
            seen.add(key)
            scored.append((candidate.confidence, candidate))

        for score, candidate in self._fuzzy_candidates(
            q.input_text,
            platform=q.platform,
            cmd_mode=q.cmd_mode,
        ):
            if score < self.config.fuzzy_threshold:
                continue
            key = (candidate.command, candidate.platform, candidate.cmd_mode)
            if key in seen:
                continue
            seen.add(key)
            scored.append((score, replace(candidate, confidence=score)))

        scored.sort(key=lambda item: (-item[0], item[1].command))
        return [candidate for _, candidate in scored]

    def _build_related_response(
        self,
        q: RetrievalQuery,
        retrieval_result,
        candidates: list[RetrievalCandidate],
        explore_mode: bool,
    ) -> IntelligentRetrievalResponse | None:
        related_candidates = [
            candidate
            for candidate in candidates
            if candidate.confidence >= self.config.related_threshold
        ]
        if not related_candidates:
            topic_candidates = [
                candidate
                for candidate in self._fuzzy_candidates(
                    q.input_text,
                    platform=q.platform,
                    cmd_mode=q.cmd_mode,
                )
                if candidate[0] >= self.config.fuzzy_threshold
            ]
            related_candidates = [replace(candidate, confidence=score) for score, candidate in topic_candidates]

        if not related_candidates:
            return None

        trimmed = related_candidates[: self.config.max_related]
        groups = self._group_candidates(q.input_text, trimmed, "related")
        message = "Related commands for this area:" if not explore_mode else "Browse these commands by topic:"
        best_confidence = max(candidate.confidence for candidate in trimmed)

        return IntelligentRetrievalResponse(
            query=q,
            response_tier="related",
            message=message,
            results=[self._to_command_result(candidate, "related") for candidate in trimmed],
            grouped_results=groups,
            confidence_score=best_confidence,
            query_mode_used=retrieval_result.query_mode_used,
            total_candidates_considered=retrieval_result.total_candidates_considered,
            execution_time_ms=retrieval_result.execution_time_ms,
            reason=retrieval_result.reason_empty,
        )

    def _group_candidates(
        self,
        input_text: str,
        candidates: list[RetrievalCandidate],
        tier: str,
    ) -> list[IntelligentResultGroup]:
        topics = self._extract_topics(input_text)
        grouped: dict[str, list[RetrievalCandidate]] = {}

        for candidate in candidates:
            haystack = f"{candidate.command} {candidate.description}".lower()
            label = next(
                (
                    self._format_group_label(topic)
                    for topic in topics
                    if topic in haystack
                ),
                self._fallback_group_label(candidate),
            )
            grouped.setdefault(label, [])
            if len(grouped[label]) < self.config.max_group_size:
                grouped[label].append(candidate)

        ordered_labels = sorted(
            grouped,
            key=lambda label: (-max(item.confidence for item in grouped[label]), label),
        )[: self.config.max_groups]

        return [
            IntelligentResultGroup(
                label=label,
                results=[self._to_command_result(candidate, tier) for candidate in grouped[label]],
            )
            for label in ordered_labels
        ]

    def _to_command_result(
        self,
        candidate: RetrievalCandidate,
        response_tier: str,
    ) -> IntelligentCommandResult:
        is_candidate = candidate.candidate_status == "candidate"
        attribution = candidate.doc_source
        if is_candidate:
            platform_label = candidate.platform.upper() if candidate.platform else "this platform"
            attribution = f"From Cisco {platform_label} Command Reference \u2014 verify on platform"
        return IntelligentCommandResult(
            command=candidate.command,
            description=candidate.description,
            syntax_variants=list(candidate.syntax_variants),
            platform=candidate.platform,
            mode=candidate.cmd_mode,
            confidence_score=candidate.confidence,
            response_tier=response_tier,
            source_attribution=attribution,
            match_type=candidate.match_type,
            is_candidate=is_candidate,
        )

    def _is_explore_query(self, text: str) -> bool:
        normalized = _normalize_text(text)
        if any(pattern in normalized for pattern in _EXPLORE_PATTERNS):
            return True
        tokens = normalized.split()
        if len(tokens) <= 2 and not any(pattern in normalized for pattern in _INTENT_PATTERNS):
            return True
        return False

    def _extract_topics(self, text: str) -> list[str]:
        normalized = _normalize_text(text)
        topics: list[str] = []
        for token in normalized.split():
            if token in _STOPWORDS or len(token) < 3:
                continue
            if token not in topics:
                topics.append(token)
        return topics[: self.config.max_groups]

    def _fallback_group_label(self, candidate: RetrievalCandidate) -> str:
        if candidate.cmd_mode.startswith("config"):
            return "Configuration commands"
        return f"{candidate.platform.upper()} {candidate.cmd_mode} commands"

    def _format_group_label(self, topic: str) -> str:
        return f"{topic.upper()} commands"

    def _fuzzy_candidates(
        self,
        text: str,
        platform: str | None,
        cmd_mode: str | None,
    ) -> list[tuple[float, RetrievalCandidate]]:
        query_text = _normalize_text(text)
        query_topics = self._extract_topics(text)
        scored: list[tuple[float, RetrievalCandidate]] = []
        for candidate in self._load_command_corpus(platform, cmd_mode):
            score = self._score_fuzzy_match(query_text, query_topics, candidate)
            if score <= 0:
                continue
            scored.append((score, candidate))
        scored.sort(key=lambda item: (-item[0], item[1].command))
        return scored[: self.config.retrieval_pool_size]

    @lru_cache(maxsize=16)
    def _load_command_corpus(
        self,
        platform: str | None,
        cmd_mode: str | None,
    ) -> tuple[RetrievalCandidate, ...]:
        con = sqlite3.connect(self.retriever.sqlite_store.db_path)
        con.row_factory = sqlite3.Row
        try:
            sql = (
                "SELECT id, command, description, syntax_variants, mode, platform, doc_source, "
                "source, candidate_status "
                "FROM commands WHERE 1=1"
            )
            params: list[str] = []
            if platform:
                sql += " AND platform = ?"
                params.append(platform)
            if cmd_mode:
                sql += " AND mode = ?"
                params.append(cmd_mode)
            try:
                rows = con.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                # Older/minimal schema without source/candidate_status columns.
                fallback_sql = sql.replace("source, candidate_status ", "").replace(
                    "doc_source, ", "doc_source "
                )
                rows = con.execute(fallback_sql, params).fetchall()
        finally:
            con.close()

        return tuple(
            RetrievalCandidate(
                command=row["command"],
                description=row["description"] or "",
                syntax_variants=_parse_json_list(row["syntax_variants"]),
                platform=row["platform"],
                cmd_mode=row["mode"],
                doc_source=row["doc_source"] or "",
                confidence=0.0,
                match_type="semantic",
                sqlite_id=row["id"],
                source=(row["source"] if "source" in row.keys() else "") or "",
                candidate_status=(row["candidate_status"] if "candidate_status" in row.keys() else "") or "",
            )
            for row in rows
        )

    def _score_fuzzy_match(
        self,
        normalized_query: str,
        query_topics: list[str],
        candidate: RetrievalCandidate,
    ) -> float:
        normalized_command = _normalize_text(candidate.command)
        normalized_description = _normalize_text(candidate.description)
        if not normalized_query or not normalized_command:
            return 0.0
        if normalized_query == normalized_command:
            return 1.0

        query_tokens = normalized_query.split()
        command_tokens = normalized_command.split()
        if not query_tokens or not command_tokens:
            return 0.0

        whole_distance = levenshtein(normalized_query, normalized_command)
        whole_score = 1.0 - (whole_distance / max(len(normalized_query), len(normalized_command), 1))

        token_scores: list[float] = []
        for index, token in enumerate(query_tokens):
            target = command_tokens[index] if index < len(command_tokens) else command_tokens[-1]
            if target.startswith(token):
                token_scores.append(max(0.9, len(token) / max(len(target), 1)))
                continue
            distance = levenshtein(token, target)
            token_scores.append(1.0 - (distance / max(len(token), len(target), 1)))

        overlap = len(set(query_tokens) & set(command_tokens)) / max(len(set(query_tokens)), 1)
        description_overlap = 0.0
        if normalized_description:
            description_overlap = len(
                {topic for topic in query_topics if topic in normalized_description}
            ) / max(len(query_topics), 1)
        leading_bonus = 0.12 if normalized_command.startswith(query_tokens[0]) else 0.0
        token_average = sum(token_scores) / len(token_scores)
        length_penalty = min(len(query_tokens), len(command_tokens)) / max(len(query_tokens), len(command_tokens))
        transposition_bonus = 0.0
        if len(query_tokens) == len(command_tokens):
            transposition_bonus = 0.08 if sum(levenshtein(a, b) for a, b in zip(query_tokens, command_tokens)) <= 4 else 0.0
        return max(
            0.0,
            min(
                1.0,
                (whole_score * 0.42)
                + (token_average * 0.34)
                + (overlap * 0.08)
                + (description_overlap * 0.08)
                + (length_penalty * 0.08)
                + leading_bonus
                + transposition_bonus,
            ),
        )


def _normalize_text(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _parse_json_list(payload: str | None) -> list[str]:
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, Iterable) or isinstance(parsed, (str, bytes)):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]