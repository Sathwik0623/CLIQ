#!/usr/bin/env python3
"""
query.py — Main retrieval logic: query execution, hybrid ranking, result assembly.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import TYPE_CHECKING, Optional

from cliq.ingest.storage.embeddings import Embedder, get_embedder
from cliq.retrieval.intent_classifier import classify_input
from cliq.retrieval.ranker import apply_filters, deduplicate_and_rank
from cliq.retrieval.schema import (
    RetrievalCandidate,
    RetrievalQuery,
    RetrievalResult,
)

if TYPE_CHECKING:
    from cliq.ingest.storage.sqlite_store import CommandStore
    from cliq.ingest.storage.vector_store import SQLiteVectorStore

log = logging.getLogger("retrieval.query")


class CommandRetriever:
    """Retrieves command candidates from the knowledge base (SQLite + Vector DB)."""

    def __init__(
        self,
        sqlite_store: CommandStore,
        vector_store: SQLiteVectorStore,
        embedder: Optional[Embedder] = None,
    ):
        """Initialize retriever with storage backends.

        Args:
            sqlite_store: The relational command store.
            vector_store: The vector/semantic store.
        """
        self.sqlite_store = sqlite_store
        self.vector_store = vector_store
        self.embedder = embedder or get_embedder()

    def query(self, q: RetrievalQuery) -> RetrievalResult:
        """Execute a retrieval query and return ranked candidates.

        Args:
            q: The query specification.

        Returns:
            A ranked result with candidates and metadata.
        """
        q.validate()
        start = time.monotonic()

        # Auto-detect query mode if needed.
        query_mode = q.mode
        if query_mode == "auto":
            query_mode = classify_input(q.input_text)

        exact_matches: list[RetrievalCandidate] = []
        semantic_matches: list[RetrievalCandidate] = []
        total_considered = 0

        # Execute exact/prefix query if requested.
        if query_mode == "exact":
            exact_matches, total_exact = self._query_exact(q.input_text, q.platform, q.cmd_mode)
            total_considered += total_exact

        # Execute semantic query whenever intent mode is used, AND also whenever
        # exact mode is used — even if a prefix match was already found. A bare
        # exact/prefix hit (e.g. a single literal-named row) must never hide
        # genuinely relevant semantic matches (including product_cr web
        # candidates) that don't happen to share a literal prefix with the query.
        if query_mode in ("intent", "exact"):
            semantic_matches, total_semantic = self._query_semantic(
                q.input_text, q.platform, q.cmd_mode
            )
            total_considered += total_semantic

        # Merge and rank.
        boost_exact = query_mode == "exact"
        all_candidates = deduplicate_and_rank(exact_matches, semantic_matches, boost_exact=boost_exact)

        # Apply final filters.
        filtered_candidates, filter_reason = apply_filters(
            all_candidates, q.platform, q.cmd_mode
        )

        # Apply confidence threshold and top-K.
        final_candidates = [
            c for c in filtered_candidates if c.confidence >= q.min_confidence
        ][: q.top_k]

        elapsed_ms = (time.monotonic() - start) * 1000

        # Determine reason if empty.
        reason_empty = None
        if not final_candidates:
            if not all_candidates:
                reason_empty = "no_match"
            elif filter_reason:
                reason_empty = filter_reason
            else:
                reason_empty = "below_threshold"

        return RetrievalResult(
            query=q,
            candidates=final_candidates,
            raw_candidates=filtered_candidates,
            query_mode_used=query_mode,  # type: ignore
            total_candidates_considered=total_considered,
            execution_time_ms=elapsed_ms,
            reason_empty=reason_empty,
        )

    def _query_exact(
        self, input_text: str, platform: Optional[str], cmd_mode: Optional[str]
    ) -> tuple[list[RetrievalCandidate], int]:
        """Execute an exact/prefix SQLite query.

        Returns: (candidates, total_rows_examined)
        """
        try:
            con = sqlite3.connect(self.sqlite_store.db_path)
            con.row_factory = sqlite3.Row
            cur = con.cursor()

            # Normalize input: lowercase, strip.
            search_term = input_text.strip().lower()
            prefix_pattern = f"{search_term}%"

            # Build SQL query.
            query_sql = "SELECT * FROM commands WHERE command LIKE ?"
            params: list = [prefix_pattern]

            if platform:
                query_sql += " AND platform = ?"
                params.append(platform)
            if cmd_mode:
                query_sql += " AND mode = ?"
                params.append(cmd_mode)

            rows = cur.execute(query_sql, params).fetchall()
            con.close()

            candidates = []
            for row in rows:
                candidate = RetrievalCandidate(
                    command=row["command"],
                    description=row["description"] or "",
                    syntax_variants=_parse_syntax_variants(row["syntax_variants"] or ""),
                    platform=row["platform"],
                    cmd_mode=row["mode"],
                    doc_source=row["doc_source"] or "",
                    confidence=0.95 if row["command"].lower() == search_term else 0.85,
                    match_type="exact" if row["command"].lower() == search_term else "prefix",
                    sqlite_id=row["id"],
                    source=_safe_row_get(row, "source"),
                    candidate_status=_safe_row_get(row, "candidate_status"),
                )
                candidates.append(candidate)

            return candidates, len(rows)
        except Exception as e:
            log.error(f"Exact query error: {e}")
            return [], 0

    def _query_semantic(
        self, input_text: str, platform: Optional[str], cmd_mode: Optional[str]
    ) -> tuple[list[RetrievalCandidate], int]:
        """Execute a semantic Vector DB query.

        Returns: (candidates, total_rows_examined)
        """
        try:
            embedding = self.embedder.embed(input_text)

            # Build where filter for vector query.
            where = {}
            if platform:
                where["platform"] = platform

            # Query the vector store.
            vector_hits = self.vector_store.query(embedding, top_k=50, where=where if where else None)

            # Fetch full metadata from SQLite.
            con = sqlite3.connect(self.sqlite_store.db_path)
            con.row_factory = sqlite3.Row
            cur = con.cursor()

            candidates = []
            for hit in vector_hits:
                # Extract sqlite_id from vector_id (format: cmd-N).
                vector_id = hit.vector_id
                sqlite_id = int(vector_id.split("-")[1]) if "-" in vector_id else None
                if sqlite_id is None:
                    continue

                row = cur.execute(
                    "SELECT * FROM commands WHERE id = ?", (sqlite_id,)
                ).fetchone()
                if not row:
                    continue

                # Apply mode filter (not available in metadata, so check here).
                if cmd_mode and row["mode"] != cmd_mode:
                    continue

                # Confidence is the vector similarity score.
                confidence = min(1.0, max(0.0, hit.score))

                candidate = RetrievalCandidate(
                    command=row["command"],
                    description=row["description"] or "",
                    syntax_variants=_parse_syntax_variants(row["syntax_variants"] or ""),
                    platform=row["platform"],
                    cmd_mode=row["mode"],
                    doc_source=row["doc_source"] or "",
                    confidence=confidence,
                    match_type="semantic",
                    sqlite_id=sqlite_id,
                    source=_safe_row_get(row, "source"),
                    candidate_status=_safe_row_get(row, "candidate_status"),
                )
                candidates.append(candidate)

            con.close()
            return candidates, len(vector_hits)
        except Exception as e:
            log.error(f"Semantic query error: {e}")
            return [], 0


def _parse_syntax_variants(syntax_str: Optional[str]) -> list[str]:
    """Parse syntax_variants from SQLite (comma-delimited or JSON)."""
    if not syntax_str:
        return []
    try:
        parsed = json.loads(syntax_str)
    except (TypeError, ValueError):
        parsed = None

    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]

    delimiter = ";" if ";" in syntax_str else ","
    return [s.strip() for s in syntax_str.split(delimiter) if s.strip()]


def _safe_row_get(row: sqlite3.Row, key: str, default: str = "") -> str:
    """Read an optional column that may not exist on an older/minimal schema."""
    try:
        value = row[key]
    except (IndexError, KeyError):
        return default
    return value or default
