#!/usr/bin/env python3
"""
sqlite_store.py — Relational (exact-lookup) store for ingested command entries.

This is CLIQ's deterministic knowledge base: every parsed CommandEntry lands
here as a row with a stable integer id. That id (as ``cmd-<id>``) is the
``vector_id`` used by the Vector DB, so a semantic hit is always traceable back
to its exact relational record.

Design points (Phase 1B/1C):
  - UNIQUE(command, platform): enforces sensible uniqueness while PRESERVING
    legitimate cross-platform duplicates (same command on iosxe vs iosxr).
  - content_hash per row + per document: enables idempotent re-ingestion —
    re-running an unchanged document is a no-op; a changed entry is updated.
  - documents table: tracks what has been ingested (for incremental refresh).
  - ingest_log table: an append-only audit of every dedup/store DECISION, so
    nothing is ever silently dropped.
  - Indexed exact lookups keep latency well under the 50ms target after large
    ingestions.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from typing import Optional

from cliq.ingest.schema import CommandEntry

log = logging.getLogger("cliq.ingest.sqlite_store")

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
)
DEFAULT_KB_DB = os.path.join(_PROJECT_ROOT, "command_kb.sqlite")


def content_hash(entry: CommandEntry) -> str:
    """Stable hash of the meaningful fields of an entry — used to detect whether
    a re-ingested entry actually changed."""
    payload = "\x00".join([
        " ".join(entry.command.lower().split()),
        entry.platform.lower(),
        entry.mode,
        entry.description.strip(),
        "|".join(entry.syntax_variants),
        # Tree / capability tags — a re-tag (e.g. corrected applies_to) must
        # register as a content change so the row is updated, not skipped.
        entry.category,
        entry.protocol,
        entry.capability_req,
        "|".join(entry.applies_to),
        entry.command_type,
        entry.source_pdf,
        # Product-CR provenance — a re-tag (corrected chapter/release) must
        # register as a content change. candidate_status is deliberately
        # EXCLUDED so a device-acceptance promotion survives re-ingestion.
        entry.source_url,
        entry.os_release,
        entry.topic_chapter,
    ])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def vector_id_for(row_id: int) -> str:
    """The canonical Vector-DB id for a SQLite row (keeps the two stores linked)."""
    return f"cmd-{row_id}"


class CommandStore:
    """Thread-safe SQLite store for the ingested command knowledge base."""

    def __init__(self, db_path: str = DEFAULT_KB_DB):
        self.db_path = db_path
        self._lock = threading.RLock()
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS commands (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    command         TEXT NOT NULL,
                    description     TEXT DEFAULT '',
                    syntax_variants TEXT DEFAULT '[]',   -- JSON array
                    mode            TEXT DEFAULT 'exec',
                    platform        TEXT NOT NULL,
                    doc_source      TEXT DEFAULT '',
                    confidence      TEXT DEFAULT 'ok',
                    flags           TEXT DEFAULT '[]',    -- JSON array
                    content_hash    TEXT NOT NULL,
                    vector_id       TEXT DEFAULT '',
                    created_at      REAL NOT NULL,
                    updated_at      REAL NOT NULL,
                    UNIQUE(command, platform)
                );
                CREATE INDEX IF NOT EXISTS idx_cmd_lookup
                    ON commands(command, platform);
                CREATE INDEX IF NOT EXISTS idx_cmd_platform
                    ON commands(platform);
                CREATE INDEX IF NOT EXISTS idx_cmd_doc
                    ON commands(doc_source);

                CREATE TABLE IF NOT EXISTS documents (
                    doc_source    TEXT PRIMARY KEY,
                    platform      TEXT NOT NULL,
                    content_hash  TEXT DEFAULT '',
                    entries_seen  INTEGER DEFAULT 0,
                    entries_stored INTEGER DEFAULT 0,
                    ingested_at   REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ingest_log (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        REAL NOT NULL,
                    doc_source TEXT,
                    command   TEXT,
                    platform  TEXT,
                    decision  TEXT,          -- inserted|updated|unchanged|duplicate_resolved|failed
                    detail    TEXT
                );
                """
            )
            self._migrate_tree_columns()
            self._conn.commit()

    # ------------------------------------------------------------------
    # Tree / capability-tag columns (Track A) — additive migration
    # ------------------------------------------------------------------
    #: New columns added for the tree-structured, capability-tagged KB. They are
    #: nullable with safe defaults so the existing 1713 records remain valid and
    #: queryable; only PDF-ingested nodes populate them with real values.
    _TREE_COLUMNS: tuple[tuple[str, str], ...] = (
        ("category", "TEXT DEFAULT ''"),
        ("protocol", "TEXT DEFAULT ''"),
        ("capability_req", "TEXT DEFAULT ''"),
        ("applies_to", "TEXT DEFAULT '[]'"),     # JSON array of capability classes
        ("command_type", "TEXT DEFAULT 'PI'"),   # PI ingested here; PD reserved (device-only)
        ("source_pdf", "TEXT DEFAULT ''"),
        ("source", "TEXT DEFAULT 'kb'"),         # KB is enrichment only, never authority
        ("os_family", "TEXT DEFAULT 'ios-xe'"),
        # Product Command-Reference (DITA HTML) provenance + two-stage promotion.
        ("source_url", "TEXT DEFAULT ''"),
        ("os_release", "TEXT DEFAULT ''"),
        ("topic_chapter", "TEXT DEFAULT ''"),
        ("candidate_status", "TEXT DEFAULT ''"),  # '' | 'candidate' | 'trusted'
    )

    def _migrate_tree_columns(self) -> None:
        """Add any missing tree-tag columns to an existing commands table.

        Idempotent: only columns absent from the live schema are added, so this
        upgrades a pre-existing KB in place without rebuilding or losing rows.
        """
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(commands)").fetchall()
        }
        for name, decl in self._TREE_COLUMNS:
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE commands ADD COLUMN {name} {decl}"
                )
        self._conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_cmd_protocol     ON commands(protocol);
            CREATE INDEX IF NOT EXISTS idx_cmd_category      ON commands(category);
            CREATE INDEX IF NOT EXISTS idx_cmd_command_type  ON commands(command_type);
            CREATE INDEX IF NOT EXISTS idx_cmd_source_pdf    ON commands(source_pdf);
            CREATE INDEX IF NOT EXISTS idx_cmd_candidate     ON commands(candidate_status);
            """
        )

    # ------------------------------------------------------------------
    # Command upsert (idempotent)
    # ------------------------------------------------------------------
    def get_by_command_platform(self, command: str, platform: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM commands WHERE command=? AND platform=?",
                (" ".join(command.split()), platform),
            ).fetchone()

    def get_by_id(self, row_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM commands WHERE id=?", (row_id,)
            ).fetchone()

    def upsert_command(self, entry: CommandEntry) -> tuple[int, str]:
        """Insert/update a single command entry idempotently.

        Returns (row_id, action) where action is one of:
          - 'inserted'  : new row created
          - 'updated'   : existing row changed (content hash differed)
          - 'unchanged' : identical content already present (idempotent no-op)
        """
        chash = content_hash(entry)
        now = time.time()
        norm_cmd = " ".join(entry.command.split())
        existing = self.get_by_command_platform(norm_cmd, entry.platform)

        with self._lock:
            if existing is None:
                cur = self._conn.execute(
                    """
                    INSERT INTO commands
                      (command, description, syntax_variants, mode, platform,
                       doc_source, confidence, flags, content_hash, vector_id,
                       category, protocol, capability_req, applies_to,
                       command_type, source_pdf, source, os_family,
                       source_url, os_release, topic_chapter, candidate_status,
                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '',
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (norm_cmd, entry.description, json.dumps(entry.syntax_variants),
                     entry.mode, entry.platform, entry.doc_source, entry.confidence,
                     json.dumps(entry.flags), chash,
                     entry.category, entry.protocol, entry.capability_req,
                     json.dumps(entry.applies_to), entry.command_type,
                     entry.source_pdf, entry.source, entry.os_family,
                     entry.source_url, entry.os_release, entry.topic_chapter,
                     entry.candidate_status,
                     now, now),
                )
                row_id = int(cur.lastrowid)
                vid = vector_id_for(row_id)
                self._conn.execute(
                    "UPDATE commands SET vector_id=? WHERE id=?", (vid, row_id)
                )
                self._conn.commit()
                return row_id, "inserted"

            row_id = int(existing["id"])
            if existing["content_hash"] == chash:
                return row_id, "unchanged"   # idempotent: nothing to do

            # NOTE: candidate_status is intentionally NOT overwritten here — a
            # device-confirmed promotion to 'trusted' must survive a re-ingest
            # of the same product-CR chapter (see promote_candidate below).
            self._conn.execute(
                """
                UPDATE commands SET
                  description=?, syntax_variants=?, mode=?, doc_source=?,
                  confidence=?, flags=?, content_hash=?,
                  category=?, protocol=?, capability_req=?, applies_to=?,
                  command_type=?, source_pdf=?, source=?, os_family=?,
                  source_url=?, os_release=?, topic_chapter=?,
                  updated_at=?
                WHERE id=?
                """,
                (entry.description, json.dumps(entry.syntax_variants), entry.mode,
                 entry.doc_source, entry.confidence, json.dumps(entry.flags),
                 chash, entry.category, entry.protocol, entry.capability_req,
                 json.dumps(entry.applies_to), entry.command_type, entry.source_pdf,
                 entry.source, entry.os_family,
                 entry.source_url, entry.os_release, entry.topic_chapter,
                 now, row_id),
            )
            self._conn.commit()
            return row_id, "updated"

    def set_vector_id(self, row_id: int, vector_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE commands SET vector_id=? WHERE id=?", (vector_id, row_id)
            )
            self._conn.commit()

    def delete_by_doc(self, doc_source: str) -> int:
        """Remove all command rows from one document (used for a clean re-ingest
        of an updated reference). Returns number of rows deleted."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM commands WHERE doc_source=?", (doc_source,)
            )
            self._conn.commit()
            return cur.rowcount

    # ------------------------------------------------------------------
    # Document tracking
    # ------------------------------------------------------------------
    def get_document(self, doc_source: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM documents WHERE doc_source=?", (doc_source,)
            ).fetchone()

    def record_document(self, doc_source: str, platform: str, doc_hash: str,
                        entries_seen: int, entries_stored: int) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO documents
                  (doc_source, platform, content_hash, entries_seen, entries_stored, ingested_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(doc_source) DO UPDATE SET
                  platform=excluded.platform, content_hash=excluded.content_hash,
                  entries_seen=excluded.entries_seen, entries_stored=excluded.entries_stored,
                  ingested_at=excluded.ingested_at
                """,
                (doc_source, platform, doc_hash, entries_seen, entries_stored, now),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------
    def log_decision(self, doc_source: str, command: str, platform: str,
                    decision: str, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO ingest_log (ts, doc_source, command, platform, decision, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), doc_source, command, platform, decision, detail),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Aggregates for the validation report
    # ------------------------------------------------------------------
    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM commands").fetchone()[0]

    def count_by_platform(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT platform, COUNT(*) AS n FROM commands GROUP BY platform"
            ).fetchall()
        return {r["platform"]: r["n"] for r in rows}

    def count_low_confidence(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM commands WHERE confidence='low'"
            ).fetchone()[0]

    def count_empty_description(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM commands WHERE TRIM(description)=''"
            ).fetchone()[0]

    def count_missing_vector(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM commands WHERE vector_id=''"
            ).fetchone()[0]

    def sample(self, limit: int = 5) -> list:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, command, description, mode, platform, doc_source, "
                "confidence, flags, vector_id FROM commands ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Tree lookup (capability-gated) — backs cliq.ingest.tree.KnowledgeTree
    # ------------------------------------------------------------------
    def query_nodes(
        self,
        capability_class: Optional[str] = None,
        command_path: Optional[str] = None,
        protocol_hint: Optional[str] = None,
        category: Optional[str] = None,
        command_type: Optional[str] = None,
        prefix: bool = True,
        limit: int = 50,
    ) -> list[dict]:
        """Return tree nodes matching the given filters.

        Capability gating: a node is returned for ``capability_class`` only if
        that class appears in the node's ``applies_to`` list OR the node is
        tagged ``all``. A node with NO ``applies_to`` (legacy/untagged record)
        is treated as universal so the existing KB stays queryable.

        - ``command_path`` exact match, or prefix match when ``prefix`` is True
          (so "show ip ospf" finds "show ip ospf neighbor").
        - ``protocol_hint`` / ``category`` / ``command_type`` are exact filters.
        """
        clauses: list[str] = []
        params: list = []

        if capability_class:
            # JSON array stored as text; match the quoted token or the "all"
            # sentinel, or an empty/untagged applies_to (legacy universal rows).
            clauses.append(
                "(applies_to LIKE ? OR applies_to LIKE ? "
                "OR applies_to IS NULL OR applies_to IN ('', '[]'))"
            )
            params.append(f'%"{capability_class}"%')
            params.append('%"all"%')

        if command_path:
            norm = " ".join(command_path.split())
            if prefix:
                clauses.append("(command = ? OR command LIKE ?)")
                params.append(norm)
                params.append(norm + " %")
            else:
                clauses.append("command = ?")
                params.append(norm)

        if protocol_hint:
            clauses.append("protocol = ?")
            params.append(protocol_hint)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if command_type:
            clauses.append("command_type = ?")
            params.append(command_type)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT id, command, description, syntax_variants, mode, platform, "
            "doc_source, category, protocol, capability_req, applies_to, "
            "command_type, source_pdf, source, os_family, vector_id, "
            "source_url, os_release, topic_chapter, candidate_status "
            "FROM commands" + where + " ORDER BY command LIMIT ?"
        )
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def count_by_protocol(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT protocol, COUNT(*) AS n FROM commands "
                "WHERE protocol != '' GROUP BY protocol"
            ).fetchall()
        return {r["protocol"]: r["n"] for r in rows}

    def count_by_category(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT category, COUNT(*) AS n FROM commands "
                "WHERE category != '' GROUP BY category"
            ).fetchall()
        return {r["category"]: r["n"] for r in rows}

    # ------------------------------------------------------------------
    # Two-stage promotion (Product Command-Reference candidates)
    #
    # product_cr rows are stored source='product_cr', candidate_status=
    # 'candidate' — advisory only ("verify on platform"). A row is promoted to
    # candidate_status='trusted' ONLY when the live device accepts the same
    # command template (see cliq.ingest.candidate_promotion). Never promoted by
    # re-ingestion alone.
    # ------------------------------------------------------------------
    def get_candidates(self, platform: Optional[str] = None, limit: int = 100) -> list[dict]:
        """Return pending product-CR candidates (not yet device-verified)."""
        sql = "SELECT * FROM commands WHERE source='product_cr' AND candidate_status='candidate'"
        params: list = []
        if platform:
            sql += " AND platform=?"
            params.append(platform)
        sql += " ORDER BY command LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def find_candidate_by_template(self, command_template: str, platform: str) -> Optional[dict]:
        """Look up a pending candidate whose (templated) command text matches
        ``command_template`` for ``platform`` — the match key used to promote a
        candidate once the live device accepts the equivalent command."""
        norm = " ".join((command_template or "").split())
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM commands WHERE source='product_cr' "
                "AND candidate_status='candidate' AND platform=? AND command=?",
                (platform, norm),
            ).fetchone()
        return dict(row) if row else None

    def promote_candidate(self, row_id: int) -> bool:
        """Flip one candidate row to candidate_status='trusted'. Idempotent —
        returns False if the row does not exist or is not a pending candidate."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE commands SET candidate_status='trusted' "
                "WHERE id=? AND source='product_cr' AND candidate_status='candidate'",
                (row_id,),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def count_candidates(self) -> dict:
        """candidate/trusted counts for product-CR rows (ingest-report use)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT candidate_status, COUNT(*) AS n FROM commands "
                "WHERE source='product_cr' GROUP BY candidate_status"
            ).fetchall()
        return {r["candidate_status"]: r["n"] for r in rows}

    def find_by_command(self, command: str) -> list[dict]:
        """All rows matching ``command`` text across every platform — used to
        detect a breadth-only match against an existing MCL/PDF ("kb") row
        when ingesting a platform-specific product-CR candidate."""
        norm = " ".join((command or "").split())
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM commands WHERE command=?", (norm,),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
