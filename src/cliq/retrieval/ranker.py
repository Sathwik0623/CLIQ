#!/usr/bin/env python3
"""
ranker.py — Hybrid ranking and deduplication of retrieval results.
"""

from __future__ import annotations

from typing import Dict, List

from cliq.retrieval.schema import RetrievalCandidate


def deduplicate_and_rank(
    exact_matches: List[RetrievalCandidate],
    semantic_matches: List[RetrievalCandidate],
    boost_exact: bool = True,
) -> List[RetrievalCandidate]:
    """
    Merge exact and semantic matches into a deduplicated, ranked list.

    Args:
        exact_matches: Results from SQLite exact/prefix queries.
        semantic_matches: Results from Vector DB semantic queries.
        boost_exact: If True, prioritize exact/prefix matches above semantic ones.

    Returns:
        Merged, ranked list of candidates.
    """
    # Build a map of candidates by sqlite_id to deduplicate.
    seen: Dict[int | str, RetrievalCandidate] = {}

    # Process exact matches first (higher priority).
    for candidate in exact_matches:
        key = candidate.sqlite_id if candidate.sqlite_id is not None else candidate.command
        if key not in seen:
            seen[key] = candidate
        else:
            # If the same command appears in both lists, keep the exact match.
            if candidate.match_type in ("exact", "prefix"):
                seen[key] = candidate

    # Process semantic matches, but don't overwrite exact ones.
    for candidate in semantic_matches:
        key = candidate.sqlite_id if candidate.sqlite_id is not None else candidate.command
        if key not in seen:
            seen[key] = candidate
        # If already seen and it was an exact match, keep the exact.
        # If already seen and it's also semantic, keep the one with higher confidence.
        elif candidate.match_type == "semantic" and seen[key].match_type == "semantic":
            if candidate.confidence > seen[key].confidence:
                seen[key] = candidate

    # Boost exact matches when requested (shift their confidence higher for sorting).
    candidates = list(seen.values())
    if boost_exact:
        for candidate in candidates:
            if candidate.match_type in ("exact", "prefix"):
                # Boost by clamping to [0.9, 1.0] if lower, or preserving if already high.
                candidate.confidence = max(0.9, candidate.confidence)

    # Sort by confidence (descending), then trust tier, then match type, then name.
    # Trust tier keeps an unvalidated product-CR candidate from outranking an
    # equally (or more) confident validated KB row on a tie — candidates still
    # surface (they are never dropped here), just ranked below trusted matches.
    candidates.sort(
        key=lambda c: (
            -c.confidence,  # Higher confidence first
            _trust_tier(c),  # trusted/validated KB before unvalidated candidate
            _match_type_priority(c.match_type),  # exact > prefix > semantic
            c.command,  # Alphabetically for ties
        )
    )

    return candidates


def _trust_tier(candidate: RetrievalCandidate) -> int:
    """0 = trusted/validated KB row, 1 = unvalidated product-CR candidate."""
    return 1 if candidate.candidate_status == "candidate" else 0


def _match_type_priority(match_type: str) -> int:
    """Return a priority score for sorting by match type."""
    if match_type == "exact":
        return 0
    elif match_type == "prefix":
        return 1
    else:  # semantic
        return 2


def apply_filters(
    candidates: List[RetrievalCandidate],
    platform: str | None,
    cmd_mode: str | None,
) -> tuple[List[RetrievalCandidate], str | None]:
    """
    Filter candidates by platform and/or command mode.

    Args:
        candidates: Candidates to filter.
        platform: Filter by this platform, or None for no filter.
        cmd_mode: Filter by this command mode, or None for no filter.

    Returns:
        (filtered_list, reason_if_empty)
        reason will be one of: None, 'platform_mismatch', 'mode_mismatch',
        'platform_and_mode_mismatch'.
    """
    filtered = candidates
    reasons = []

    if platform is not None:
        before = len(filtered)
        filtered = [c for c in filtered if c.platform == platform]
        if not filtered:
            reasons.append(f"platform_mismatch ({platform})")

    if cmd_mode is not None:
        before = len(filtered)
        filtered = [c for c in filtered if c.cmd_mode == cmd_mode]
        if not filtered:
            reasons.append(f"mode_mismatch ({cmd_mode})")

    reason = "_and_".join(reasons) if reasons else None
    return filtered, reason
