#!/usr/bin/env python3
"""
pipeline.py — Ingestion orchestrator for CLIQ's knowledge base (Phase 1B/1C).

Takes structured CommandEntry objects from the Phase 1A parser and persists
them into BOTH stores — the SQLite command store (exact lookup) and the Vector
DB (semantic search) — with linked ids, a logged deduplication policy, full
per-document statistics, a validation report, and a post-ingestion spot-check.

Guarantees:
  - Storage integrity: every entry either lands in BOTH stores or is recorded
    as an explicit failure in the ingest log + report. No silent loss.
  - Idempotency: re-ingesting an unchanged document is a no-op; re-ingesting a
    changed entry updates in place (never duplicates).
  - Decoupled backends: the orchestrator depends only on the CommandStore /
    VectorStore / Embedder interfaces, so any can be swapped.
  - Partial-failure isolation: one bad document never aborts a multi-doc run.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml

from cliq.ingest.doc_parser import parse_document
from cliq.ingest.schema import CommandEntry
from cliq.ingest.storage.embeddings import Embedder, get_embedder
from cliq.ingest.storage.sqlite_store import (
    CommandStore,
    DEFAULT_KB_DB,
    content_hash,
    vector_id_for,
)
from cliq.ingest.storage.vector_store import (
    SQLiteVectorStore,
    VectorStore,
)

log = logging.getLogger("cliq.ingest.pipeline")

DEFAULT_VECTOR_DB = os.path.join(
    os.path.dirname(DEFAULT_KB_DB), "command_kb_vectors.sqlite"
)

_HIGH_VALUE_COMMANDS = (
    "show ip route",
    "show running-config",
    "show ip interface brief",
    "show version",
)

_CURATED_SHOW_COMMANDS = Path(_PROJECT_ROOT := os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))) / "command_db" / "exec" / "show_commands.yaml"


class DedupPolicy(str, Enum):
    """How to resolve multiple entries that share the same (command, platform).

    Cross-platform duplicates are NEVER collapsed — only same-platform ones.
    """

    KEEP_FIRST = "keep-first"            # first occurrence wins
    KEEP_MOST_COMPLETE = "keep-most-complete"  # longest description wins


@dataclass
class DocIngestResult:
    doc_source: str
    platform: str
    entries_seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates_resolved: int = 0
    low_confidence_stored: int = 0
    failures: list = field(default_factory=list)   # (command, reason)
    skipped_idempotent: bool = False
    warnings: list = field(default_factory=list)

    @property
    def entries_stored(self) -> int:
        return self.inserted + self.updated + self.unchanged


@dataclass
class ValidationReport:
    per_doc: list = field(default_factory=list)        # DocIngestResult[]
    sqlite_total: int = 0
    vector_total: int = 0
    by_platform: dict = field(default_factory=dict)
    low_confidence_total: int = 0
    empty_description_total: int = 0
    missing_vector_total: int = 0
    reconciled: bool = True
    reconcile_detail: str = ""
    quality_score: int = 0
    high_value_present: dict = field(default_factory=dict)
    high_value_missing: list[str] = field(default_factory=list)

    def summary_lines(self) -> list:
        out = [
            "Stores reconciled : " + ("YES" if self.reconciled else f"NO — {self.reconcile_detail}"),
            f"SQLite records    : {self.sqlite_total}",
            f"Vector records    : {self.vector_total}",
            f"Low-confidence    : {self.low_confidence_total}",
            f"Empty description : {self.empty_description_total}",
            f"Missing vectors   : {self.missing_vector_total}",
            f"Quality score     : {self.quality_score}/100",
            "",
            "Per platform:",
        ]
        for plat, n in sorted(self.by_platform.items()):
            out.append(f"  {plat:<12} {n}")
        out.append("")
        out.append("High-value verification:")
        for command, present in self.high_value_present.items():
            out.append(f"  {'OK ' if present else 'MISS'} {command}")
        out.append("")
        out.append("Per document:")
        for d in self.per_doc:
            if d.skipped_idempotent:
                out.append(f"  {d.doc_source} [{d.platform}] — unchanged (skipped, idempotent)")
                continue
            out.append(
                f"  {d.doc_source} [{d.platform}] — seen {d.entries_seen}, "
                f"inserted {d.inserted}, updated {d.updated}, unchanged {d.unchanged}, "
                f"dupes resolved {d.duplicates_resolved}, low-conf {d.low_confidence_stored}, "
                f"failures {len(d.failures)}"
            )
            for cmd, reason in d.failures:
                out.append(f"      FAILED: {cmd} — {reason}")
            for w in d.warnings:
                out.append(f"      warn: {w}")
        return out


class IngestionPipeline:
    """Coordinates parser output -> SQLite + Vector DB with dedup & reporting."""

    def __init__(
        self,
        command_store: Optional[CommandStore] = None,
        vector_store: Optional[VectorStore] = None,
        embedder: Optional[Embedder] = None,
        dedup_policy: DedupPolicy = DedupPolicy.KEEP_FIRST,
        kb_db_path: str = DEFAULT_KB_DB,
        vector_db_path: str = DEFAULT_VECTOR_DB,
    ):
        self.store = command_store or CommandStore(kb_db_path)
        self.vectors = vector_store or SQLiteVectorStore(vector_db_path)
        self.embedder = embedder or get_embedder()
        self.dedup_policy = dedup_policy

    # ------------------------------------------------------------------
    # Embedding text
    # ------------------------------------------------------------------
    @staticmethod
    def _embed_text(entry: CommandEntry) -> str:
        # Semantic key = command + its description (per spec).
        desc = entry.description.strip()
        return f"{entry.command}. {desc}" if desc else entry.command

    # ------------------------------------------------------------------
    # Deduplication (logged, never silent)
    # ------------------------------------------------------------------
    def _resolve_duplicates(
        self, entries: list, result: DocIngestResult
    ) -> list:
        """Collapse same-(command, platform) entries per policy, logging each
        discarded duplicate. Cross-platform duplicates are preserved."""
        groups: dict[tuple, list] = {}
        order: list[tuple] = []
        for e in entries:
            key = (" ".join(e.command.lower().split()), e.platform.lower())
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(e)

        resolved: list = []
        for key in order:
            members = groups[key]
            if len(members) == 1:
                resolved.append(members[0])
                continue
            if self.dedup_policy == DedupPolicy.KEEP_MOST_COMPLETE:
                winner = max(members, key=lambda e: len(e.description))
            else:  # KEEP_FIRST
                winner = members[0]
            resolved.append(winner)
            for e in members:
                if e is winner:
                    continue
                result.duplicates_resolved += 1
                self.store.log_decision(
                    e.doc_source, e.command, e.platform,
                    "duplicate_resolved",
                    f"policy={self.dedup_policy.value}; kept '{winner.command}' "
                    f"(desc_len={len(winner.description)})",
                )
        return resolved

    # ------------------------------------------------------------------
    # Store a single entry into BOTH stores (integrity-guaranteed)
    # ------------------------------------------------------------------
    def _store_entry(self, entry: CommandEntry, result: DocIngestResult) -> None:
        try:
            row_id, action = self.store.upsert_command(entry)
        except Exception as exc:
            result.failures.append((entry.command, f"sqlite: {exc}"))
            self.store.log_decision(entry.doc_source, entry.command, entry.platform,
                                    "failed", f"sqlite: {exc}")
            return

        vid = vector_id_for(row_id)
        try:
            embedding = self.embedder.embed(self._embed_text(entry))
            self.vectors.upsert(
                vid, embedding,
                metadata={
                    "command": entry.command,
                    "platform": entry.platform,
                    "mode": entry.mode,
                    "doc_source": entry.doc_source,
                    "sqlite_id": row_id,
                    # Tree / capability tags — enable protocol-scoped and
                    # capability-gated semantic retrieval (where={'protocol': ...}).
                    "category": entry.category,
                    "protocol": entry.protocol,
                    "capability_req": entry.capability_req,
                    "applies_to": entry.applies_to,
                    "command_type": entry.command_type,
                    "source_pdf": entry.source_pdf,
                    "source": entry.source,
                    "source_url": entry.source_url,
                    "os_release": entry.os_release,
                    "topic_chapter": entry.topic_chapter,
                    "candidate_status": entry.candidate_status,
                },
                document=self._embed_text(entry),
            )
            self.store.set_vector_id(row_id, vid)
        except Exception as exc:
            # SQLite landed but the vector did not — record as an explicit
            # failure so the reconciliation check surfaces it (no silent loss).
            result.failures.append((entry.command, f"vector: {exc}"))
            self.store.log_decision(entry.doc_source, entry.command, entry.platform,
                                    "failed", f"vector: {exc}")
            return

        if action == "inserted":
            result.inserted += 1
        elif action == "updated":
            result.updated += 1
        else:
            result.unchanged += 1
        if entry.confidence == "low":
            result.low_confidence_stored += 1
        self.store.log_decision(entry.doc_source, entry.command, entry.platform,
                                action, f"vector_id={vid}")

    # ------------------------------------------------------------------
    # Ingest already-parsed entries
    # ------------------------------------------------------------------
    @staticmethod
    def _doc_hash(entries: list) -> str:
        h = hashlib.sha1()
        for e in sorted(entries, key=lambda e: (e.command, e.platform)):
            h.update(content_hash(e).encode("utf-8"))
        return h.hexdigest()

    def ingest_entries(
        self, entries: list, doc_source: str, platform: str,
        force: bool = False, refresh: bool = False,
        ensure_high_value: bool = False,
    ) -> DocIngestResult:
        """Persist a list of entries from one document.

        - force=True   : ingest even if the document hash is unchanged.
        - refresh=True : delete this document's prior rows first (clean re-ingest
                         of an updated reference) without touching other docs.
        """
        result = DocIngestResult(doc_source=doc_source, platform=platform,
                                 entries_seen=len(entries))

        if refresh:
            removed_cmd = self.store.delete_by_doc(doc_source)
            removed_vec = self.vectors.delete_by_doc(doc_source)
            result.warnings.append(
                f"refresh: removed {removed_cmd} sqlite + {removed_vec} vector rows"
            )

        doc_hash = self._doc_hash(entries) if entries else ""
        prior = self.store.get_document(doc_source)
        if prior and not force and not refresh and prior["content_hash"] == doc_hash:
            result.skipped_idempotent = True
            log.info("Document '%s' unchanged — skipping (idempotent).", doc_source)
            return result

        if not entries:
            result.warnings.append("Document produced zero entries.")
            self.store.record_document(doc_source, platform, doc_hash, 0, 0)
            return result

        resolved = self._resolve_duplicates(entries, result)
        for entry in resolved:
            self._store_entry(entry, result)

        self.store.record_document(
            doc_source, platform, doc_hash,
            entries_seen=len(entries), entries_stored=result.entries_stored,
        )
        if ensure_high_value:
            self._ensure_high_value_commands(platform)
        return result

    # ------------------------------------------------------------------
    # Ingest a document from disk (parse + store)
    # ------------------------------------------------------------------
    def ingest_document(
        self, path: str, platform: str, force: bool = False, refresh: bool = False,
    ) -> DocIngestResult:
        """Parse one document via Phase 1A then store it. Parser/read failures
        are captured in the result — never raised — so a multi-doc run survives
        a single bad file."""
        doc_source = os.path.basename(path)
        try:
            entries, parse_report = parse_document(path, platform)
        except Exception as exc:  # defensive: parser should not raise, but guard
            result = DocIngestResult(doc_source=doc_source, platform=platform)
            result.failures.append(("<document>", f"parse: {exc}"))
            return result

        result = self.ingest_entries(entries, doc_source, platform,
                                     force=force, refresh=refresh,
                                     ensure_high_value=True)
        result.warnings.extend(parse_report.warnings)
        return result

    def ingest_documents(self, docs: list, force: bool = False) -> ValidationReport:
        """Ingest many (path, platform) documents in one run. A failure in one
        document is isolated and reported; the run continues."""
        per_doc = []
        for path, platform in docs:
            try:
                per_doc.append(self.ingest_document(path, platform, force=force))
            except Exception as exc:  # last-resort isolation
                r = DocIngestResult(doc_source=os.path.basename(path), platform=platform)
                r.failures.append(("<document>", str(exc)))
                per_doc.append(r)
        return self.build_report(per_doc)

    # ------------------------------------------------------------------
    # Validation report + reconciliation + quality score
    # ------------------------------------------------------------------
    def build_report(self, per_doc: list) -> ValidationReport:
        report = ValidationReport(per_doc=per_doc)
        report.sqlite_total = self.store.count()
        report.vector_total = self.vectors.count()
        report.by_platform = self.store.count_by_platform()
        report.low_confidence_total = self.store.count_low_confidence()
        report.empty_description_total = self.store.count_empty_description()
        report.missing_vector_total = self.store.count_missing_vector()
        report.high_value_present = {
            command: self._command_exists(command)
            for command in _HIGH_VALUE_COMMANDS
        }
        report.high_value_missing = [
            command for command, present in report.high_value_present.items() if not present
        ]

        # Consistency check: the two stores should reconcile.
        if report.sqlite_total == report.vector_total and report.missing_vector_total == 0:
            report.reconciled = True
            report.reconcile_detail = ""
        else:
            report.reconciled = False
            report.reconcile_detail = (
                f"sqlite={report.sqlite_total}, vector={report.vector_total}, "
                f"missing_vector={report.missing_vector_total}"
            )

        report.quality_score = self._quality_score(report)
        return report

    @staticmethod
    def _quality_score(report: ValidationReport) -> int:
        total = report.sqlite_total
        if total == 0:
            return 0
        failures = sum(len(d.failures) for d in report.per_doc)
        attempted = total + failures
        low_ratio = report.low_confidence_total / total
        fail_ratio = (failures / attempted) if attempted else 0.0
        missing_high_value = len(report.high_value_missing)
        score = 100.0
        score -= 40.0 * low_ratio      # description quality
        score -= 60.0 * fail_ratio     # storage integrity
        score -= 5.0 * missing_high_value
        if not report.reconciled:
            score -= 20.0              # cross-store consistency
        return max(0, min(100, round(score)))

    # ------------------------------------------------------------------
    # Post-ingestion spot-check (samples BOTH stores)
    # ------------------------------------------------------------------
    def spot_check(self, limit: int = 5) -> str:
        lines = ["SQLite sample:"]
        for row in self.store.sample(limit):
            flags = row.get("flags", "[]")
            lines.append(
                f"  #{row['id']} [{row['platform']}/{row['mode']}] {row['command']} "
                f"-> vector_id={row['vector_id']} conf={row['confidence']} flags={flags}"
            )
        lines.append("High-value commands:")
        for command in _HIGH_VALUE_COMMANDS:
            row = self._lookup_command_row(command)
            if row is None:
                lines.append(f"  MISSING {command}")
            else:
                desc = (row['description'] or '').strip()[:100]
                lines.append(f"  OK {command} -> {desc}")
        lines.append("Vector DB sample (traceable to SQLite id):")
        sampler = getattr(self.vectors, "sample", None)
        if callable(sampler):
            for rec in sampler(limit):
                meta = rec["metadata"]
                lines.append(
                    f"  {rec['vector_id']} -> sqlite_id={meta.get('sqlite_id')} "
                    f"[{meta.get('platform')}] {meta.get('command')}"
                )
        return "\n".join(lines)

    def _lookup_command_row(self, command: str):
        return self.store.get_by_command_platform(command, "iosxe") or self.store.get_by_command_platform(command, "iosxr") or self.store.get_by_command_platform(command, "iosv") or self.store.get_by_command_platform(command, "iosvl2")

    def _command_exists(self, command: str) -> bool:
        return self._lookup_command_row(command) is not None

    def _ensure_high_value_commands(self, platform: str) -> None:
        missing = [command for command in _HIGH_VALUE_COMMANDS if not self._command_exists(command)]
        if not missing:
            return
        curated = self._load_curated_high_value_entries(platform)
        for entry in curated:
            if entry.command not in missing:
                continue
            result = DocIngestResult(doc_source=entry.doc_source, platform=entry.platform)
            self._store_entry(entry, result)
            if result.failures:
                log.warning("High-value backfill failed for %s: %s", entry.command, result.failures)

    def _load_curated_high_value_entries(self, platform: str) -> list[CommandEntry]:
        if not _CURATED_SHOW_COMMANDS.is_file():
            return []
        with _CURATED_SHOW_COMMANDS.open("r", encoding="utf-8") as fh:
            payload = yaml.safe_load(fh) or {}
        out: list[CommandEntry] = []
        for item in payload.get("commands") or []:
            command = str(item.get("full_command", "")).strip()
            if command not in _HIGH_VALUE_COMMANDS:
                continue
            out.append(
                CommandEntry(
                    command=command,
                    description=str(item.get("description", "")).strip(),
                    syntax_variants=list(item.get("examples") or []),
                    mode="enable" if command == "show running-config" else str(item.get("mode", "exec")).strip() or "exec",
                    platform=platform,
                    doc_source="curated_show_commands.yaml",
                )
            )
        return out

    # ------------------------------------------------------------------
    def semantic_search(self, query: str, top_k: int = 5,
                        platform: Optional[str] = None) -> list:
        """Convenience: embed a query and search the vector store, returning
        hits whose ids link straight back to SQLite rows."""
        emb = self.embedder.embed(query)
        where = {"platform": platform} if platform else None
        return self.vectors.query(emb, top_k=top_k, where=where)

    def close(self) -> None:
        self.store.close()
        self.vectors.close()






