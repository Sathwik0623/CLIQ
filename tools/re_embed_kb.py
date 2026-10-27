#!/usr/bin/env python3
"""
re_embed_kb.py — Regenerate Vector DB embeddings from hardened SQLite text.

After KB hardening, vector embeddings must be regenerated from the cleaned text
to ensure semantic search results contain cleaned descriptions (not stale OCR corruption).

Usage:
  python tools/re_embed_kb.py --kb-db kb/cliq_kb.sqlite --vector-db kb/cliq_vec.sqlite
"""

import argparse
import logging
import os
import sqlite3
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_XET"] = "1"

from cliq.ingest.storage.embeddings import get_embedder
from cliq.ingest.storage.sqlite_store import CommandStore
from cliq.ingest.storage.vector_store import SQLiteVectorStore

log = logging.getLogger("re_embed_kb")
logging.basicConfig(level=logging.INFO)


def _build_embedding_text(row: sqlite3.Row) -> str:
    """Build the text to embed from a SQLite command row."""
    command = str(row["command"] or "").strip()
    description = str(row["description"] or "").strip()
    # Use command + description as embedding source (same as ingestion pipeline)
    return f"{command} {description}".strip()


def re_embed_kb(
    kb_path: str,
    vec_path: str,
    batch_size: int = 50,
    verbose: bool = False,
) -> dict:
    """
    Regenerate all vector embeddings from hardened SQLite text.
    
    Returns:
        dict with statistics: {updated, skipped, failed, total}
    """
    kb_store = CommandStore(kb_path)
    vec_store = SQLiteVectorStore(vec_path)
    embedder = get_embedder()
    
    stats = {"updated": 0, "skipped": 0, "failed": 0, "total": 0}
    
    con = sqlite3.connect(kb_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    
    # Get all commands
    rows = cur.execute("SELECT id, command, description, platform, doc_source FROM commands ORDER BY id").fetchall()
    total = len(rows)
    
    log.info(f"Re-embedding {total} commands from {kb_path}")
    
    for batch_start in range(0, total, batch_size):
        batch_end = min(batch_start + batch_size, total)
        batch = rows[batch_start:batch_end]
        
        # Build embeddings for batch
        texts = [_build_embedding_text(row) for row in batch]
        embeddings = embedder.embed_batch(texts)
        
        # Update vector DB
        for row, embedding in zip(batch, embeddings):
            sqlite_id = row["id"]
            vector_id = f"cmd-{sqlite_id}"
            
            try:
                # Prepare metadata
                metadata = {
                    "sqlite_id": sqlite_id,
                    "command": row["command"],
                    "platform": row["platform"],
                    "doc_source": row["doc_source"] or "",
                }
                
                # Upsert into vector store (replaces old embedding)
                vec_store.upsert(
                    vector_id=vector_id,
                    embedding=embedding,
                    metadata=metadata,
                    document=_build_embedding_text(row),
                )
                
                stats["updated"] += 1
                
                if verbose and (stats["updated"] + stats["failed"]) % 100 == 0:
                    log.info(f"  ... {stats['updated']} updated, {stats['failed']} failed")
                    
            except Exception as e:
                log.error(f"Failed to re-embed cmd-{sqlite_id}: {e}")
                stats["failed"] += 1
        
        stats["total"] = batch_end
    
    con.close()
    kb_store.close()
    vec_store.close()
    
    return stats


def reconcile_stores(kb_path: str, vec_path: str) -> dict:
    """
    Verify that SQLite and Vector DB are in sync.
    
    Returns:
        dict with reconciliation results
    """
    kb_con = sqlite3.connect(kb_path)
    kb_cur = kb_con.cursor()
    sqlite_count = kb_cur.execute("SELECT COUNT(*) FROM commands").fetchone()[0]
    kb_con.close()
    
    vec_con = sqlite3.connect(vec_path)
    vec_cur = vec_con.cursor()
    vector_count = vec_cur.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    vec_con.close()
    
    log.info(f"\nStore Reconciliation:")
    log.info(f"  SQLite records: {sqlite_count}")
    log.info(f"  Vector records: {vector_count}")
    
    if sqlite_count == vector_count:
        log.info(f"  ✓ Stores reconciled (matched at {sqlite_count})")
        return {"reconciled": True, "sqlite_count": sqlite_count, "vector_count": vector_count}
    else:
        log.warning(f"  ⚠ Store mismatch: {abs(sqlite_count - vector_count)} records differ")
        return {"reconciled": False, "sqlite_count": sqlite_count, "vector_count": vector_count}


def main():
    parser = argparse.ArgumentParser(
        description="Re-embed Vector DB from hardened SQLite text.",
    )
    parser.add_argument(
        "--kb-db",
        default="./kb/cliq_kb.sqlite",
        help="Path to SQLite knowledge base",
    )
    parser.add_argument(
        "--vector-db",
        default="./kb/cliq_vec.sqlite",
        help="Path to vector database",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Batch size for embedding (default: 50)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    
    args = parser.parse_args()
    
    # Validate paths
    if not Path(args.kb_db).exists():
        print(f"Error: {args.kb_db} not found")
        return 1
    if not Path(args.vector_db).exists():
        print(f"Error: {args.vector_db} not found")
        return 1
    
    # Re-embed
    stats = re_embed_kb(args.kb_db, args.vector_db, args.batch_size, args.verbose)
    
    log.info(f"\nRe-embedding Summary:")
    log.info(f"  Updated: {stats['updated']}")
    log.info(f"  Failed:  {stats['failed']}")
    log.info(f"  Total:   {stats['total']}")
    
    # Reconcile
    reconcile_stats = reconcile_stores(args.kb_db, args.vector_db)
    
    return 0 if stats["failed"] == 0 and reconcile_stats["reconciled"] else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
