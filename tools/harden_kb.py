#!/usr/bin/env python3
"""
harden_kb.py — Repair an existing CLIQ KB in place.

Applies the latest local text-repair logic to stored command rows, refreshes the
linked vector records, backfills high-value commands from curated local YAML if
missing, and prints a random readability sample.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_XET"] = "1"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cliq.ingest.schema import CommandEntry
from cliq.ingest.storage import IngestionPipeline, DEFAULT_KB_DB, DEFAULT_VECTOR_DB
from cliq.ingest.storage.sqlite_store import content_hash
from cliq.ingest.text_repair import desegment_text, repair_command_text


def _parse_syntax_variants(payload: str | None) -> list[str]:
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _normalise_row(row: sqlite3.Row) -> CommandEntry:
    syntax_variants = [repair_command_text(item) for item in _parse_syntax_variants(row["syntax_variants"])]
    syntax_variants = [item for item in syntax_variants if item]
    return CommandEntry(
        command=repair_command_text(row["command"]),
        description=desegment_text(row["description"] or "").strip(),
        syntax_variants=syntax_variants,
        mode=row["mode"],
        platform=row["platform"],
        doc_source=row["doc_source"] or "",
        confidence=row["confidence"] or "ok",
        flags=json.loads(row["flags"] or "[]"),
    )


def _refresh_row(pipe: IngestionPipeline, row: sqlite3.Row, entry: CommandEntry) -> str:
    now = time.time()
    existing = pipe.store.get_by_command_platform(entry.command, entry.platform)
    vector_id = row["vector_id"]
    if existing is not None and int(existing["id"]) != int(row["id"]):
        merged = CommandEntry(
            command=entry.command,
            description=entry.description if len(entry.description) >= len(existing["description"] or "") else (existing["description"] or ""),
            syntax_variants=sorted(set(entry.syntax_variants) | set(_parse_syntax_variants(existing["syntax_variants"]))),
            mode=entry.mode,
            platform=entry.platform,
            doc_source=entry.doc_source or (existing["doc_source"] or ""),
            confidence=entry.confidence,
            flags=entry.flags,
        )
        target_id = int(existing["id"])
        with pipe.store._lock:
            pipe.store._conn.execute(
                """
                UPDATE commands SET
                  description=?, syntax_variants=?, mode=?, doc_source=?,
                  confidence=?, flags=?, content_hash=?, updated_at=?
                WHERE id=?
                """,
                (
                    merged.description,
                    json.dumps(merged.syntax_variants),
                    merged.mode,
                    merged.doc_source,
                    merged.confidence,
                    json.dumps(merged.flags),
                    content_hash(merged),
                    now,
                    target_id,
                ),
            )
            pipe.store._conn.execute("DELETE FROM commands WHERE id=?", (int(row["id"]),))
            pipe.store._conn.commit()
        with pipe.vectors._lock:
            pipe.vectors._conn.execute("DELETE FROM vectors WHERE vector_id=?", (vector_id,))
            pipe.vectors._conn.commit()
        refreshed = pipe.store.get_by_id(target_id)
        pipe.vectors.upsert(
            refreshed["vector_id"],
            pipe.embedder.embed(pipe._embed_text(merged)),
            metadata={
                "command": merged.command,
                "platform": merged.platform,
                "mode": merged.mode,
                "doc_source": merged.doc_source,
                "sqlite_id": target_id,
            },
            document=pipe._embed_text(merged),
        )
        return "merged"

    with pipe.store._lock:
        pipe.store._conn.execute(
            """
            UPDATE commands SET
              command=?, description=?, syntax_variants=?, mode=?, platform=?,
              doc_source=?, confidence=?, flags=?, content_hash=?, updated_at=?
            WHERE id=?
            """,
            (
                entry.command,
                entry.description,
                json.dumps(entry.syntax_variants),
                entry.mode,
                entry.platform,
                entry.doc_source,
                entry.confidence,
                json.dumps(entry.flags),
                content_hash(entry),
                now,
                int(row["id"]),
            ),
        )
        pipe.store._conn.commit()
    pipe.vectors.upsert(
        vector_id,
        pipe.embedder.embed(pipe._embed_text(entry)),
        metadata={
            "command": entry.command,
            "platform": entry.platform,
            "mode": entry.mode,
            "doc_source": entry.doc_source,
            "sqlite_id": int(row["id"]),
        },
        document=pipe._embed_text(entry),
    )
    return "updated"


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair an existing CLIQ KB in place.")
    parser.add_argument("--kb-db", default=os.path.join("kb", "cliq_kb.sqlite"), help="Path to SQLite KB")
    parser.add_argument("--vector-db", default=os.path.join("kb", "cliq_vec.sqlite"), help="Path to vector DB")
    parser.add_argument("--sample-size", type=int, default=20, help="Random sample size for readability check")
    args = parser.parse_args()

    pipe = IngestionPipeline(kb_db_path=args.kb_db, vector_db_path=args.vector_db)
    try:
        with pipe.store._lock:
            rows = pipe.store._conn.execute("SELECT * FROM commands ORDER BY id").fetchall()

        updated = 0
        merged = 0
        unchanged = 0
        for row in rows:
            entry = _normalise_row(row)
            current_syntax = _parse_syntax_variants(row["syntax_variants"])
            if (
                entry.command == row["command"]
                and entry.description == (row["description"] or "")
                and entry.syntax_variants == current_syntax
            ):
                unchanged += 1
                continue
            action = _refresh_row(pipe, row, entry)
            if action == "merged":
                merged += 1
            else:
                updated += 1

        for platform in sorted(pipe.store.count_by_platform()):
            pipe._ensure_high_value_commands(platform)

        report = pipe.build_report([])
        print("KB hardening summary")
        print(f"  updated rows : {updated}")
        print(f"  merged rows  : {merged}")
        print(f"  unchanged    : {unchanged}")
        print()
        print("\n".join(report.summary_lines()))
        print()

        with pipe.store._lock:
            sample_rows = pipe.store._conn.execute(
                "SELECT command, substr(description,1,180) AS description FROM commands ORDER BY RANDOM() LIMIT ?",
                (args.sample_size,),
            ).fetchall()
        print(f"Random readability sample ({len(sample_rows)} rows):")
        for row in sample_rows:
            print(f"  {row['command']} -> {row['description']}")
        return 0 if report.reconciled and not report.high_value_missing else 1
    finally:
        pipe.close()


if __name__ == "__main__":
    raise SystemExit(main())
