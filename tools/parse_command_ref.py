#!/usr/bin/env python3
"""
parse_command_ref.py — CLI for the CLIQ command-reference parser (Phase 1A).

Reads ONE local Cisco command-reference document and extracts structured
command entries. Runs in preview mode by default so a human can verify
extraction quality before bulk ingestion (Phase 1B/1C).

No execution, no network access — this only reads local files.

USAGE:
  # Preview the first 20 entries + summary report (does not write anything)
  python tools/parse_command_ref.py --file refs/cat9600_iprouting.pdf --platform iosxe

  # Show more entries
  python tools/parse_command_ref.py --file ref.txt --platform iosxr --limit 40

  # Write the full structured result to JSON (for the storage layer)
  python tools/parse_command_ref.py --file ref.docx --platform iosv --out out.json

  # Only entries flagged for review
  python tools/parse_command_ref.py --file ref.txt --platform iosxe --review-only
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Make the src/ tree importable without installing the package.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cliq.ingest import parse_document, format_preview, available_layouts  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Parse a Cisco command-reference document into structured entries."
    )
    ap.add_argument("--file", "-f", required=True, help="Path to the document (.pdf/.docx/.txt)")
    ap.add_argument("--platform", "-p", required=True,
                    help="Platform tag (e.g. iosxe, iosxr, iosv, iosvl2) — source of truth")
    ap.add_argument("--layout", default="cisco_reference",
                    help=f"Layout strategy. Available: {', '.join(available_layouts())}")
    ap.add_argument("--limit", type=int, default=20, help="Preview sample size (default 20)")
    ap.add_argument("--out", help="Write full entries+report to this JSON file")
    ap.add_argument("--review-only", action="store_true",
                    help="In the preview, show only low-confidence/duplicate entries")
    args = ap.parse_args()

    entries, report = parse_document(args.file, args.platform, layout=args.layout)

    # ---- Summary report ----
    print("=" * 70)
    print("EXTRACTION SUMMARY")
    print("=" * 70)
    for line in report.summary_lines():
        print(line)
    print()

    # ---- Preview / spot-check ----
    preview_entries = entries
    if args.review_only:
        preview_entries = [e for e in entries if e.flags or e.confidence == "low"]
        print(f"REVIEW-ONLY PREVIEW ({len(preview_entries)} flagged of {len(entries)} total)")
    else:
        print(f"PREVIEW (first {min(args.limit, len(entries))} of {len(entries)} entries)")
    print("-" * 70)
    print(format_preview(preview_entries, limit=args.limit))
    print()

    # ---- Optional JSON export for the storage layer ----
    if args.out:
        payload = {
            "report": report.to_dict(),
            "entries": [e.to_dict() for e in entries],
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        print(f"Wrote {len(entries)} entries -> {args.out}")

    # Non-zero exit if nothing was extracted, so the pipeline/CI can react.
    return 0 if entries else 1


if __name__ == "__main__":
    raise SystemExit(main())
