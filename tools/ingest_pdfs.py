#!/usr/bin/env python3
"""
ingest_pdfs.py — CLI for Track A: ingest IOS-XE command-ref PDFs into the
tree-structured, capability-tagged KB (extends the Phase 1B pipeline).

Each command becomes one capability-tagged tree node (category/protocol +
applies_to gating). The KB is enrichment only — every node is PI / source=kb;
the live device is always the source of truth.

USAGE
  # Auto-classify and ingest a single PDF (branch inferred from filename)
  python tools/ingest_pdfs.py --pdf refs/ospf_cmd_ref.pdf

  # Force an explicit branch (for cryptically-named docs) — applies to all --pdf
  python tools/ingest_pdfs.py --pdf refs/b_xe_17_cr.pdf --category ip-routing --protocol ospf

  # Ingest an entire folder in ingestion-priority order (routing → debug → …)
  python tools/ingest_pdfs.py --dir refs/iosxe_command_refs

  # Classify only — no storage (verify branch placement before committing)
  python tools/ingest_pdfs.py --dir refs/ --dry-run

  # Verify the tree after ingest
  python tools/ingest_pdfs.py --pdf refs/ospf_cmd_ref.pdf --verify ospf
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cliq.ingest.pdf_ingest import (  # noqa: E402
    KB_DB,
    VEC_DB,
    PdfTreeIngestor,
    discover_specs,
    write_report,
)
from cliq.ingest.taxonomy import classify_pdf  # noqa: E402

_DEFAULT_REPORT = os.path.join(os.path.dirname(KB_DB), "ingest_report.json")


def _print_classification_table(paths: list[str], category, protocol) -> None:
    print("\nClassification (dry-run — nothing stored):")
    print(f"  {'PDF':<42} {'category':<14} {'protocol':<18} applies_to")
    print("  " + "-" * 92)
    for p in paths:
        cls = classify_pdf(os.path.basename(p), category=category, protocol=protocol)
        if cls.skip:
            print(f"  {os.path.basename(p):<42} SKIP — {cls.skip_reason}")
            continue
        gate = "[" + ", ".join(cls.applies_to) + "]"
        flag = "" if cls.confident else "  (!)"
        print(f"  {os.path.basename(p):<42} {cls.category:<14} {cls.protocol:<18} {gate}{flag}")


def main() -> int:
    # Windows consoles default to cp1252, which cannot encode the report's
    # arrows (→) and would crash mid-run with UnicodeEncodeError. Force UTF-8 on
    # the output streams so summary/report printing is always safe.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", action="append", default=[], metavar="PATH",
                    help="PDF to ingest (repeatable). Auto-classified unless "
                         "--category/--protocol given.")
    ap.add_argument("--dir", help="Folder to recursively scan for *.pdf (auto-classified)")
    ap.add_argument("--category", help="Force category for all --pdf args")
    ap.add_argument("--protocol", help="Force protocol for all --pdf args")
    ap.add_argument("--kb-db", default=KB_DB, help=f"SQLite KB path (default: {KB_DB})")
    ap.add_argument("--vector-db", default=VEC_DB, help=f"Vector DB path (default: {VEC_DB})")
    ap.add_argument("--force", action="store_true", help="Ingest even if a doc is unchanged")
    ap.add_argument("--refresh", action="store_true",
                    help="Clean re-ingest: drop each doc's prior rows first")
    ap.add_argument("--dry-run", action="store_true",
                    help="Classify only — show branch placement, store nothing")
    ap.add_argument("--report", default=_DEFAULT_REPORT,
                    help=f"Per-PDF JSON report path (default: {_DEFAULT_REPORT})")
    ap.add_argument("--verify", metavar="PROTOCOL",
                    help="After ingest, show tree.lookup(protocol_hint=...) sample")
    args = ap.parse_args()

    # Build the spec list: explicit --pdf (with optional forced tags) + --dir.
    specs: list[tuple[str, str | None, str | None]] = [
        (p, args.category, args.protocol) for p in args.pdf
    ]
    dir_paths: list[str] = []
    if args.dir:
        dir_specs = discover_specs(args.dir)
        dir_paths = [s[0] for s in dir_specs]
        specs += dir_specs

    if not specs:
        ap.error("provide at least one --pdf or --dir")

    all_paths = [s[0] for s in specs]
    if args.dry_run:
        _print_classification_table(all_paths, args.category, args.protocol)
        return 0

    missing = [p for p in all_paths if not os.path.isfile(p)]
    for p in missing:
        print(f"!! missing file: {p}")

    ingestor = PdfTreeIngestor(kb_db_path=args.kb_db, vector_db_path=args.vector_db)
    reports = ingestor.ingest_paths(specs, force=args.force, refresh=args.refresh)

    print("\n=== Per-PDF ingestion report ===")
    total_cmds = total_nodes = total_embedded = 0
    for r in reports:
        for line in r.summary_lines():
            print(line)
        total_cmds += r.commands_extracted
        total_nodes += r.nodes_stored
        total_embedded += r.embedded_count

    print("\n=== Totals ===")
    print(f"  PDFs processed   : {len(reports)}")
    print(f"  Commands extracted: {total_cmds}")
    print(f"  Nodes stored      : {total_nodes}")
    print(f"  Embedded          : {total_embedded}")

    tree = ingestor.tree()
    print("\n=== Tree branches (protocol → node count) ===")
    for proto, n in sorted(tree.protocols().items()):
        print(f"  {proto:<22} {n}")

    report_path = write_report(reports, tree, args.report)
    print(f"\nWrote per-PDF JSON report: {report_path}")

    if args.verify:
        print(f"\n=== Verify: tree.lookup(router, protocol_hint='{args.verify}') ===")
        nodes = tree.lookup("router", protocol_hint=args.verify, limit=10)
        for node in nodes:
            print(f"  {node.command_path:<40} {node.category}/{node.protocol} "
                  f"{node.applies_to}")
        if not nodes:
            print("  (no nodes — check classification/ingest)")

    ingestor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
