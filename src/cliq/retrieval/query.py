#!/usr/bin/env python3
"""
query.py — Main retrieval logic: query execution, hybrid ranking, result assembly.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
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
        self._validate_embedder_compatibility()

    def _validate_embedder_compatibility(self) -> None:
        """Ensure the embedder dimension and model space match the vector index."""
        if hasattr(self.vector_store, "get_dimension"):
            stored_dim = self.vector_store.get_dimension()
            if stored_dim is not None and stored_dim > 0:
                embedder_dim = getattr(self.embedder, "dim", None)
                if embedder_dim is not None and embedder_dim != stored_dim:
                    raise ValueError(
                        f"Incompatible embedder: embedder '{getattr(self.embedder, 'name', '')}' produces {embedder_dim}-dim vectors, "
                        f"but vector index requires {stored_dim}-dim vectors. "
                        f"Expected compatible model (e.g. 'all-MiniLM-L6-v2' for 384-dim index)."
                    )
                if stored_dim == 384 and getattr(self.embedder, "name", "") == "local-hashing":
                    raise ValueError(
                        "Cannot query neural 384-dimensional vector index with local hashing embedder. "
                        "Neural embeddings require a compatible SentenceTransformer model."
                    )

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

        # Domain-specific relevance correction:
        # If user is asking for standard VLAN access port / switchport configuration,
        # demote 802.1X access-session commands and boost switchport access vlan commands.
        input_lower = q.input_text.lower()
        if any(term in input_lower for term in ("access port", "access-port", "vlan access", "switchport mode access")):
            for cand in all_candidates:
                c_cmd = cand.command.lower()
                if "switchport access vlan" in c_cmd or "switchport mode" in c_cmd:
                    cand.confidence = min(1.0, cand.confidence + 0.35)
                elif any(k in c_cmd for k in ("access-session", "dot1x", "authentication")):
                    cand.confidence = max(0.0, cand.confidence - 0.40)

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


# ---------------------------------------------------------------------------
# Shared Singleton Retriever Provider (for Copilot and Advisory integration)
# ---------------------------------------------------------------------------
_shared_retriever: Optional[CommandRetriever] = None
_retriever_lock = threading.Lock()


def get_shared_retriever(
    kb_path: Optional[str] = None,
    vec_path: Optional[str] = None,
    embedder: Optional[Embedder] = None,
    force_reload: bool = False,
) -> Optional[CommandRetriever]:
    """Retrieve or initialize the shared singleton CommandRetriever."""
    global _shared_retriever
    with _retriever_lock:
        if _shared_retriever is not None and not force_reload:
            return _shared_retriever

        from pathlib import Path
        from cliq.ingest.storage.sqlite_store import CommandStore
        from cliq.ingest.storage.vector_store import SQLiteVectorStore

        kb = kb_path or os.environ.get("CLIQ_KB_DB", "./kb/cliq_kb.sqlite")
        vec = vec_path or os.environ.get("CLIQ_VEC_DB", "./kb/cliq_vec.sqlite")

        if not Path(kb).exists() or not Path(vec).exists():
            log.info("Knowledge base files not found (%s, %s); shared retriever inactive.", kb, vec)
            return None

        try:
            sqlite_store = CommandStore(kb)
            vector_store = SQLiteVectorStore(vec)
            emb = embedder or get_embedder()
            _shared_retriever = CommandRetriever(sqlite_store, vector_store, embedder=emb)
            log.info(
                "Initialized shared CommandRetriever with KB=%s VEC=%s (records=%d, dim=%s)",
                kb, vec, vector_store.count(), vector_store.get_dimension(),
            )
            return _shared_retriever
        except Exception as exc:
            log.warning("Failed to initialize shared CommandRetriever: %s", exc)
            return None


def set_shared_retriever(retriever: Optional[CommandRetriever]) -> None:
    """Explicitly set or reset the shared singleton CommandRetriever."""
    global _shared_retriever
    with _retriever_lock:
        _shared_retriever = retriever


def validate_citation(candidate: RetrievalCandidate) -> tuple[bool, str]:
    """Validate that a candidate citation points to a genuine, non-fabricated source
    and is strictly relevant to the command it claims to document."""
    if not candidate.doc_source or not candidate.doc_source.strip():
        return False, "Candidate citation missing doc_source reference."

    src = candidate.doc_source.strip()
    if src.startswith("http://") or src.startswith("https://"):
        if not any(domain in src.lower() for domain in ("cisco.com", "juniper.net", "arista.com")):
            return False, f"Fabricated or untrusted citation URL domain: {src}"

    if not candidate.platform or not candidate.platform.strip():
        return False, "Citation missing platform metadata."

    if not candidate.cmd_mode or not candidate.cmd_mode.strip():
        return False, "Citation missing CLI mode metadata."

    # Citation / source semantic mismatch check
    cmd_lower = candidate.command.lower()
    src_lower = src.lower()
    if "ospf" in cmd_lower and "bgp_command_reference" in src_lower and "ospf" not in src_lower:
        return False, f"Citation source mismatch: command '{candidate.command}' does not match BGP source '{src}'"

    # Reject 802.1X access-session documentation for standard VLAN switchport commands
    if any(k in cmd_lower for k in ("access-session", "dot1x", "authentication host-mode")) and any(k in src_lower for k in ("ibns", "security")):
        return False, f"Citation relevance error: '{candidate.command}' documents 802.1X security sessions, not standard VLAN access-port configuration."

    return True, "Citation verified."
