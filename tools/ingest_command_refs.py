#!/usr/bin/env python3
"""
ingest_command_refs.py — CLI for the CLIQ storage & ingestion layer (Phase 1B/1C).

Parses one or more local Cisco command-reference documents (via Phase 1A) and
persists them into SQLite + the Vector DB, then prints a validation report and
a post-ingestion spot-check so a human can confirm the knowledge base is ready
for Phase 2 retrieval.

No execution, no device contact, no network — purely local ingestion.

USAGE:
  # Ingest two documents, each with its own platform tag
  python tools/ingest_command_refs.py \
      --doc refs/cat9600_iprouting.pdf:iosxe \
      --doc refs/iosxr_routing.pdf:iosxr

  # Re-ingest is idempotent by default (unchanged docs are skipped).
  # Force a clean refresh of a single updated reference:
  python tools/ingest_command_refs.py --doc refs/cat9600.pdf:iosxe --refresh

  # Choose a deduplication policy and a custom DB location
  python tools/ingest_command_refs.py --doc ref.txt:iosxe \
      --dedup keep-most-complete --kb-db kb.sqlite --vector-db vec.sqlite

  # Try a semantic query against what was ingested
  python tools/ingest_command_refs.py --doc ref.txt:iosxe --search "display routing table"
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cliq.ingest.storage import (  # noqa: E402
    IngestionPipeline,
    DedupPolicy,
    DEFAULT_KB_DB,
    DEFAULT_VECTOR_DB,
    get_embedder,
    available_embedders,
)


def _parse_doc_arg(value: str) -> tuple[str, str]:
    """Split a --doc value of the form 'path:platform'. The path may contain a
    Windows drive letter (C:\\...), so split on the LAST colon."""
    if ":" not in value:
        raise argparse.ArgumentTypeError(
            f"--doc must be 'path:platform' (got '{value}')"
        )
    path, _, platform = value.rpartition(":")
    if not path or not platform:
        raise argparse.ArgumentTypeError(
            f"--doc must be 'path:platform' (got '{value}')"
        )
    return path, platform


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ingest Cisco command-reference docs into SQLite + Vector DB."
    )
    ap.add_argument("--doc", action="append", required=True, type=_parse_doc_arg,
                    metavar="PATH:PLATFORM",
                    help="Document to ingest as 'path:platform' (repeatable)")
    ap.add_argument("--dedup", default=DedupPolicy.KEEP_FIRST.value,
                    choices=[p.value for p in DedupPolicy],
                    help="Duplicate-resolution policy")
    ap.add_argument("--kb-db", default=DEFAULT_KB_DB, help="SQLite knowledge-base path")
    ap.add_argument("--vector-db", default=DEFAULT_VECTOR_DB, help="Vector DB path")
    ap.add_argument(
        "--embedder",
        default="sentence-transformers",
        choices=available_embedders(),
        help="Embedding backend (default: sentence-transformers)",
    )
    ap.add_argument(
        "--embed-model",
        default="all-MiniLM-L6-v2",
        help="Sentence-transformers model name (default: all-MiniLM-L6-v2)",
    )
    ap.add_argument(
        "--local-model-only",
        action="store_true",
        help="Require model to be present locally (no download attempts)",
    )
    ap.add_argument("--force", action="store_true",
                    help="Ingest even if a document is unchanged")
    ap.add_argument("--refresh", action="store_true",
                    help="Clean re-ingest: drop each doc's prior rows first")
    ap.add_argument("--limit", type=int, default=8, help="Spot-check sample size")
    ap.add_argument("--search", help="Run a semantic query after ingestion")
    args = ap.parse_args()

    embedder_kwargs = {}
    if args.embedder == "sentence-transformers":
        embedder_kwargs = {
            "model_name": args.embed_model,
            "local_files_only": args.local_model_only,
        }
    embedder = get_embedder(name=args.embedder, **embedder_kwargs)

    pipe = IngestionPipeline(
        dedup_policy=DedupPolicy(args.dedup),
        kb_db_path=args.kb_db,
        vector_db_path=args.vector_db,
        embedder=embedder,
    )
    try:
        per_doc = []
        for path, platform in args.doc:
            if not os.path.isfile(path):
                print(f"!! Skipping missing file: {path}")
            per_doc.append(
                pipe.ingest_document(path, platform, force=args.force, refresh=args.refresh)
            )
        report = pipe.build_report(per_doc)

        print("=" * 72)
        print("INGESTION VALIDATION REPORT")
        print("=" * 72)
        print("\n".join(report.summary_lines()))
        print()
        print("=" * 72)
        print(f"POST-INGESTION SPOT-CHECK (sample {args.limit})")
        print("=" * 72)
        print(pipe.spot_check(limit=args.limit))

        if args.search:
            print()
            print("=" * 72)
            print(f"SEMANTIC SEARCH: {args.search!r}")
            print("=" * 72)
            for hit in pipe.semantic_search(args.search, top_k=5):
                m = hit.metadata
                print(f"  {hit.score:.3f}  {hit.vector_id} "
                      f"(sqlite_id={m.get('sqlite_id')}) [{m.get('platform')}] {m.get('command')}")

        ready = report.reconciled and report.quality_score >= 70
        print()
        print(f"Knowledge base ready for Phase 2: {'YES' if ready else 'REVIEW NEEDED'} "
              f"(quality {report.quality_score}/100)")
        return 0 if ready else 1
    finally:
        pipe.close()


if __name__ == "__main__":
    raise SystemExit(main())
