#!/usr/bin/env python3
"""
vector_store.py — Local, swappable Vector DB backends for CLIQ ingestion.

The ingestion pipeline depends only on the abstract `VectorStore` interface, so
the concrete backend can be swapped (local SQLite ↔ Chroma ↔ FAISS) without
touching orchestration code.

The default `SQLiteVectorStore` is fully local and dependency-free: it persists
each embedding as a packed-float BLOB alongside JSON metadata in a SQLite file,
and performs cosine-similarity search in pure Python. It is deterministic and
needs no network — ideal for on-prem use and for running the pipeline/tests
without installing a heavyweight vector database.

Each record's `vector_id` is the SAME id used by the SQLite command store, so a
semantic hit is always traceable back to its exact relational record.
"""

from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import struct
import threading
from dataclasses import dataclass
from typing import Optional, Protocol

log = logging.getLogger("cliq.ingest.vector_store")


@dataclass
class VectorHit:
    vector_id: str
    score: float
    metadata: dict


class VectorStore(Protocol):
    """Interface every vector backend must satisfy."""

    def upsert(self, vector_id: str, embedding: list, metadata: dict, document: str) -> None: ...
    def query(self, embedding: list, top_k: int = 5,
              where: Optional[dict] = None) -> list: ...
    def get(self, vector_id: str) -> Optional[dict]: ...
    def delete_by_doc(self, doc_source: str) -> int: ...
    def count(self) -> int: ...
    def close(self) -> None: ...


class SQLiteVectorStore:
    """Pure-Python, file-backed vector store (cosine similarity)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS vectors (
                    vector_id  TEXT PRIMARY KEY,
                    dim        INTEGER NOT NULL,
                    embedding  BLOB NOT NULL,
                    metadata   TEXT NOT NULL,
                    document   TEXT NOT NULL,
                    doc_source TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_vectors_doc
                    ON vectors(doc_source);
                """
            )
            self._conn.commit()

    @staticmethod
    def _pack(embedding: list) -> bytes:
        return struct.pack(f"{len(embedding)}f", *(float(x) for x in embedding))

    @staticmethod
    def _unpack(blob: bytes, dim: int) -> list:
        return list(struct.unpack(f"{dim}f", blob))

    def upsert(self, vector_id: str, embedding: list, metadata: dict, document: str) -> None:
        """Insert or replace a vector. Idempotent on `vector_id` — re-ingesting
        the same record overwrites rather than duplicating."""
        blob = self._pack(embedding)
        doc_source = str(metadata.get("doc_source", ""))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO vectors (vector_id, dim, embedding, metadata, document, doc_source)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(vector_id) DO UPDATE SET
                    dim=excluded.dim, embedding=excluded.embedding,
                    metadata=excluded.metadata, document=excluded.document,
                    doc_source=excluded.doc_source
                """,
                (vector_id, len(embedding), blob, json.dumps(metadata), document, doc_source),
            )
            self._conn.commit()

    @staticmethod
    def _matches(metadata: dict, where: Optional[dict]) -> bool:
        if not where:
            return True
        return all(metadata.get(k) == v for k, v in where.items())

    @staticmethod
    def _cosine(a: list, b: list) -> float:
        # Embeddings are stored normalised, but we normalise defensively so the
        # store works with any provider.
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    def query(self, embedding: list, top_k: int = 5,
              where: Optional[dict] = None) -> list:
        """Return the top_k most similar records as VectorHit, optionally
        filtered by exact metadata match (e.g. {'platform': 'iosxe'})."""
        results: list[VectorHit] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT vector_id, dim, embedding, metadata FROM vectors"
            ).fetchall()
        for row in rows:
            meta = json.loads(row["metadata"])
            if not self._matches(meta, where):
                continue
            vec = self._unpack(row["embedding"], row["dim"])
            score = self._cosine(embedding, vec)
            results.append(VectorHit(row["vector_id"], score, meta))
        results.sort(key=lambda h: h.score, reverse=True)
        return results[:top_k]

    def get(self, vector_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT vector_id, dim, embedding, metadata, document FROM vectors WHERE vector_id=?",
                (vector_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "vector_id": row["vector_id"],
            "embedding": self._unpack(row["embedding"], row["dim"]),
            "metadata": json.loads(row["metadata"]),
            "document": row["document"],
        }

    def delete_by_doc(self, doc_source: str) -> int:
        """Remove all vectors originating from one document (for re-ingest /
        refresh of a single source). Returns the number of rows deleted."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM vectors WHERE doc_source=?", (doc_source,)
            )
            self._conn.commit()
            return cur.rowcount

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]

    def sample(self, limit: int = 5) -> list:
        """Return up to `limit` stored records for post-ingestion spot-checks."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT vector_id, metadata, document FROM vectors LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"vector_id": r["vector_id"], "metadata": json.loads(r["metadata"]),
             "document": r["document"]}
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# ---------------------------------------------------------------------------
# Registry / factory
# ---------------------------------------------------------------------------
_BACKENDS = {"sqlite": SQLiteVectorStore}


def register_vector_backend(name: str, factory) -> None:
    _BACKENDS[name] = factory


def available_backends() -> list[str]:
    return sorted(_BACKENDS.keys())


def get_vector_store(backend: str = "sqlite", **kwargs) -> VectorStore:
    factory = _BACKENDS.get(backend)
    if factory is None:
        log.warning("Unknown vector backend '%s'; using 'sqlite'.", backend)
        factory = SQLiteVectorStore
    return factory(**kwargs)
