#!/usr/bin/env python3
"""
product_cr_ingest.py — orchestrator: Cisco product Command-Reference (DITA
HTML) books -> advisory candidate commands in the CLIQ KB.

Pipeline per book:
    fetch TOC page -> discover_toc -> classify each chapter (product_cr_taxonomy)
        -> skip intro/prose chapters -> fetch each COMMAND chapter
        -> parse_chapter (product_cr_parser) -> tag (category/protocol/
           capability/source/platform/os_release/topic_chapter) -> value-strip
           syntax (command_template) -> dedup-check vs MCL/PDF ("kb") rows
        -> store via the EXISTING IngestionPipeline (source='product_cr',
           candidate_status='candidate') -> per-chapter + book report.

This is a SEPARATE ingester from the MCL flat-list parser (different HTML
structure) — both feed the same tag_entries -> embed -> upsert pipeline.

Two-stage integration: every stored command starts as an advisory CANDIDATE
("from Cisco <platform> CR — verify on platform"); it is promoted to a
trusted suggestion only once the live device accepts the equivalent command
(see :mod:`cliq.ingest.candidate_promotion`). Capability gating still applies
at read time via the existing ``applies_to`` tree lookup.

Offline hot path preserved: fetching happens ONLY here, at ingestion time,
driven by ``tools/ingest_product_cr.py``. Nothing on the suggestion/advisory
hot path imports this module.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional
from urllib.parse import urljoin

from cliq.intelligence.command_template import clean_prefix, extract_template, strip_syntax_notation
from cliq.ingest.product_cr_parser import RawCommand, TocEntry, discover_toc, parse_chapter
from cliq.ingest.product_cr_taxonomy import ChapterClassification, classify_chapter
from cliq.ingest.schema import CONFIDENCE_LOW, CONFIDENCE_OK, CommandEntry
from cliq.ingest.storage.pipeline import DocIngestResult, IngestionPipeline
from cliq.ingest.storage.sqlite_store import DEFAULT_KB_DB

_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
#: Same KB as the PDF ingester — product-CR candidates enrich the one shared KB.
KB_DB = os.path.join(_PROJECT_ROOT, "kb", "cliq_kb.sqlite")
VEC_DB = os.path.join(_PROJECT_ROOT, "kb", "cliq_vec.sqlite")

_DEFAULT_TIMEOUT_S = 15
_USER_AGENT = "cliq-ingest-product-cr/1.0 (offline KB ingestion tool)"

#: Injectable fetch function: url -> html text. Real default hits the network
#: via `requests` (imported lazily so this module has no hard network dep at
#: import time); tests inject a fake fetcher and never touch the network.
FetchFn = Callable[[str], str]


def default_fetch(url: str, timeout: int = _DEFAULT_TIMEOUT_S) -> str:
    """Fetch one URL's HTML. The ONLY place in this feature that makes a live
    web call — used exclusively by the offline ingestion CLI, never the
    advisory/suggestion hot path."""
    import requests

    resp = requests.get(url, timeout=timeout, headers={"User-Agent": _USER_AGENT})
    resp.raise_for_status()
    return resp.text


@dataclass
class ChapterIngestReport:
    """Per-chapter ingestion summary."""

    title: str
    url: str = ""
    category: str = ""
    protocol: str = ""
    capability_req: str = ""
    applies_to: list[str] = field(default_factory=list)
    commands_extracted: int = 0
    candidates_stored: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    dedup_matches: int = 0
    failures: list = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    confident: bool = True

    def summary_line(self) -> str:
        if self.skipped:
            return f"  SKIP {self.title} — {self.skip_reason}"
        tag = f"{self.category}/{self.protocol}"
        gate = "[" + ", ".join(self.applies_to) + "]"
        low_conf = "" if self.confident else "  (LOW-CONFIDENCE classification)"
        return (
            f"  {self.title}  ->  {tag}  cap={self.capability_req} {gate}{low_conf}\n"
            f"      extracted={self.commands_extracted} stored={self.candidates_stored} "
            f"(ins {self.inserted}, upd {self.updated}, unch {self.unchanged}) "
            f"dedup_matches={self.dedup_matches} failures={len(self.failures)}"
        )


@dataclass
class ProductCrIngestReport:
    """Whole-book ingestion summary (Part: ingest-report update)."""

    toc_url: str
    platform: str
    os_release: str
    chapters: list = field(default_factory=list)   # ChapterIngestReport[]

    @property
    def command_chapters_processed(self) -> int:
        return sum(1 for c in self.chapters if not c.skipped)

    @property
    def chapters_skipped(self) -> int:
        return sum(1 for c in self.chapters if c.skipped)

    @property
    def commands_extracted(self) -> int:
        return sum(c.commands_extracted for c in self.chapters)

    @property
    def candidates_stored(self) -> int:
        return sum(c.candidates_stored for c in self.chapters)

    @property
    def dedup_matches(self) -> int:
        return sum(c.dedup_matches for c in self.chapters)

    @property
    def failures(self) -> int:
        return sum(len(c.failures) for c in self.chapters)

    def summary_lines(self) -> list[str]:
        out = [
            f"Book TOC        : {self.toc_url}",
            f"Platform        : {self.platform}   OS release: {self.os_release}",
            f"Chapters        : {len(self.chapters)} "
            f"(command={self.command_chapters_processed}, skipped={self.chapters_skipped})",
            f"Commands        : extracted={self.commands_extracted} "
            f"candidates_stored={self.candidates_stored}",
            f"Dedup vs MCL/PDF: {self.dedup_matches} breadth-only matches "
            f"(kept — product-CR is authoritative for platform-matched detail)",
            f"Failures        : {self.failures}",
            "",
        ]
        out.extend(c.summary_line() for c in self.chapters)
        return out


def _tag_entry(
    entry: CommandEntry, cls: ChapterClassification, *,
    source_url: str, platform: str, os_release: str,
) -> None:
    """Stamp product-CR provenance + capability tags onto one entry (in place)."""
    entry.category = cls.category
    entry.protocol = cls.protocol
    entry.capability_req = cls.capability_req
    entry.applies_to = list(cls.applies_to)
    entry.command_type = "PI"
    entry.os_family = "ios-xe"
    entry.platform = platform
    entry.source = "product_cr"
    entry.source_url = source_url
    entry.os_release = os_release
    entry.topic_chapter = cls.title
    entry.candidate_status = "candidate"   # advisory until device-accepted


def _to_entry(raw: RawCommand, *, doc_source: str) -> CommandEntry:
    """Derive the clean, runnable base command from the raw doc syntax and
    store it as ``command`` (never the bracket/pipe DITA notation — that would
    fail as ``% Invalid input`` if ever inserted). The full notation is kept
    verbatim in ``syntax_variants`` for panel/reference display. Shape-
    recognisable literal values (IP/mask/interface/integer) are then also
    value-stripped via ``command_template`` for consistency with learned
    commands. No help_lookup is available at ingestion time, so only
    shape-recognisable operands are stripped in that second pass."""
    base = strip_syntax_notation(raw.syntax) or raw.syntax
    tmpl = extract_template(base)
    description = raw.description or ""
    return CommandEntry(
        command=tmpl.template,
        description=description,
        syntax_variants=[raw.syntax],
        mode=raw.mode or "exec",
        doc_source=doc_source,
        confidence=CONFIDENCE_OK if description else CONFIDENCE_LOW,
        flags=[] if description else ["empty_description"],
    )


class ProductCrIngestor:
    """Crawl + parse + tag + store one Cisco product Command-Reference book."""

    def __init__(
        self,
        kb_db_path: str = KB_DB,
        vector_db_path: str = VEC_DB,
        pipeline: Optional[IngestionPipeline] = None,
        fetch: FetchFn = default_fetch,
    ):
        self.pipeline = pipeline or IngestionPipeline(
            kb_db_path=kb_db_path, vector_db_path=vector_db_path
        )
        self._fetch = fetch

    # ------------------------------------------------------------------
    def _ingest_chapter(
        self, entry_link: TocEntry, toc_url: str, *,
        platform: str, os_release: str, force: bool, refresh: bool,
    ) -> ChapterIngestReport:
        cls = classify_chapter(entry_link.title)
        report = ChapterIngestReport(
            title=entry_link.title,
            url=urljoin(toc_url, entry_link.href),
            category=cls.category, protocol=cls.protocol,
            capability_req=cls.capability_req, applies_to=list(cls.applies_to),
            confident=cls.confident,
        )
        if not cls.is_command_chapter:
            report.skipped = True
            report.skip_reason = cls.skip_reason
            return report

        try:
            html = self._fetch(report.url)
        except Exception as exc:
            report.failures.append(("<chapter>", f"fetch: {exc}"))
            return report

        try:
            raw_commands = parse_chapter(html)
        except Exception as exc:  # defensive — parser must not raise
            report.failures.append(("<chapter>", f"parse: {exc}"))
            return report

        report.commands_extracted = len(raw_commands)
        entries = [_to_entry(rc, doc_source=report.url) for rc in raw_commands]
        for e in entries:
            _tag_entry(e, cls, source_url=report.url, platform=platform, os_release=os_release)
            # Dedup vs existing MCL/PDF ("kb") rows: compare the CLEAN keyword
            # prefix (up to the first value placeholder) rather than the full
            # templated string, since a breadth-only KB row is typically the
            # bare command name with no argument placeholders at all.
            prefix = clean_prefix(e.command) or e.command
            existing = self.pipeline.store.find_by_command(prefix)
            if not existing and prefix != e.command:
                existing = self.pipeline.store.find_by_command(e.command)
            if any(r.get("source") in ("kb", "mcl") for r in existing):
                report.dedup_matches += 1
                e.flags.append("breadth_match:mcl_pdf")

        result: DocIngestResult = self.pipeline.ingest_entries(
            entries, report.url, platform, force=force, refresh=refresh,
        )
        report.inserted = result.inserted
        report.updated = result.updated
        report.unchanged = result.unchanged
        report.candidates_stored = result.entries_stored
        report.failures.extend(result.failures)
        return report

    def crawl_book(
        self,
        toc_url: str,
        *,
        platform: str,
        os_release: str,
        force: bool = False,
        refresh: bool = False,
        max_chapters: Optional[int] = None,
    ) -> ProductCrIngestReport:
        """Crawl the book's TOC, ingest every command chapter, skip prose ones."""
        report = ProductCrIngestReport(toc_url=toc_url, platform=platform, os_release=os_release)
        try:
            html = self._fetch(toc_url)
        except Exception as exc:
            report.chapters.append(ChapterIngestReport(
                title="<book TOC>", url=toc_url, skipped=True,
                skip_reason=f"fetch: {exc}",
            ))
            return report

        toc_entries = discover_toc(html)
        if max_chapters is not None:
            toc_entries = toc_entries[:max_chapters]

        for entry_link in toc_entries:
            report.chapters.append(
                self._ingest_chapter(
                    entry_link, toc_url, platform=platform, os_release=os_release,
                    force=force, refresh=refresh,
                )
            )
        return report

    def close(self) -> None:
        self.pipeline.store.close()
        self.pipeline.vectors.close()


# ---------------------------------------------------------------------------
# Ingest-report JSON (kb/product_cr_ingest_report.json)
# ---------------------------------------------------------------------------
def build_report_payload(report: ProductCrIngestReport, store) -> dict:
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "toc_url": report.toc_url,
        "platform": report.platform,
        "os_release": report.os_release,
        "source": "product_cr",
        "note": "Advisory candidates only — promoted to trusted on device acceptance.",
        "totals": {
            "chapters": len(report.chapters),
            "chapters_processed": report.command_chapters_processed,
            "chapters_skipped": report.chapters_skipped,
            "commands_extracted": report.commands_extracted,
            "candidates_stored": report.candidates_stored,
            "dedup_matches_mcl_pdf": report.dedup_matches,
            "failures": report.failures,
        },
        "candidate_status_counts": store.count_candidates(),
        "chapters": [asdict(c) for c in report.chapters],
    }


def write_report(report: ProductCrIngestReport, store, path: str) -> str:
    payload = build_report_payload(report, store)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path
