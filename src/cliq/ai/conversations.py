from __future__ import annotations
from contextlib import contextmanager
"""
CLIQ AI Conversation Persistence Layer (Phase V).

Thread-safe SQLite storage for AI copilot conversations and message histories.
Supports conversation creation, listing, retrieval, renaming, deletion, message
persistence, and multi-tenant session isolation.
"""


import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("cliq.ai.conversations")

_DEFAULT_DB_DIR = Path.home() / ".cliq"
DEFAULT_CONVERSATIONS_DB = str(_DEFAULT_DB_DIR / "copilot.sqlite")


@dataclass
class StoredMessage:
    id: str
    conversation_id: str
    role: str
    content: str
    created_at: float
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }


@dataclass
class Conversation:
    id: str
    title: str
    created_at: float
    updated_at: float
    model: str
    provider: str
    session_token: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    messages: List[StoredMessage] = field(default_factory=list)

    def to_dict(self, include_messages: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "model": self.model,
            "provider": self.provider,
            "metadata": self.metadata,
        }
        if include_messages:
            d["messages"] = [m.to_dict() for m in self.messages]
        return d


class ConversationStore:
    """Thread-safe SQLite repository for conversation histories."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or (os.environ.get("CLIQ_COPILOT_DB", "").strip() or DEFAULT_CONVERSATIONS_DB)
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                model TEXT NOT NULL,
                provider TEXT NOT NULL,
                session_token TEXT,
                metadata TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL,
                metadata TEXT,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_conv_session ON conversations(session_token);
            CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, created_at ASC);
        ''')

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        self._ensure_schema(conn)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._get_connection() as conn:
            self._ensure_schema(conn)

    def create_conversation(
        self,
        title: str = "New Conversation",
        model: str = "Llama-3.3-70B-Instruct",
        provider: str = "meta",
        session_token: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Conversation:
        conv_id = f"conv-{uuid.uuid4().hex[:12]}"
        now = time.time()
        meta_json = json.dumps(metadata or {})

        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO conversations (id, title, created_at, updated_at, model, provider, session_token, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (conv_id, title, now, now, model, provider, session_token, meta_json),
            )
            conn.commit()

        log.info("Created conversation %s: '%s'", conv_id, title)
        return Conversation(
            id=conv_id,
            title=title,
            created_at=now,
            updated_at=now,
            model=model,
            provider=provider,
            session_token=session_token,
            metadata=metadata or {},
            messages=[],
        )

    def list_conversations(self, session_token: Optional[str] = None, limit: int = 50) -> List[Conversation]:
        query = "SELECT * FROM conversations"
        params: List[Any] = []
        if session_token:
            query += " WHERE session_token = ? OR session_token IS NULL"
            params.append(session_token)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        with self._lock, self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()

        conversations = []
        for r in rows:
            meta = json.loads(r["metadata"] or "{}")
            conversations.append(
                Conversation(
                    id=r["id"],
                    title=r["title"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                    model=r["model"],
                    provider=r["provider"],
                    session_token=r["session_token"],
                    metadata=meta,
                )
            )
        return conversations

    def get_conversation(self, conversation_id: str, session_token: Optional[str] = None) -> Optional[Conversation]:
        with self._lock, self._get_connection() as conn:
            c_row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if not c_row:
                return None
            
            # Session isolation check
            if session_token and c_row["session_token"] and c_row["session_token"] != session_token:
                return None

            m_rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()

        messages = [
            StoredMessage(
                id=m["id"],
                conversation_id=m["conversation_id"],
                role=m["role"],
                content=m["content"],
                created_at=m["created_at"],
                metadata=json.loads(m["metadata"] or "{}"),
            )
            for m in m_rows
        ]

        return Conversation(
            id=c_row["id"],
            title=c_row["title"],
            created_at=c_row["created_at"],
            updated_at=c_row["updated_at"],
            model=c_row["model"],
            provider=c_row["provider"],
            session_token=c_row["session_token"],
            metadata=json.loads(c_row["metadata"] or "{}"),
            messages=messages,
        )

    def get_messages(self, conversation_id: str) -> List[StoredMessage]:
        with self._lock, self._get_connection() as conn:
            m_rows = conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC",
                (conversation_id,),
            ).fetchall()
        return [
            StoredMessage(
                id=m["id"],
                conversation_id=m["conversation_id"],
                role=m["role"],
                content=m["content"],
                created_at=m["created_at"],
                metadata=json.loads(m["metadata"] or "{}"),
            )
            for m in m_rows
        ]

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StoredMessage:
        msg_id = f"msg-{uuid.uuid4().hex[:12]}"
        now = time.time()
        meta_json = json.dumps(metadata or {})

        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO messages (id, conversation_id, role, content, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (msg_id, conversation_id, role, content, now, meta_json),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
            conn.commit()

        return StoredMessage(
            id=msg_id,
            conversation_id=conversation_id,
            role=role,
            content=content,
            created_at=now,
            metadata=metadata or {},
        )

    def rename_conversation(self, conversation_id: str, new_title: str, session_token: Optional[str] = None) -> bool:
        with self._lock, self._get_connection() as conn:
            if session_token:
                cursor = conn.execute(
                    "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ? AND (session_token = ? OR session_token IS NULL)",
                    (new_title.strip(), time.time(), conversation_id, session_token),
                )
            else:
                cursor = conn.execute(
                    "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                    (new_title.strip(), time.time(), conversation_id),
                )
            conn.commit()
            return cursor.rowcount > 0

    def delete_conversation(self, conversation_id: str, session_token: Optional[str] = None) -> bool:
        with self._lock, self._get_connection() as conn:
            if session_token:
                cursor = conn.execute(
                    "DELETE FROM conversations WHERE id = ? AND (session_token = ? OR session_token IS NULL)",
                    (conversation_id, session_token),
                )
            else:
                cursor = conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            if cursor.rowcount > 0:
                conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
                conn.commit()
                return True
            conn.commit()
            return False

    def search_conversations(self, query: str, session_token: Optional[str] = None, limit: int = 20) -> List[Conversation]:
        like_query = f"%{query}%"
        sql = """
            SELECT DISTINCT c.* FROM conversations c
            LEFT JOIN messages m ON c.id = m.conversation_id
            WHERE (c.title LIKE ? OR m.content LIKE ?)
        """
        params: List[Any] = [like_query, like_query]
        if session_token:
            sql += " AND (c.session_token = ? OR c.session_token IS NULL)"
            params.append(session_token)
        sql += " ORDER BY c.updated_at DESC LIMIT ?"
        params.append(limit)

        with self._lock, self._get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [
            Conversation(
                id=r["id"],
                title=r["title"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                model=r["model"],
                provider=r["provider"],
                session_token=r["session_token"],
                metadata=json.loads(r["metadata"] or "{}"),
            )
            for r in rows
        ]
