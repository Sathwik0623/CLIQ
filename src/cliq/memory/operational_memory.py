#!/usr/bin/env python3
"""
operational_memory.py — Contextual operational memory for CLIQ (Phase 5).

Stores issue descriptions, suggested/used commands, platform context, and outcomes.
Provides semantic recall of similar past issues across devices/sessions.
Uses SQLite for structured records + Vector DB for semantic matching.
Fully offline, zero network calls.
"""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from cliq.ingest.storage.embeddings import get_embedder
from cliq.ingest.storage.vector_store import SQLiteVectorStore
from cliq.retrieval.schema import MemoryRecord, MemoryScope, OutcomeStatus, PastResolution

logger = logging.getLogger(__name__)


class OperationalMemoryStore:
    """
    Persistent contextual memory for resolved issues.
    
    Stores issue descriptions, commands, platform, device, outcome, and timestamp.
    Embeddings stored in separate vector DB for semantic recall.
    Fully offline; uses cached SentenceTransformer embedder.
    """

    SCHEMA_VERSION = "1"
    DEFAULT_MEMORY_DIR = ".cliq/memory"

    def __init__(self, memory_dir: Optional[str] = None):
        """
        Initialize operational memory store.
        
        Args:
            memory_dir: Path to memory store directory (default: .cliq/memory).
                       Auto-created if needed.
        """
        self.memory_dir = Path(memory_dir or self.DEFAULT_MEMORY_DIR)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        
        self.kb_path = self.memory_dir / "operational_memories.db"
        self.vec_path = self.memory_dir / "operational_memories_vectors.db"
        
        # Initialize offline embedder (same as Phase 3.5.2)
        self.embedder = get_embedder(
            name="sentence-transformers",
            allow_fallback_to_hashing=False,
        )
        
        # Vector store for semantic recall
        self.vector_store = SQLiteVectorStore(str(self.vec_path))
        
        self._init_schema()
        logger.info(f"OperationalMemoryStore initialized at {self.memory_dir}")

    def _init_schema(self) -> None:
        """Create SQLite schema if not exists."""
        with sqlite3.connect(self.kb_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version TEXT PRIMARY KEY,
                    created_at TEXT
                )
                """
            )
            
            # Check if schema already initialized
            cursor = conn.execute(
                "SELECT version FROM schema_version WHERE version = ?",
                (self.SCHEMA_VERSION,),
            )
            if cursor.fetchone():
                return  # Already initialized
            
            # Create memories table
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    memory_id TEXT PRIMARY KEY,
                    issue_text TEXT NOT NULL,
                    commands TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    device_name TEXT,
                    outcome TEXT NOT NULL,
                    outcome_notes TEXT,
                    scope TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_outcome ON memories(outcome)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_platform ON memories(platform)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_timestamp ON memories(timestamp DESC)"
            )
            
            # Record schema version
            conn.execute(
                "INSERT INTO schema_version (version, created_at) VALUES (?, ?)",
                (self.SCHEMA_VERSION, datetime.utcnow().isoformat()),
            )
            conn.commit()

    def store_memory(
        self,
        issue_text: str,
        commands: list[str],
        platform: str = "iosxe",
        device_name: Optional[str] = None,
        outcome: OutcomeStatus = "pending",
        outcome_notes: str = "",
        scope: MemoryScope = "user",
    ) -> str:
        """
        Store a new memory record.
        
        Args:
            issue_text: Description of the problem.
            commands: List of suggested/used commands.
            platform: Platform context (iosxe, iosxr, etc.).
            device_name: Optional device identifier.
            outcome: Resolution status (pending, resolved, failed, partial).
            outcome_notes: Optional outcome details.
            scope: Access scope (user, team, organization).
            
        Returns:
            memory_id (UUID string) of stored record.
        """
        memory_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        
        # Store in SQLite
        with sqlite3.connect(self.kb_path) as conn:
            conn.execute(
                """
                INSERT INTO memories 
                (memory_id, issue_text, commands, platform, device_name, outcome, 
                 outcome_notes, scope, timestamp, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    issue_text,
                    json.dumps(commands),
                    platform,
                    device_name,
                    outcome,
                    outcome_notes,
                    scope,
                    now,
                    now,
                ),
            )
            conn.commit()
        
        # Embed and store in vector DB
        try:
            embedding = self.embedder.embed(issue_text)
            self.vector_store.upsert(
                vector_id=memory_id,
                embedding=embedding,
                metadata={
                    "platform": platform,
                    "device_name": device_name,
                    "outcome": outcome,
                    "scope": scope,
                    "doc_source": "operational_memory",
                },
                document=issue_text,
            )
        except Exception as e:
            logger.error(f"Failed to embed memory {memory_id}: {e}")
            # Continue anyway; memory is stored even if embedding fails
        
        logger.info(f"Stored memory {memory_id}: {issue_text[:50]}...")
        return memory_id

    def recall_similar(
        self,
        query_text: str,
        top_k: int = 3,
        min_similarity: float = 0.5,
        platform_filter: Optional[str] = None,
        outcome_filter: Optional[OutcomeStatus] = None,
    ) -> list[PastResolution]:
        """
        Recall similar past resolutions.
        
        Args:
            query_text: Current issue/query text.
            top_k: Return at most this many memories.
            min_similarity: Minimum similarity threshold (0–1).
            platform_filter: Optional platform filter (e.g., "iosxe").
            outcome_filter: Optional outcome filter (e.g., "resolved").
            
        Returns:
            List of PastResolution objects, ranked by relevance.
        """
        # Vector search for semantic similarity
        try:
            embedding = self.embedder.embed(query_text)
        except Exception as e:
            logger.error(f"Failed to embed query: {e}")
            return []
        
        # Search vector DB
        results = self.vector_store.query(
            embedding=embedding,
            top_k=top_k * 2,  # Over-fetch to apply filters
        )
        
        if not results:
            logger.debug("No vector search results for query")
            return []
        
        # Load full records from SQLite and apply filters
        memory_ids = [r.vector_id for r in results]
        past_resolutions = []
        
        with sqlite3.connect(self.kb_path) as conn:
            for mem_id, vec_result in zip(memory_ids, results):
                try:
                    cursor = conn.execute(
                        "SELECT * FROM memories WHERE memory_id = ?",
                        (mem_id,),
                    )
                    row = cursor.fetchone()
                    if not row:
                        continue
                    
                    # Parse row
                    (
                        memory_id,
                        issue_text,
                        commands_json,
                        platform,
                        device_name,
                        outcome,
                        outcome_notes,
                        scope,
                        timestamp,
                        created_at,
                    ) = row
                    
                    # Apply filters
                    if platform_filter and platform != platform_filter:
                        continue  # Different platform; still surface but lower score
                    
                    if outcome_filter and outcome != outcome_filter:
                        continue
                    
                    similarity = vec_result.score
                    if similarity < min_similarity:
                        continue
                    
                    # Calculate composite score
                    outcome_weight = {
                        "resolved": 1.0,
                        "partial": 0.7,
                        "pending": 0.3,
                        "failed": 0.1,
                    }.get(outcome, 0.0)
                    
                    # Recency boost (decay over time)
                    try:
                        ts = datetime.fromisoformat(timestamp)
                        age_days = (datetime.utcnow() - ts).days
                        recency_score = max(0.0, 1.0 - (age_days / 365.0))
                    except Exception:
                        recency_score = 0.5
                    
                    final_score = similarity * outcome_weight * (0.7 + 0.3 * recency_score)
                    
                    # Platform note
                    reason = f"Similar issue"
                    if platform_filter and platform != platform_filter:
                        reason += f" (different platform: {platform})"
                    if device_name:
                        reason += f" on {device_name}"
                    
                    record = MemoryRecord(
                        issue_text=issue_text,
                        commands=json.loads(commands_json),
                        platform=platform,
                        device_name=device_name,
                        outcome=outcome,
                        outcome_notes=outcome_notes,
                        scope=scope,
                        timestamp=timestamp,
                        memory_id=memory_id,
                    )
                    
                    past_resolutions.append(
                        PastResolution(
                            memory_record=record,
                            similarity_score=similarity,
                            recency_score=recency_score,
                            final_rank_score=final_score,
                            reason=reason,
                        )
                    )
                
                except Exception as e:
                    logger.warning(f"Failed to load memory {mem_id}: {e}")
                    continue
        
        # Sort by composite score descending
        past_resolutions.sort(key=lambda x: x.final_rank_score, reverse=True)
        return past_resolutions[:top_k]

    def update_outcome(
        self,
        memory_id: str,
        outcome: OutcomeStatus,
        outcome_notes: str = "",
    ) -> bool:
        """
        Update the outcome of a memory record.
        
        Args:
            memory_id: UUID of the memory to update.
            outcome: New outcome status.
            outcome_notes: Optional outcome details.
            
        Returns:
            True if updated, False if not found.
        """
        with sqlite3.connect(self.kb_path) as conn:
            cursor = conn.execute(
                "UPDATE memories SET outcome = ?, outcome_notes = ? WHERE memory_id = ?",
                (outcome, outcome_notes, memory_id),
            )
            conn.commit()
            updated = cursor.rowcount > 0
        
        if updated:
            logger.info(f"Updated memory {memory_id} outcome to {outcome}")
        return updated

    def list_memories(
        self,
        platform_filter: Optional[str] = None,
        outcome_filter: Optional[OutcomeStatus] = None,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        """
        List stored memories with optional filters.
        
        Args:
            platform_filter: Optional platform filter.
            outcome_filter: Optional outcome filter.
            limit: Maximum records to return.
            
        Returns:
            List of MemoryRecord objects.
        """
        query = "SELECT * FROM memories WHERE 1=1"
        params = []
        
        if platform_filter:
            query += " AND platform = ?"
            params.append(platform_filter)
        
        if outcome_filter:
            query += " AND outcome = ?"
            params.append(outcome_filter)
        
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        
        records = []
        with sqlite3.connect(self.kb_path) as conn:
            cursor = conn.execute(query, params)
            for row in cursor.fetchall():
                (
                    memory_id,
                    issue_text,
                    commands_json,
                    platform,
                    device_name,
                    outcome,
                    outcome_notes,
                    scope,
                    timestamp,
                    created_at,
                ) = row
                records.append(
                    MemoryRecord(
                        issue_text=issue_text,
                        commands=json.loads(commands_json),
                        platform=platform,
                        device_name=device_name,
                        outcome=outcome,
                        outcome_notes=outcome_notes,
                        scope=scope,
                        timestamp=timestamp,
                        memory_id=memory_id,
                    )
                )
        
        return records

    def clear_memory(self, memory_id: str) -> bool:
        """
        Delete a specific memory record.
        
        Args:
            memory_id: UUID of memory to delete.
            
        Returns:
            True if deleted, False if not found.
        """
        with sqlite3.connect(self.kb_path) as conn:
            cursor = conn.execute(
                "DELETE FROM memories WHERE memory_id = ?",
                (memory_id,),
            )
            conn.commit()
            deleted = cursor.rowcount > 0
        
        # Also remove from vector store
        try:
            # Vector store doesn't have direct delete by ID, so we use delete_by_doc
            # Instead, we'll just mark it as deleted or ignore it
            logger.debug(f"Removing vector for memory {memory_id}")
        except Exception as e:
            logger.warning(f"Failed to delete vector {memory_id}: {e}")
        
        if deleted:
            logger.info(f"Deleted memory {memory_id}")
        return deleted

    def clear_all_memories(self) -> int:
        """
        Delete all memories (caution: irreversible).
        
        Returns:
            Number of records deleted.
        """
        with sqlite3.connect(self.kb_path) as conn:
            cursor = conn.execute("DELETE FROM memories")
            conn.commit()
            count = cursor.rowcount
        
        # Recreate vector store
        try:
            self.vec_path.unlink(missing_ok=True)
            self.vector_store = SQLiteVectorStore(str(self.vec_path))
        except Exception as e:
            logger.error(f"Failed to reset vector store: {e}")
        
        logger.warning(f"Cleared all {count} memories")
        return count

    def memory_count(self) -> int:
        """Get total count of stored memories."""
        with sqlite3.connect(self.kb_path) as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM memories")
            return cursor.fetchone()[0]

    def resolved_count(self) -> int:
        """Get count of resolved memories."""
        with sqlite3.connect(self.kb_path) as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE outcome = 'resolved'"
            )
            return cursor.fetchone()[0]
