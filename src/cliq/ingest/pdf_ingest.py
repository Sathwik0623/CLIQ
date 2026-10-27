#!/usr/bin/env python3
"""
pdf_ingest.py — Track A orchestrator: IOS-XE command-ref PDFs → tree-structured KB.

Pipeline per PDF:
    classify (taxonomy)  →  parse per-command (doc_parser, Phase 1A)
        →  tag each entry with tree/capability tags
        →  store into SQLite + Vector DB (existing IngestionPipeline)
        →  emit observability records + a per-PDF ingestion report.

This EXTENDS the existing Phase 1B pipeline — it does not replace it. The parser
already chunks Cisco command-reference docs at the per-command level (one node =
one command, multi-page entries handled), so this layer only adds branch
placement + capability gating + reporting on top.

The KB is **enrichment only**: every ingested node is ``command_type="PI"`` /
``source="kb"``. ``PD`` commands are never ingested here (device ``?``-help only).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from cliq.ingest.doc_parser import parse_document
from cliq.ingest.schema import CommandEntry
from cliq.ingest.text_repair import count_protected_acronyms
from cliq.ingest.storage.pipeline import (
    DEFAULT_VECTOR_DB,
    DocIngestResult,
    IngestionPipeline,
)
from cliq.ingest.storage.sqlite_store import DEFAULT_KB_DB
from cliq.ingest.taxonomy import (
    CATEGORY_CAPABILITY,
    PdfClassification,
    classify_pdf,
    protocol_for_command,
)
from cliq.ingest.tree import KnowledgeTree
from cliq.utils.observability import log_event

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
#: Default to the real, populated KB (1713 records) — extend, don't replace.
KB_DB = os.path.join(_PROJECT_ROOT, "kb", "cliq_kb.sqlite")
VEC_DB = os.path.join(_PROJECT_ROOT, "kb", "cliq_vec.sqlite")

_PDF_PLATFORM = "iosxe"   # these are all IOS-XE references


@dataclass
class PdfIngestReport:
    """Per-PDF ingestion summary (Part: ingestion report per PDF)."""

    source_pdf: str
    category: str = ""
    protocol: str = ""
    capability_req: str = ""
    applies_to: list[str] = field(default_factory=list)
    commands_extracted: int = 0
    nodes_stored: int = 0
    embedded_count: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    failures: list = field(default_factory=list)   # (command, reason)
    parse_warnings: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    confident: bool = True

    def summary_lines(self) -> list[str]:
        if self.skipped:
            return [f"  SKIP {self.source_pdf} — {self.skip_reason}"]
        tag = f"{self.category}/{self.protocol}"
        gate = "[" + ", ".join(self.applies_to) + "]"
        out = [
            f"  {self.source_pdf}  →  {tag}  cap={self.capability_req} {gate}"
            + ("" if self.confident else "  (LOW-CONFIDENCE classification)"),
            f"      commands extracted : {self.commands_extracted}",
            f"      nodes stored       : {self.nodes_stored} "
            f"(ins {self.inserted}, upd {self.updated}, unch {self.unchanged})",
            f"      embedded           : {self.embedded_count}",
        ]
        if self.failures:
            out.append(f"      parse/store failures: {len(self.failures)}")
            for cmd, reason in self.failures[:5]:
                out.append(f"        - {cmd}: {reason}")
        for w in self.parse_warnings[:5]:
            out.append(f"      warn: {w}")
        return out


def tag_entries(entries: list[CommandEntry], cls: PdfClassification, source_pdf: str) -> None:
    """Stamp tree/capability tags onto every parsed entry (in place).

    Capability gating is applied UNIFORMLY from the PDF's classification, so an
    entire branch is tagged consistently (critical for the lan-switching gate).
    The *protocol*, however, is derived PER COMMAND for multi-family branches
    (lan-switching mixes vlan / spanning-tree / trunk / etherchannel / switchport),
    so protocol sub-branches stay meaningful; single-topic PDFs keep the PDF-level
    protocol unchanged.
    """
    for e in entries:
        e.category = cls.category
        e.protocol = protocol_for_command(cls.category, e.command, cls.protocol)
        e.capability_req = cls.capability_req
        e.applies_to = list(cls.applies_to)
        e.command_type = cls.command_type     # always "PI" for these PDFs
        e.os_family = cls.os_family
        e.source = cls.source                 # "kb" — enrichment only
        e.source_pdf = source_pdf


class PdfTreeIngestor:
    """Ingest classified PDFs into the tree-structured, capability-tagged KB."""

    def __init__(
        self,
        kb_db_path: str = KB_DB,
        vector_db_path: str = VEC_DB,
        pipeline: Optional[IngestionPipeline] = None,
    ):
        self.pipeline = pipeline or IngestionPipeline(
            kb_db_path=kb_db_path, vector_db_path=vector_db_path
        )

    # ------------------------------------------------------------------
    def ingest_pdf(
        self,
        path: str,
        *,
        category: Optional[str] = None,
        protocol: Optional[str] = None,
        force: bool = False,
        refresh: bool = False,
    ) -> PdfIngestReport:
        """Classify, parse, tag, and store one PDF. Never raises — a bad file is
        captured in the report so a batch run survives it."""
        source_pdf = os.path.basename(path)
        cls = classify_pdf(source_pdf, category=category, protocol=protocol)

        if cls.skip:
            log_event("ingest", "pdf_parse", "classify", "skipped",
                      source_pdf=source_pdf, detail=cls.skip_reason)
            return PdfIngestReport(source_pdf=source_pdf, skipped=True,
                                   skip_reason=cls.skip_reason, confident=cls.confident)

        report = PdfIngestReport(
            source_pdf=source_pdf,
            category=cls.category,
            protocol=cls.protocol,
            capability_req=cls.capability_req,
            applies_to=list(cls.applies_to),
            confident=cls.confident,
        )

        # ---- Parse (per-command chunking) -------------------------------
        try:
            entries, parse_report = parse_document(path, _PDF_PLATFORM)
        except Exception as exc:   # defensive; parser is meant not to raise
            report.failures.append(("<document>", f"parse: {exc}"))
            log_event("ingest", "pdf_parse", "read", "error",
                      source_pdf=source_pdf, category=cls.category,
                      protocol=cls.protocol, detail=str(exc))
            return report

        report.commands_extracted = len(entries)
        report.parse_warnings = list(parse_report.warnings)
        log_event(
            "ingest", "pdf_parse", "complete",
            "success" if entries else "empty",
            source_pdf=source_pdf, category=cls.category, protocol=cls.protocol,
            commands_extracted=len(entries),
            detail=f"{cls.category}/{cls.protocol} cap={cls.capability_req} "
                   f"applies_to={cls.applies_to}",
        )

        # ---- Tag + store -------------------------------------------------
        tag_entries(entries, cls, source_pdf)

        # Observability: how many command tokens the acronym guard kept atomic
        # (e.g. "eigrp" never degrading to "ei grp"), so re-ingests are auditable.
        acronyms_protected = sum(count_protected_acronyms(e.command) for e in entries)
        commands_corrected = sum(
            1 for e in entries if count_protected_acronyms(e.command) > 0
        )
        log_event(
            "ingest", "text_repair", "scan", "success",
            source_pdf=source_pdf,
            acronyms_protected=acronyms_protected,
            commands_corrected=commands_corrected,
            detail=f"{cls.category}/{cls.protocol} protected {acronyms_protected} "
                   f"acronym token(s) across {commands_corrected} command(s)",
        )

        result: DocIngestResult = self.pipeline.ingest_entries(
            entries, source_pdf, _PDF_PLATFORM, force=force, refresh=refresh,
        )

        report.inserted = result.inserted
        report.updated = result.updated
        report.unchanged = result.unchanged
        report.nodes_stored = result.entries_stored
        report.embedded_count = result.entries_stored   # every stored node is embedded
        report.failures.extend(result.failures)

        log_event(
            "ingest", "node_store", "complete",
            "success" if not result.failures else "partial",
            source_pdf=source_pdf, category=cls.category, protocol=cls.protocol,
            commands_extracted=report.commands_extracted,
            embedded_count=report.embedded_count,
            detail=(f"inserted={result.inserted} updated={result.updated} "
                    f"unchanged={result.unchanged} failures={len(result.failures)}"),
        )
        return report

    # ------------------------------------------------------------------
    def ingest_paths(
        self,
        specs: list[tuple[str, Optional[str], Optional[str]]],
        *,
        force: bool = False,
        refresh: bool = False,
    ) -> list[PdfIngestReport]:
        """Ingest many (path, category, protocol) specs in ingestion-priority order.

        ``category``/``protocol`` may be ``None`` to auto-classify. Documents are
        sorted so demo-critical branches (IP routing → debug → …) land first.
        """
        # Pre-classify to sort by priority without committing anything.
        annotated = []
        for path, cat, proto in specs:
            cls = classify_pdf(os.path.basename(path), category=cat, protocol=proto)
            annotated.append((cls.priority, path, cat, proto))
        annotated.sort(key=lambda t: t[0])

        reports: list[PdfIngestReport] = []
        for _, path, cat, proto in annotated:
            if not os.path.isfile(path):
                reports.append(PdfIngestReport(
                    source_pdf=os.path.basename(path), skipped=True,
                    skip_reason="file not found"))
                continue
            reports.append(self.ingest_pdf(path, category=cat, protocol=proto,
                                           force=force, refresh=refresh))
        return reports

    def tree(self) -> KnowledgeTree:
        """A :class:`KnowledgeTree` view sharing this ingestor's command store."""
        return KnowledgeTree(store=self.pipeline.store)

    def close(self) -> None:
        self.pipeline.store.close()
        self.pipeline.vectors.close()


def discover_pdfs(root: str) -> list[str]:
    """Recursively collect ``*.pdf`` paths under ``root`` (sorted)."""
    found: list[str] = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if name.lower().endswith(".pdf"):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


def infer_category_from_path(path: str) -> Optional[str]:
    """Return the category implied by a path component, if any.

    The on-disk layout ``kb/sources/ios-xe/<category>/<file>.pdf`` makes the
    parent folder the AUTHORITATIVE category (e.g. a debug PDF named
    ``debug_a-d.pdf`` lives under ``debug/``). When a path component exactly
    matches a known category, it wins over filename auto-detection so an entire
    branch is tagged consistently — critical for the gated ``lan-switching``
    folder.
    """
    parts = {p.strip().lower() for p in path.replace("\\", "/").split("/")}
    for part in parts:
        if part in CATEGORY_CAPABILITY:
            return part
    return None


def discover_specs(root: str) -> list[tuple[str, Optional[str], Optional[str]]]:
    """Discover PDFs under ``root`` with category inferred from their folder."""
    return [(p, infer_category_from_path(p), None) for p in discover_pdfs(root)]


def build_report_payload(reports: list[PdfIngestReport], tree: KnowledgeTree) -> dict:
    """Assemble the machine-readable ingestion report (kb/ingest_report.json)."""
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "os_family": "ios-xe",
        "command_type": "PI",
        "source": "kb",
        "note": "KB is enrichment only — the live device is the source of truth.",
        "totals": {
            "pdfs_processed": len(reports),
            "pdfs_skipped": sum(1 for r in reports if r.skipped),
            "commands_extracted": sum(r.commands_extracted for r in reports),
            "nodes_stored": sum(r.nodes_stored for r in reports),
            "embedded": sum(r.embedded_count for r in reports),
            "failures": sum(len(r.failures) for r in reports),
        },
        "branches": {
            "by_category": tree.categories(),
            "by_protocol": tree.protocols(),
        },
        "per_pdf": [asdict(r) for r in reports],
    }


def write_report(reports: list[PdfIngestReport], tree: KnowledgeTree, path: str) -> str:
    """Write the per-PDF ingestion report to ``path`` as JSON. Returns the path."""
    payload = build_report_payload(reports, tree)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path
