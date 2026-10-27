#!/usr/bin/env python3
"""
ingest_product_cr.py — CLI: crawl a Cisco product Command-Reference (DITA HTML)
book's TOC and ingest its command chapters into the CLIQ KB as advisory
candidates (source=product_cr).

This is a SEPARATE ingester from the MCL flat-list parser (tools/ingest_mcl.py)
— different HTML structure — but feeds the SAME tag_entries -> embed -> upsert
pipeline / KB as the PDF ingester. Network access happens ONLY in this CLI, at
ingestion time; the runtime advisory/suggestion path never imports this module.

USAGE
  # Crawl a book's TOC and ingest every command chapter as candidates
  python tools/ingest_product_cr.py \\
      --toc-url https://.../catalyst9500/.../command_reference/b_1716_9500_cr.html \\
      --platform cat9500 --os-release 17.16.x

  # Classify only — show which chapters would be crawled/skipped, store nothing
  python tools/ingest_product_cr.py --toc-url <url> --platform cat9500 \\
      --os-release 17.16.x --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cliq.ingest.product_cr_ingest import (  # noqa: E402
    KB_DB,
    VEC_DB,
    ProductCrIngestor,
    default_fetch,
    write_report,
)
from cliq.ingest.product_cr_parser import discover_toc  # noqa: E402
from cliq.ingest.product_cr_taxonomy import classify_chapter  # noqa: E402

_DEFAULT_REPORT = os.path.join(os.path.dirname(KB_DB), "product_cr_ingest_report.json")


def _dry_run(toc_url: str, max_chapters: int | None) -> int:
    html = default_fetch(toc_url)
    entries = discover_toc(html)
    if max_chapters is not None:
        entries = entries[:max_chapters]
    if not entries:
        print("No <ul id=\"bookToc\"> chapters found at this URL.")
        return 1
    print(f"\nBook TOC ({len(entries)} chapter links) — classification (dry-run):")
    print(f"  {'chapter':<48} {'category':<14} {'protocol':<16} applies_to")
    print("  " + "-" * 96)
    for e in entries:
        cls = classify_chapter(e.title)
        if not cls.is_command_chapter:
            print(f"  {e.title:<48} SKIP — {cls.skip_reason}")
            continue
        gate = "[" + ", ".join(cls.applies_to) + "]"
        flag = "" if cls.confident else "  (!)"
        print(f"  {e.title:<48} {cls.category:<14} {cls.protocol:<16} {gate}{flag}")
    return 0


def main() -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--toc-url", required=True, help="Book page containing <ul id=bookToc>")
    ap.add_argument("--platform", required=True, help="e.g. cat9500")
    ap.add_argument("--os-release", required=True, help="e.g. 17.16.x")
    ap.add_argument("--kb-db", default=KB_DB, help=f"SQLite KB path (default: {KB_DB})")
    ap.add_argument("--vector-db", default=VEC_DB, help=f"Vector DB path (default: {VEC_DB})")
    ap.add_argument("--force", action="store_true", help="Ingest even if a chapter is unchanged")
    ap.add_argument("--refresh", action="store_true",
                    help="Clean re-ingest: drop each chapter's prior rows first")
    ap.add_argument("--max-chapters", type=int, default=None,
                    help="Safety cap on number of TOC chapters crawled")
    ap.add_argument("--dry-run", action="store_true",
                    help="Classify TOC chapters only — fetch the TOC page, store nothing")
    ap.add_argument("--report", default=_DEFAULT_REPORT,
                    help=f"JSON report path (default: {_DEFAULT_REPORT})")
    args = ap.parse_args()

    if args.dry_run:
        return _dry_run(args.toc_url, args.max_chapters)

    ingestor = ProductCrIngestor(kb_db_path=args.kb_db, vector_db_path=args.vector_db)
    report = ingestor.crawl_book(
        args.toc_url, platform=args.platform, os_release=args.os_release,
        force=args.force, refresh=args.refresh, max_chapters=args.max_chapters,
    )

    print("\n=== Product-CR ingestion report ===")
    for line in report.summary_lines():
        print(line)

    report_path = write_report(report, ingestor.pipeline.store, args.report)
    print(f"\nWrote JSON report: {report_path}")

    ingestor.close()
    return 0 if report.failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
