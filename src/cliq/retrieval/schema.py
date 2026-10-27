#!/usr/bin/env python3
"""
schema.py — Query and result schemas for the retrieval layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


ResponseTier = Literal["exact", "suggestion", "related", "fallback"]


@dataclass
class RetrievalQuery:
    """A query to retrieve command candidates from the knowledge base."""

    input_text: str
    """The engineer's input: natural-language intent or command prefix."""

    mode: Literal["auto", "intent", "exact"] = "auto"
    """Query mode: auto-detect, semantic intent search, or exact/prefix lookup."""

    platform: Optional[str] = None
    """Optional platform filter (iosxe, iosxr, iosv, iosvl2). None = no filter."""

    cmd_mode: Optional[str] = None
    """Optional command mode filter (exec, config, config-if, enable). None = no filter."""

    top_k: int = 5
    """Return at most this many candidates."""

    min_confidence: float = 0.6
    """Suppress results with confidence < this threshold (0–1)."""

    def validate(self) -> None:
        """Raise ValueError if the query is invalid."""
        if not self.input_text or not self.input_text.strip():
            raise ValueError("input_text cannot be empty")
        if self.mode not in ("auto", "intent", "exact"):
            raise ValueError(f"mode must be 'auto', 'intent', or 'exact'; got {self.mode}")
        if not (0 <= self.min_confidence <= 1):
            raise ValueError(f"min_confidence must be in [0, 1]; got {self.min_confidence}")
        if self.top_k < 1:
            raise ValueError(f"top_k must be >= 1; got {self.top_k}")


@dataclass
class RetrievalCandidate:
    """A ranked command candidate returned by the retrieval layer."""

    command: str
    """The full command syntax (e.g., 'ip pim rp-address rp-address [ access-list ]')."""

    description: str
    """Command description (may be empty)."""

    syntax_variants: list[str] = field(default_factory=list)
    """Alternative syntax forms of this command."""

    platform: str = "iosxe"
    """Platform this command applies to."""

    cmd_mode: str = "exec"
    """Command mode (exec, config, config-if, enable, etc.)."""

    doc_source: str = ""
    """Documentation source (e.g., filename, traceability)."""

    confidence: float = 0.0
    """Confidence score (0–1). Higher = more confident in relevance."""

    match_type: Literal["exact", "prefix", "semantic"] = "semantic"
    """How the command matched the query."""

    sqlite_id: Optional[int] = None
    """Internal link to SQLite row (for deduplication and direct lookup)."""

    source: str = ""
    """KB row source: 'kb'/'mcl' (PDF/MCL, validated) or 'product_cr' (web-derived)."""

    candidate_status: str = ""
    """'' | 'candidate' | 'trusted' — two-stage promotion status for product_cr rows."""

    def __lt__(self, other: RetrievalCandidate) -> bool:
        """Sort by confidence descending, then by command alphabetically."""
        if self.confidence != other.confidence:
            return self.confidence > other.confidence
        return self.command < other.command


@dataclass
class RetrievalResult:
    """The result of a retrieval query."""

    query: RetrievalQuery
    """The original query."""

    candidates: list[RetrievalCandidate] = field(default_factory=list)
    """Ranked list of command candidates (best first)."""

    raw_candidates: list[RetrievalCandidate] = field(default_factory=list)
    """Ranked candidates before the confidence threshold is applied."""

    query_mode_used: Literal["intent", "exact"] = "exact"
    """Which mode was actually used for this query."""

    total_candidates_considered: int = 0
    """Number of candidates examined before filtering/ranking."""

    execution_time_ms: float = 0.0
    """Query execution time in milliseconds."""

    reason_empty: Optional[str] = None
    """If candidates is empty, the reason why (e.g., 'no_match', 'platform_mismatch')."""

    def summary(self) -> str:
        """Return a one-line summary of the result."""
        if not self.candidates:
            reason = self.reason_empty or "unknown"
            return f"No candidates ({reason})"
        best = self.candidates[0]
        conf_pct = int(best.confidence * 100)
        return f"{len(self.candidates)} candidates; best: '{best.command}' ({conf_pct}%, {best.match_type})"


@dataclass
class IntelligentRetrievalConfig:
    """Thresholds and limits for the suggestion/discovery layer."""

    exact_threshold: float = 0.85
    suggestion_threshold: float = 0.6
    related_threshold: float = 0.35
    fuzzy_threshold: float = 0.72
    max_exact_results: int = 1
    max_suggestions: int = 4
    max_related: int = 6
    max_groups: int = 3
    max_group_size: int = 4
    retrieval_pool_size: int = 12

    def validate(self) -> None:
        """Raise ValueError if assistant configuration is invalid."""
        thresholds = (
            self.exact_threshold,
            self.suggestion_threshold,
            self.related_threshold,
            self.fuzzy_threshold,
        )
        if any(not 0 <= threshold <= 1 for threshold in thresholds):
            raise ValueError("assistant thresholds must be in [0, 1]")
        if not self.exact_threshold >= self.suggestion_threshold >= self.related_threshold:
            raise ValueError(
                "thresholds must satisfy exact_threshold >= suggestion_threshold >= related_threshold"
            )
        counts = (
            self.max_exact_results,
            self.max_suggestions,
            self.max_related,
            self.max_groups,
            self.max_group_size,
            self.retrieval_pool_size,
        )
        if any(count < 1 for count in counts):
            raise ValueError("assistant limits must be >= 1")


@dataclass
class IntelligentCommandResult:
    """A user-facing command suggestion or answer."""

    command: str
    description: str
    syntax_variants: list[str] = field(default_factory=list)
    platform: str = "iosxe"
    mode: str = "exec"
    confidence_score: float = 0.0
    response_tier: ResponseTier = "fallback"
    source_attribution: str = ""
    match_type: str = "semantic"
    is_candidate: bool = False
    """True for an unvalidated product-CR advisory candidate (verify on platform)."""


@dataclass
class IntelligentResultGroup:
    """A grouped set of user-facing command suggestions."""

    label: str
    results: list[IntelligentCommandResult] = field(default_factory=list)


@dataclass
class IntelligentRetrievalResponse:
    """A rich response that never dead-ends the user."""

    query: RetrievalQuery
    response_tier: ResponseTier
    message: str
    results: list[IntelligentCommandResult] = field(default_factory=list)
    grouped_results: list[IntelligentResultGroup] = field(default_factory=list)
    confidence_score: float = 0.0
    query_mode_used: Literal["intent", "exact"] = "exact"
    total_candidates_considered: int = 0
    execution_time_ms: float = 0.0
    reason: Optional[str] = None
    past_resolutions: list[PastResolution] = field(default_factory=list)
    """Recalled contextual memories from similar past issues."""

    def summary(self) -> str:
        """Return a one-line summary of the intelligent response."""
        if self.response_tier == "fallback":
            return f"fallback: {self.message}"
        count = len(self.results)
        if not count and self.grouped_results:
            count = sum(len(group.results) for group in self.grouped_results)
        return f"{self.response_tier}: {count} result(s); {self.message}"


OutcomeStatus = Literal["pending", "resolved", "failed", "partial"]
MemoryScope = Literal["user", "team", "organization"]


@dataclass
class MemoryRecord:
    """A recorded issue, commands, and resolution outcome."""

    issue_text: str
    """Description of the problem/issue encountered."""

    commands: list[str]
    """Commands suggested/used to resolve the issue."""

    platform: str = "iosxe"
    """Platform context (iosxe, iosxr, iosv, etc.)."""

    device_name: Optional[str] = None
    """Optional device identifier (switch-1, router-2, etc.)."""

    outcome: OutcomeStatus = "pending"
    """Resolution outcome: pending, resolved, failed, or partial."""

    outcome_notes: str = ""
    """Optional notes about the outcome or resolution."""

    scope: MemoryScope = "user"
    """Access scope: user, team, organization (for future RBAC)."""

    timestamp: str = ""
    """ISO8601 timestamp when the memory was recorded."""

    memory_id: Optional[str] = None
    """UUID of this memory record in the store."""

    embedding: Optional[list[float]] = None
    """Semantic vector embedding of the issue text (cached from vector DB)."""

    similarity_score: float = 0.0
    """Semantic similarity to a query (populated during recall, not storage)."""

    def to_display_string(self) -> str:
        """Format for user display."""
        platform_str = f" on {self.device_name}" if self.device_name else f" ({self.platform})"
        outcome_emoji = {
            "resolved": "✓",
            "failed": "✗",
            "partial": "⚠",
            "pending": "⏳",
        }.get(self.outcome, "?")
        commands_str = "; ".join(self.commands[:2])
        if len(self.commands) > 2:
            commands_str += f" (+{len(self.commands) - 2} more)"
        return f"{outcome_emoji} {self.issue_text}{platform_str}\n    Commands: {commands_str}"


@dataclass
class PastResolution:
    """A recalled past resolution relevant to a current query."""

    memory_record: MemoryRecord
    """The full memory record."""

    similarity_score: float = 0.0
    """Semantic similarity score (0–1) to current query."""

    recency_score: float = 0.0
    """Recency boost (0–1, higher = more recent)."""

    final_rank_score: float = 0.0
    """Composite rank score (similarity * outcome_weight * recency_boost)."""

    reason: str = ""
    """Why this memory was recalled (e.g., 'Similar OSPF issue on different device')."""

    def display_string(self) -> str:
        """Format for showing in a response."""
        score_pct = int(self.final_rank_score * 100)
        platform_str = f" {self.memory_record.device_name or self.memory_record.platform}"
        outcome_str = self.memory_record.outcome
        if self.memory_record.outcome_notes:
            outcome_str += f": {self.memory_record.outcome_notes}"
        commands_str = "; ".join(self.memory_record.commands[:2])
        if len(self.memory_record.commands) > 2:
            commands_str += f" (+{len(self.memory_record.commands) - 2} more)"
        return (
            f"[{score_pct}% relevant{platform_str}] {self.memory_record.issue_text}\n"
            f"    Status: {outcome_str}\n"
            f"    Commands: {commands_str}"
        )
