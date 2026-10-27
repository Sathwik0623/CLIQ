#!/usr/bin/env python3
"""
test_retrieval.py — Interactive query preview/test tool for the retrieval layer.

Loads the knowledge base and allows interactive querying to validate retrieval
performance before deploying to the actual CLI.
"""

import argparse
import os
import sys
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_XET"] = "1"

from cliq.ingest.storage.pipeline import IngestionPipeline
from cliq.ingest.storage.sqlite_store import CommandStore
from cliq.ingest.storage.vector_store import SQLiteVectorStore
from cliq.retrieval import (
    CommandRetriever,
    IntelligentCommandAssistant,
    IntelligentRetrievalConfig,
    RetrievalQuery,
)


def load_stores(kb_path: str, vec_path: str) -> tuple[CommandStore, SQLiteVectorStore]:
    """Load SQLite and Vector DB stores."""
    sqlite_store = CommandStore(kb_path)
    vector_store = SQLiteVectorStore(vec_path)
    return sqlite_store, vector_store


def format_candidate(i: int, candidate, max_desc_len: int = 100) -> str:
    """Format a single candidate for display."""
    desc = candidate.description[:max_desc_len]
    if len(candidate.description) > max_desc_len:
        desc += "..."
    conf_pct = int(candidate.confidence * 100)
    return (
        f"  {i}. [{conf_pct}% {candidate.match_type}] {candidate.command}\n"
        f"     mode={candidate.cmd_mode} platform={candidate.platform}\n"
        f"     desc: {desc}\n"
    )


def format_intelligent_result(i: int, result, max_desc_len: int = 100) -> str:
    """Format a single intelligent response result for display."""
    desc = result.description[:max_desc_len]
    if len(result.description) > max_desc_len:
        desc += "..."
    conf_pct = int(result.confidence_score * 100)
    return (
        f"  {i}. [{conf_pct}% {result.response_tier}/{result.match_type}] {result.command}\n"
        f"     mode={result.mode} platform={result.platform}\n"
        f"     source: {result.source_attribution or 'unknown'}\n"
        f"     desc: {desc}\n"
    )


def interactive_mode(retriever: CommandRetriever, assistant: IntelligentCommandAssistant) -> None:
    """Run interactive query loop."""
    print("\n=== Retrieval Query Preview ===")
    print("Commands:")
    print("  <query>              - Query with intelligent suggestions/discovery")
    print("  --intent <query>     - Force semantic intent mode")
    print("  --exact <query>      - Force exact/prefix mode")
    print("  --raw <query>        - Show raw retrieval output without the assistant layer")
    print("  --platform <p> <q>   - Query with platform filter (iosxe/iosxr/iosv/iosvl2)")
    print("  --mode <m> <q>       - Query with mode filter (exec/config/config-if/enable)")
    print("  --top-k <k> <q>      - Set result count")
    print("  --threshold <t> <q>  - Set confidence threshold (0–1)")
    print("  --preview-tier       - Run the built-in Phase 3.5 sample suite")
    print("  help                 - Show this help")
    print("  quit, exit           - Exit\n")

    while True:
        try:
            user_input = input("> ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("help", "--help", "-h"):
                print(
                    "Queries can use flags: --intent, --exact, --platform, --mode, --top-k, --threshold\n"
                    "Example: --platform iosxe --top-k 10 show multicast routing\n"
                )
                continue
            if user_input.lower() in ("quit", "exit"):
                print("Goodbye!")
                break

            # Parse flags and query text.
            parts = user_input.split()
            mode_override = None
            platform_override = None
            mode_override_cmd = None
            top_k_override = 5
            threshold_override = 0.6
            raw_mode = False
            query_text = ""

            i = 0
            while i < len(parts):
                if parts[i] == "--intent":
                    mode_override = "intent"
                    i += 1
                elif parts[i] == "--exact":
                    mode_override = "exact"
                    i += 1
                elif parts[i] == "--raw":
                    raw_mode = True
                    i += 1
                elif parts[i] == "--preview-tier":
                    run_preview_suite(assistant)
                    query_text = ""
                    break
                elif parts[i] == "--platform" and i + 1 < len(parts):
                    platform_override = parts[i + 1]
                    i += 2
                elif parts[i] == "--mode" and i + 1 < len(parts):
                    mode_override_cmd = parts[i + 1]
                    i += 2
                elif parts[i] == "--top-k" and i + 1 < len(parts):
                    try:
                        top_k_override = int(parts[i + 1])
                        i += 2
                    except ValueError:
                        i += 1
                elif parts[i] == "--threshold" and i + 1 < len(parts):
                    try:
                        threshold_override = float(parts[i + 1])
                        i += 2
                    except ValueError:
                        i += 1
                else:
                    query_text = " ".join(parts[i:])
                    break

            if not query_text:
                print("No query text provided.")
                continue

            # Build and execute query.
            q = RetrievalQuery(
                input_text=query_text,
                mode=mode_override or "auto",  # type: ignore
                platform=platform_override,
                cmd_mode=mode_override_cmd,
                top_k=top_k_override,
                min_confidence=threshold_override,
            )
            try:
                q.validate()
            except ValueError as e:
                print(f"Invalid query: {e}")
                continue

            if raw_mode:
                result = retriever.query(q)
                print()
                print(f"Query: {result.query.input_text}")
                print(f"Mode: {result.query_mode_used} | Considered: {result.total_candidates_considered}")
                print(f"Latency: {result.execution_time_ms:.1f}ms | Summary: {result.summary()}")
                print()
                if not result.candidates:
                    print(f"No candidates ({result.reason_empty})")
                else:
                    print("Results:")
                    for idx, candidate in enumerate(result.candidates, 1):
                        print(format_candidate(idx, candidate))
            else:
                response = assistant.respond(q)
                print()
                print(f"Query: {response.query.input_text}")
                print(
                    f"Tier: {response.response_tier} | Mode: {response.query_mode_used} | "
                    f"Considered: {response.total_candidates_considered}"
                )
                print(f"Latency: {response.execution_time_ms:.1f}ms | Summary: {response.summary()}")
                print(response.message)
                print()
                if response.results:
                    print("Results:")
                    for idx, item in enumerate(response.results, 1):
                        print(format_intelligent_result(idx, item))
                if response.grouped_results:
                    print("Groups:")
                    for group in response.grouped_results:
                        print(f"  {group.label}")
                        for idx, item in enumerate(group.results, 1):
                            print(format_intelligent_result(idx, item))
            print()

        except KeyboardInterrupt:
            print("\nInterrupted. Goodbye!")
            break
        except Exception as e:
            print(f"Error: {e}")
            continue


def batch_mode(
    retriever: CommandRetriever,
    assistant: IntelligentCommandAssistant,
    queries: list[str],
    json_output: bool = False,
    raw_mode: bool = False,
) -> None:
    """Run queries from command-line arguments."""
    for query_text in queries:
        q = RetrievalQuery(input_text=query_text)
        try:
            q.validate()
        except ValueError as e:
            print(f"Invalid query '{query_text}': {e}", file=sys.stderr)
            continue

        if raw_mode:
            result = retriever.query(q)
            if json_output:
                import json

                output = {
                    "query": result.query.input_text,
                    "mode": result.query_mode_used,
                    "summary": result.summary(),
                    "candidates": [
                        {
                            "command": c.command,
                            "description": c.description,
                            "platform": c.platform,
                            "mode": c.cmd_mode,
                            "confidence": c.confidence,
                            "match_type": c.match_type,
                            "source_attribution": c.doc_source,
                        }
                        for c in result.candidates
                    ],
                }
                print(json.dumps(output, indent=2))
            else:
                print(f"\nQuery: {result.query.input_text}")
                print(f"Summary: {result.summary()}\n")
                for idx, candidate in enumerate(result.candidates, 1):
                    print(format_candidate(idx, candidate))
            continue

        response = assistant.respond(q)

        if json_output:
            import json

            output = {
                "query": response.query.input_text,
                "mode": response.query_mode_used,
                "response_tier": response.response_tier,
                "message": response.message,
                "summary": response.summary(),
                "results": [
                    {
                        "command": c.command,
                        "description": c.description,
                        "platform": c.platform,
                        "mode": c.mode,
                        "confidence_score": c.confidence_score,
                        "response_tier": c.response_tier,
                        "match_type": c.match_type,
                        "source_attribution": c.source_attribution,
                        "syntax_variants": c.syntax_variants,
                    }
                    for c in response.results
                ],
                "groups": [
                    {
                        "label": group.label,
                        "results": [
                            {
                                "command": item.command,
                                "description": item.description,
                                "platform": item.platform,
                                "mode": item.mode,
                                "confidence_score": item.confidence_score,
                                "response_tier": item.response_tier,
                                "match_type": item.match_type,
                                "source_attribution": item.source_attribution,
                                "syntax_variants": item.syntax_variants,
                            }
                            for item in group.results
                        ],
                    }
                    for group in response.grouped_results
                ],
            }
            print(json.dumps(output, indent=2))
        else:
            print(f"\nQuery: {response.query.input_text}")
            print(f"Tier: {response.response_tier}")
            print(f"Summary: {response.summary()}")
            print(f"Message: {response.message}\n")
            for idx, item in enumerate(response.results, 1):
                print(format_intelligent_result(idx, item))
            for group in response.grouped_results:
                print(f"{group.label}:")
                for idx, item in enumerate(group.results, 1):
                    print(format_intelligent_result(idx, item))


def run_preview_suite(assistant: IntelligentCommandAssistant) -> None:
    """Run the built-in Phase 3.5 sample suite."""
    samples = [
        "show ip route",
        "shwo ip rote",
        "how do i check ospf neighbors",
        "what commands for ospf",
        "something with multicast",
        "asdfqwerty",
    ]
    print("\n=== Phase 3.5 Preview Suite ===\n")
    for sample in samples:
        response = assistant.respond(RetrievalQuery(input_text=sample))
        print(f"Query: {sample}")
        print(f"Tier: {response.response_tier}")
        print(f"Message: {response.message}")
        if response.results:
            for idx, item in enumerate(response.results, 1):
                print(format_intelligent_result(idx, item))
        elif response.grouped_results:
            for group in response.grouped_results:
                print(f"{group.label}:")
                for idx, item in enumerate(group.results, 1):
                    print(format_intelligent_result(idx, item))
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Test/preview retrieval queries against the knowledge base."
    )
    parser.add_argument(
        "--kb-db",
        default="./kb/cliq_kb.sqlite",
        help="Path to SQLite knowledge base (default: ./kb/cliq_kb.sqlite)",
    )
    parser.add_argument(
        "--vector-db",
        default="./kb/cliq_vec.sqlite",
        help="Path to vector database (default: ./kb/cliq_vec.sqlite)",
    )
    parser.add_argument(
        "--interactive",
        "-i",
        action="store_true",
        help="Run interactive query loop (default if no queries specified)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Bypass the intelligent assistant and show raw retrieval output",
    )
    parser.add_argument(
        "--preview-tier",
        action="store_true",
        help="Run the built-in Phase 3.5 sample suite",
    )
    parser.add_argument(
        "queries",
        nargs="*",
        help="Queries to run (optional; if omitted or --interactive, use interactive mode)",
    )

    args = parser.parse_args()

    # Validate DB paths.
    if not Path(args.kb_db).exists():
        print(f"Knowledge base not found: {args.kb_db}")
        sys.exit(1)
    if not Path(args.vector_db).exists():
        print(f"Vector database not found: {args.vector_db}")
        sys.exit(1)

    print(f"Loading stores...")
    sqlite_store, vector_store = load_stores(args.kb_db, args.vector_db)
    retriever = CommandRetriever(sqlite_store, vector_store)
    assistant = IntelligentCommandAssistant(retriever, IntelligentRetrievalConfig())
    print(f"Loaded SQLite store ({sqlite_store.db_path})")
    print(f"Loaded Vector store ({vector_store.db_path})\n")

    if args.preview_tier:
        run_preview_suite(assistant)
        return

    # Run batch or interactive mode.
    if args.queries and not args.interactive:
        batch_mode(
            retriever,
            assistant,
            args.queries,
            json_output=args.json,
            raw_mode=args.raw,
        )
    else:
        interactive_mode(retriever, assistant)


if __name__ == "__main__":
    main()
