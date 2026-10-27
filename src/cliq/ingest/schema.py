#!/usr/bin/env python3
"""
schema.py — Structured output types for the CLIQ document-ingestion parser.

These dataclasses define the *contract* between the parser (Phase 1A) and the
storage layer (Phase 1B/1C: SQLite + Vector DB). Keeping the schema here, free
of any parsing or I/O logic, lets the storage layer depend on the shape of the
data without depending on how it was produced.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# Confidence levels surfaced per entry so a human can triage before ingestion.
CONFIDENCE_OK = "ok"
CONFIDENCE_LOW = "low"


@dataclass
class CommandEntry:
    """A single structured command extracted from a reference document."""

    command: str                                   # full command syntax, e.g. "show ip route"
    description: str                                # what the command does (multi-line collapsed)
    syntax_variants: list[str] = field(default_factory=list)  # optional-arg forms
    mode: str = "exec"                             # CLI mode; defaults to exec
    platform: str = ""                             # from the supplied platform tag
    doc_source: str = ""                           # originating document filename

    # ---- Tree / capability tags (Track A: tree-structured KB from PDFs) ----
    # These tag a command as a node in the capability-gated knowledge tree. They
    # are populated by the PDF ingestion layer (cliq.ingest.taxonomy); the legacy
    # text/parser path leaves them at their defaults so existing behaviour and the
    # existing KB records are unaffected. The KB is ENRICHMENT ONLY — never the
    # authority — so every ingested node is marked source="kb" / command_type="PI".
    category: str = ""                             # top-level topic, e.g. "ip-routing"
    protocol: str = ""                             # specific protocol/topic, e.g. "ospf"
    capability_req: str = ""                       # routing|switching|services|mgmt|security|qos|none
    applies_to: list[str] = field(default_factory=list)  # capability classes that may use it
    command_type: str = "PI"                       # PI (these PDFs) | PD (reserved; device-only)
    source_pdf: str = ""                           # originating PDF filename (tree keying)
    source: str = "kb"                             # provenance marker — KB is advisory only
    os_family: str = "ios-xe"                      # OS family this node documents

    # ---- Product Command-Reference tags (DITA HTML ingester) ----
    # Populated by cliq.ingest.product_cr_ingest for source="product_cr" nodes.
    # Left at defaults for every other ingestion path (no behaviour change).
    source_url: str = ""                            # chapter URL the command was crawled from
    os_release: str = ""                            # e.g. "17.16.x"
    topic_chapter: str = ""                          # book chapter title, e.g. "IP Routing Commands"
    candidate_status: str = ""                       # "" | "candidate" | "trusted" (two-stage promotion)

    # ---- Quality / review signals (not stored as command data) ----
    confidence: str = CONFIDENCE_OK                # ok | low
    flags: list[str] = field(default_factory=list) # e.g. empty_description, duplicate
    duplicate_of: Optional[int] = None             # index of first occurrence if flagged dup
    source_line: Optional[int] = None              # 1-based line where the command was found

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict (ready for JSON / the storage layer)."""
        return asdict(self)


@dataclass
class ParseReport:
    """Per-document extraction summary for human review."""

    doc_source: str
    platform: str
    lines_total: int = 0                            # raw lines read from the document
    lines_skipped_noise: int = 0                    # headers/footers/page-nums/TOC dropped
    toc_lines_skipped: int = 0                      # lines dropped as Table-of-Contents region
    candidates_rejected: int = 0                    # heading-like lines rejected as TOC/structural noise
    rejected_no_evidence: int = 0                   # rejected: no syntax/section/description
    rejected_section_label: int = 0                 # rejected: bare mode/section label (VLAN, Interface...)
    rejected_descriptive_note: int = 0              # rejected: prose/note (e.g. "... length: /0")
    rejected_empty_heading: int = 0                 # rejected: empty + heading/label/note pattern
    entries_extracted: int = 0
    duplicates_flagged: int = 0
    low_confidence: int = 0
    suspicious_commands: int = 0                    # accepted entries whose command text looks noisy
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary_lines(self) -> list[str]:
        """Human-readable summary, one fact per line."""
        out = [
            f"Document : {self.doc_source}",
            f"Platform : {self.platform}",
            f"Lines read           : {self.lines_total}",
            f"Lines skipped (noise): {self.lines_skipped_noise}",
            f"TOC lines skipped    : {self.toc_lines_skipped}",
            f"Candidates rejected  : {self.candidates_rejected}",
            f"  - no evidence      : {self.rejected_no_evidence}",
            f"  - section label    : {self.rejected_section_label}",
            f"  - descriptive note : {self.rejected_descriptive_note}",
            f"  - empty + heading  : {self.rejected_empty_heading}",
            f"Entries extracted    : {self.entries_extracted}",
            f"Duplicates flagged   : {self.duplicates_flagged}",
            f"Low-confidence entries: {self.low_confidence}",
            f"Suspicious commands  : {self.suspicious_commands}",
        ]
        if self.warnings:
            out.append(f"Warnings ({len(self.warnings)}):")
            out.extend(f"  - {w}" for w in self.warnings)
        return out
