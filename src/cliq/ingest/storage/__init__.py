#!/usr/bin/env python3
"""
cliq.ingest.storage — Phase 1B/1C storage & ingestion layer.

Persists Phase 1A command entries into SQLite (exact lookup) and a Vector DB
(semantic search) with linked ids, a logged deduplication policy, idempotent
multi-document ingestion, a validation report, and a post-ingestion spot-check.

All backends are local and swappable:
    embedders     — get_embedder() / register_embedder()
    vector stores — get_vector_store() / register_vector_backend()
    sqlite store  — CommandStore

Public entry point:
    pipe = IngestionPipeline()
    report = pipe.ingest_documents([(path, platform), ...])
    print("\\n".join(report.summary_lines()))
    print(pipe.spot_check())
"""

from cliq.ingest.storage.embeddings import (
    Embedder,
    LocalHashingEmbedder,
    SentenceTransformerEmbedder,
    get_embedder,
    register_embedder,
    available_embedders,
)
from cliq.ingest.storage.vector_store import (
    VectorStore,
    SQLiteVectorStore,
    VectorHit,
    get_vector_store,
    register_vector_backend,
    available_backends,
)
from cliq.ingest.storage.sqlite_store import (
    CommandStore,
    content_hash,
    vector_id_for,
    DEFAULT_KB_DB,
)
from cliq.ingest.storage.pipeline import (
    IngestionPipeline,
    DedupPolicy,
    DocIngestResult,
    ValidationReport,
    DEFAULT_VECTOR_DB,
)

__all__ = [
    "Embedder",
    "LocalHashingEmbedder",
    "SentenceTransformerEmbedder",
    "get_embedder",
    "register_embedder",
    "available_embedders",
    "VectorStore",
    "SQLiteVectorStore",
    "VectorHit",
    "get_vector_store",
    "register_vector_backend",
    "available_backends",
    "CommandStore",
    "content_hash",
    "vector_id_for",
    "DEFAULT_KB_DB",
    "IngestionPipeline",
    "DedupPolicy",
    "DocIngestResult",
    "ValidationReport",
    "DEFAULT_VECTOR_DB",
]
