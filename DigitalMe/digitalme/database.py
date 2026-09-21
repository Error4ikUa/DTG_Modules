from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import aiosqlite

from .memory import ApprovedMemory
from .utils import json_dumps, json_loads, now_ts


CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    sender_id INTEGER,
    message_id INTEGER,
    timestamp REAL NOT NULL,
    text TEXT NOT NULL,
    reply_to_message_id INTEGER,
    forwarded_from TEXT,
    reactions_json TEXT NOT NULL DEFAULT '[]',
    message_type TEXT NOT NULL DEFAULT 'message',
    dialog_id INTEGER,
    imported_at REAL NOT NULL,
    UNIQUE(chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_id ON messages(chat_id);
CREATE INDEX IF NOT EXISTS idx_messages_sender_id ON messages(sender_id);
CREATE INDEX IF NOT EXISTS idx_messages_message_id ON messages(message_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_dialog_id ON messages(dialog_id);

CREATE TABLE IF NOT EXISTS dialogs (
    chat_id INTEGER PRIMARY KEY,
    contact_id INTEGER,
    display_name TEXT NOT NULL DEFAULT '',
    dialog_type TEXT NOT NULL DEFAULT 'personal_chat',
    first_timestamp REAL,
    last_timestamp REAL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dialogs_contact_id ON dialogs(contact_id);

CREATE TABLE IF NOT EXISTS conversation_examples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    contact_id INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    context_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    owner_message_ids_json TEXT NOT NULL,
    bubble_count INTEGER NOT NULL,
    UNIQUE(chat_id, owner_message_ids_json)
);
CREATE INDEX IF NOT EXISTS idx_examples_chat_id ON conversation_examples(chat_id);
CREATE INDEX IF NOT EXISTS idx_examples_contact_id ON conversation_examples(contact_id);
CREATE INDEX IF NOT EXISTS idx_examples_timestamp ON conversation_examples(timestamp);

CREATE TABLE IF NOT EXISTS relationship_profiles (
    contact_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    profile_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_relationship_profiles_chat_id ON relationship_profiles(chat_id);

CREATE TABLE IF NOT EXISTS personality_profiles (
    owner_id INTEGER PRIMARY KEY,
    profile_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact TEXT NOT NULL,
    scope TEXT NOT NULL,
    confidence REAL NOT NULL,
    chat_id INTEGER,
    contact_id INTEGER,
    created_at REAL NOT NULL,
    expires_at REAL
);
CREATE INDEX IF NOT EXISTS idx_memories_chat_id ON memories(chat_id);
CREATE INDEX IF NOT EXISTS idx_memories_contact_id ON memories(contact_id);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    chat_id INTEGER PRIMARY KEY,
    summary_json TEXT NOT NULL,
    source_message_count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    chat_id INTEGER PRIMARY KEY,
    recent_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS rag_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_example_id INTEGER UNIQUE,
    kind TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    contact_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rag_documents_chat_id ON rag_documents(chat_id);
CREATE INDEX IF NOT EXISTS idx_rag_documents_contact_id ON rag_documents(contact_id);
CREATE INDEX IF NOT EXISTS idx_rag_documents_timestamp ON rag_documents(timestamp);

CREATE TABLE IF NOT EXISTS embeddings (
    doc_id INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS queue_history (
    generation_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    sender_id INTEGER NOT NULL,
    buffered_count INTEGER NOT NULL,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    status TEXT NOT NULL,
    error_kind TEXT
);
CREATE INDEX IF NOT EXISTS idx_queue_history_chat_id ON queue_history(chat_id);
CREATE INDEX IF NOT EXISTS idx_queue_history_timestamp ON queue_history(created_at);

CREATE TABLE IF NOT EXISTS chat_controls (
    chat_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    allowed INTEGER NOT NULL DEFAULT 0,
    denied INTEGER NOT NULL DEFAULT 0,
    display_name TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);
"""


WriteOperation = Callable[[], Awaitable[Any]]


class DigitalMeDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self.fts_enabled = False

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = sqlite3.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=8000")
        await conn.executescript(CORE_SCHEMA)
        try:
            cursor = await conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'rag_fts'")
            existing = await cursor.fetchone()
            await cursor.close()
            # Older development builds used an external-content FTS table that cannot be
            # safely cleared with DELETE on every SQLite build. The index is derived data.
            if existing and "content='rag_documents'" in str(existing[0] or ""):
                await conn.execute("DROP TABLE rag_fts")
                existing = None
            if not existing:
                await conn.execute("CREATE VIRTUAL TABLE rag_fts USING fts5(content)")
                await conn.execute("INSERT INTO rag_fts(rowid, content) SELECT id, content FROM rag_documents")
            self.fts_enabled = True
        except aiosqlite.OperationalError:
            self.fts_enabled = False
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        async with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            await conn.close()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("DigitalMe database is not connected")
        return self._conn

    async def _write(self, operation: WriteOperation) -> Any:
        for attempt in range(4):
            try:
                async with self._lock:
                    result = await operation()
                    await self.conn.commit()
                    return result
            except aiosqlite.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 3:
                    raise
                await asyncio.sleep(0.15 * (attempt + 1))
        return None

    async def _fetchall(self, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        async with self._lock:
            cursor = await self.conn.execute(query, params)
            rows = await cursor.fetchall()
            await cursor.close()
            return rows

    async def _fetchone(self, query: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        async with self._lock:
            cursor = await self.conn.execute(query, params)
            row = await cursor.fetchone()
            await cursor.close()
            return row

    async def set_setting(self, key: str, value: Any) -> None:
        async def operation() -> None:
            await self.conn.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, json_dumps(value), now_ts()),
            )

        await self._write(operation)

    async def get_setting(self, key: str, default: Any = None) -> Any:
        row = await self._fetchone("SELECT value_json FROM settings WHERE key = ?", (key,))
        return json_loads(row["value_json"], default) if row else default

    async def set_import_status(self, value: dict[str, Any]) -> None:
        await self.set_setting("import_status", value)

    async def get_import_status(self) -> dict[str, Any]:
        value = await self.get_setting("import_status", {})
        return value if isinstance(value, dict) else {}

    async def set_owner_id(self, owner_id: int) -> None:
        await self.set_setting("owner_id", int(owner_id))

    async def get_owner_id(self) -> int | None:
        value = await self.get_setting("owner_id")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    async def insert_import_batch(self, records: list[dict[str, Any]]) -> int:
        if not records:
            return 0

        async def operation() -> int:
            dialogs: dict[int, tuple[Any, ...]] = {}
            payload: list[tuple[Any, ...]] = []
            current = now_ts()
            for record in records:
                chat_id = int(record["chat_id"])
                timestamp = float(record.get("timestamp") or 0.0)
                dialogs[chat_id] = (
                    chat_id,
                    int(record.get("contact_id") or chat_id),
                    str(record.get("display_name") or ""),
                    str(record.get("dialog_type") or "personal_chat"),
                    timestamp,
                    timestamp,
                    current,
                )
                payload.append(
                    (
                        chat_id,
                        record.get("sender_id"),
                        record.get("message_id"),
                        timestamp,
                        str(record.get("text") or ""),
                        record.get("reply_to_message_id"),
                        json_dumps(record.get("forwarded_from")),
                        json_dumps(record.get("reactions") or []),
                        str(record.get("message_type") or "message"),
                        chat_id,
                        current,
                    )
                )
            await self.conn.executemany(
                "INSERT INTO dialogs(chat_id, contact_id, display_name, dialog_type, first_timestamp, last_timestamp, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET "
                "contact_id=excluded.contact_id, display_name=excluded.display_name, dialog_type=excluded.dialog_type, "
                "first_timestamp=MIN(dialogs.first_timestamp, excluded.first_timestamp), "
                "last_timestamp=MAX(dialogs.last_timestamp, excluded.last_timestamp), updated_at=excluded.updated_at",
                list(dialogs.values()),
            )
            before = self.conn.total_changes
            await self.conn.executemany(
                "INSERT OR IGNORE INTO messages "
                "(chat_id, sender_id, message_id, timestamp, text, reply_to_message_id, forwarded_from, reactions_json, message_type, dialog_id, imported_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            return max(0, self.conn.total_changes - before)

        return int(await self._write(operation) or 0)

    async def insert_live_message(
        self,
        *,
        chat_id: int,
        sender_id: int,
        message_id: int | None,
        timestamp: float,
        text: str,
        reply_to_message_id: int | None,
        display_name: str = "",
    ) -> None:
        record = {
            "chat_id": chat_id,
            "contact_id": chat_id,
            "display_name": display_name,
            "dialog_type": "personal_chat",
            "sender_id": sender_id,
            "message_id": message_id,
            "timestamp": timestamp,
            "text": text,
            "reply_to_message_id": reply_to_message_id,
        }
        await self.insert_import_batch([record])

    async def chat_ids(self) -> list[int]:
        rows = await self._fetchall("SELECT chat_id FROM dialogs ORDER BY chat_id")
        return [int(row["chat_id"]) for row in rows]

    async def iter_messages(
        self,
        *,
        chat_id: int | None = None,
        sender_id: int | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[dict[str, Any]]:
        last_timestamp = -1.0
        last_id = 0
        while True:
            clauses = ["(timestamp > ? OR (timestamp = ? AND id > ?))"]
            params: list[Any] = [last_timestamp, last_timestamp, last_id]
            if chat_id is not None:
                clauses.append("chat_id = ?")
                params.append(int(chat_id))
            if sender_id is not None:
                clauses.append("sender_id = ?")
                params.append(int(sender_id))
            rows = await self._fetchall(
                "SELECT id, chat_id, sender_id, message_id, timestamp, text, reply_to_message_id "
                "FROM messages WHERE " + " AND ".join(clauses) + " ORDER BY timestamp, id LIMIT ?",
                tuple(params + [batch_size]),
            )
            if not rows:
                return
            for row in rows:
                last_timestamp = float(row["timestamp"])
                last_id = int(row["id"])
                yield dict(row)

    async def get_recent_messages(self, chat_id: int, limit: int) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT chat_id, sender_id, message_id, timestamp, text, reply_to_message_id "
            "FROM messages WHERE chat_id = ? ORDER BY timestamp DESC, id DESC LIMIT ?",
            (int(chat_id), max(1, int(limit))),
        )
        return [dict(row) for row in reversed(rows)]

    async def count_messages(self, *, owner_id: int | None = None) -> int:
        if owner_id is None:
            row = await self._fetchone("SELECT COUNT(*) AS count FROM messages")
        else:
            row = await self._fetchone("SELECT COUNT(*) AS count FROM messages WHERE sender_id = ?", (owner_id,))
        return int(row["count"] if row else 0)

    async def clear_examples_and_rag(self) -> None:
        async def operation() -> None:
            if self.fts_enabled:
                await self.conn.execute("DELETE FROM rag_fts")
            await self.conn.execute("DELETE FROM embeddings")
            await self.conn.execute("DELETE FROM rag_documents")
            await self.conn.execute("DELETE FROM conversation_examples")

        await self._write(operation)

    async def insert_examples(self, examples: list[dict[str, Any]]) -> None:
        if not examples:
            return

        async def operation() -> None:
            payload = [
                (
                    item["chat_id"],
                    item["contact_id"],
                    item["timestamp"],
                    json_dumps(item["context_messages"]),
                    json_dumps(item["owner_response_messages"]),
                    json_dumps(item["owner_message_ids"]),
                    len(item["owner_response_messages"]),
                )
                for item in examples
            ]
            await self.conn.executemany(
                "INSERT OR IGNORE INTO conversation_examples "
                "(chat_id, contact_id, timestamp, context_json, response_json, owner_message_ids_json, bubble_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                payload,
            )

        await self._write(operation)

    async def count_examples(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS count FROM conversation_examples")
        return int(row["count"] if row else 0)

    async def iter_examples(self, *, batch_size: int = 500) -> AsyncIterator[dict[str, Any]]:
        last_id = 0
        while True:
            rows = await self._fetchall(
                "SELECT id, chat_id, contact_id, timestamp, context_json, response_json, bubble_count "
                "FROM conversation_examples WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, batch_size),
            )
            if not rows:
                return
            for row in rows:
                last_id = int(row["id"])
                yield dict(row)

    async def clear_rag_documents(self) -> None:
        async def operation() -> None:
            if self.fts_enabled:
                await self.conn.execute("DELETE FROM rag_fts")
            await self.conn.execute("DELETE FROM embeddings")
            await self.conn.execute("DELETE FROM rag_documents")

        await self._write(operation)

    async def insert_rag_documents(self, documents: list[dict[str, Any]]) -> None:
        if not documents:
            return

        async def operation() -> None:
            await self.conn.executemany(
                "INSERT OR REPLACE INTO rag_documents "
                "(source_example_id, kind, chat_id, contact_id, content, metadata_json, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        item["source_example_id"],
                        item.get("kind", "style"),
                        item["chat_id"],
                        item["contact_id"],
                        item["content"],
                        json_dumps(item.get("metadata") or {}),
                        item["timestamp"],
                    )
                    for item in documents
                ],
            )

        await self._write(operation)

    async def rebuild_fts(self) -> None:
        if not self.fts_enabled:
            return

        async def operation() -> None:
            await self.conn.execute("DELETE FROM rag_fts")
            await self.conn.execute("INSERT INTO rag_fts(rowid, content) SELECT id, content FROM rag_documents")

        await self._write(operation)

    async def search_documents(
        self,
        query: str,
        *,
        chat_id: int,
        same_chat_only: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        if self.fts_enabled:
            terms = [term.replace('"', "") for term in query.split() if len(term) > 1][:12]
            match = " OR ".join(terms)
            if match:
                try:
                    clause = "AND rd.chat_id = ?" if same_chat_only else ""
                    params: list[Any] = [match]
                    if same_chat_only:
                        params.append(chat_id)
                    params.append(max(1, limit))
                    rows = await self._fetchall(
                        "SELECT rd.id, rd.chat_id, rd.contact_id, rd.content, rd.metadata_json, rd.timestamp, "
                        "bm25(rag_fts) AS lexical_score FROM rag_fts "
                        "JOIN rag_documents rd ON rd.id = rag_fts.rowid "
                        "WHERE rag_fts MATCH ? " + clause + " ORDER BY lexical_score LIMIT ?",
                        tuple(params),
                    )
                    return [dict(row) for row in rows]
                except aiosqlite.OperationalError:
                    pass
        clause = "AND chat_id = ?" if same_chat_only else ""
        params = [f"%{query[:120]}%"]
        if same_chat_only:
            params.append(chat_id)
        params.append(max(1, limit))
        rows = await self._fetchall(
            "SELECT id, chat_id, contact_id, content, metadata_json, timestamp, 0.0 AS lexical_score "
            "FROM rag_documents WHERE content LIKE ? " + clause + " ORDER BY timestamp DESC LIMIT ?",
            tuple(params),
        )
        return [dict(row) for row in rows]

    async def embedding_rows(self, *, chat_id: int, same_chat_only: bool, limit: int) -> list[dict[str, Any]]:
        clause = "WHERE rd.chat_id = ?" if same_chat_only else ""
        params: tuple[Any, ...] = (chat_id, limit) if same_chat_only else (limit,)
        rows = await self._fetchall(
            "SELECT rd.id, rd.chat_id, rd.contact_id, rd.content, rd.metadata_json, rd.timestamp, "
            "e.model, e.vector_json, e.dimensions FROM embeddings e JOIN rag_documents rd ON rd.id = e.doc_id "
            + clause + " ORDER BY rd.timestamp DESC LIMIT ?",
            params,
        )
        return [dict(row) for row in rows]

    async def documents_without_embeddings(self, model: str, *, after_id: int, limit: int) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT rd.id, rd.content FROM rag_documents rd LEFT JOIN embeddings e ON e.doc_id = rd.id AND e.model = ? "
            "WHERE rd.id > ? AND e.doc_id IS NULL ORDER BY rd.id LIMIT ?",
            (model, after_id, limit),
        )
        return [dict(row) for row in rows]

    async def store_embeddings(self, model: str, values: list[tuple[int, list[float]]]) -> None:
        if not values:
            return

        async def operation() -> None:
            current = now_ts()
            await self.conn.executemany(
                "INSERT INTO embeddings(doc_id, model, vector_json, dimensions, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(doc_id) DO UPDATE SET model=excluded.model, vector_json=excluded.vector_json, "
                "dimensions=excluded.dimensions, updated_at=excluded.updated_at",
                [(doc_id, model, json_dumps(vector), len(vector), current) for doc_id, vector in values],
            )

        await self._write(operation)

    async def count_rag_documents(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS count FROM rag_documents")
        return int(row["count"] if row else 0)

    async def count_embeddings(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS count FROM embeddings")
        return int(row["count"] if row else 0)

    async def set_personality_profile(self, owner_id: int, profile: dict[str, Any]) -> None:
        async def operation() -> None:
            await self.conn.execute(
                "INSERT INTO personality_profiles(owner_id, profile_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(owner_id) DO UPDATE SET profile_json=excluded.profile_json, updated_at=excluded.updated_at",
                (owner_id, json_dumps(profile), now_ts()),
            )

        await self._write(operation)

    async def get_personality_profile(self, owner_id: int) -> dict[str, Any]:
        row = await self._fetchone("SELECT profile_json FROM personality_profiles WHERE owner_id = ?", (owner_id,))
        value = json_loads(row["profile_json"], {}) if row else {}
        return value if isinstance(value, dict) else {}

    async def set_relationship_profile(self, contact_id: int, chat_id: int, profile: dict[str, Any]) -> None:
        async def operation() -> None:
            await self.conn.execute(
                "INSERT INTO relationship_profiles(contact_id, chat_id, profile_json, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(contact_id) DO UPDATE SET chat_id=excluded.chat_id, profile_json=excluded.profile_json, updated_at=excluded.updated_at",
                (contact_id, chat_id, json_dumps(profile), now_ts()),
            )

        await self._write(operation)

    async def get_relationship_profile(self, contact_id: int) -> dict[str, Any]:
        row = await self._fetchone("SELECT profile_json FROM relationship_profiles WHERE contact_id = ?", (contact_id,))
        value = json_loads(row["profile_json"], {}) if row else {}
        return value if isinstance(value, dict) else {}

    async def count_relationships(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS count FROM relationship_profiles")
        return int(row["count"] if row else 0)

    async def get_dialog(self, chat_id: int) -> dict[str, Any]:
        row = await self._fetchone("SELECT * FROM dialogs WHERE chat_id = ?", (chat_id,))
        return dict(row) if row else {}

    async def get_summary(self, chat_id: int) -> dict[str, Any]:
        row = await self._fetchone("SELECT summary_json FROM conversation_summaries WHERE chat_id = ?", (chat_id,))
        value = json_loads(row["summary_json"], {}) if row else {}
        return value if isinstance(value, dict) else {}

    async def set_summary(self, chat_id: int, summary: dict[str, Any], source_message_count: int) -> None:
        async def operation() -> None:
            await self.conn.execute(
                "INSERT INTO conversation_summaries(chat_id, summary_json, source_message_count, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET summary_json=excluded.summary_json, "
                "source_message_count=excluded.source_message_count, updated_at=excluded.updated_at",
                (chat_id, json_dumps(summary), source_message_count, now_ts()),
            )

        await self._write(operation)

    async def memory_for_chat(self, *, chat_id: int, contact_id: int, limit: int) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            "SELECT fact, scope, confidence, chat_id, contact_id, created_at, expires_at FROM memories "
            "WHERE (expires_at IS NULL OR expires_at > ?) AND "
            "(scope = 'global' OR (scope = 'chat' AND chat_id = ?) OR (scope = 'person' AND contact_id = ?)) "
            "ORDER BY confidence DESC, created_at DESC LIMIT ?",
            (now_ts(), chat_id, contact_id, max(1, limit)),
        )
        return [dict(row) for row in rows]

    async def add_memories(self, memories: list[ApprovedMemory]) -> int:
        if not memories:
            return 0

        async def operation() -> int:
            await self.conn.executemany(
                "INSERT INTO memories(fact, scope, confidence, chat_id, contact_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (item.fact, item.scope, item.confidence, item.chat_id, item.contact_id, now_ts(), item.expires_at)
                    for item in memories
                ],
            )
            return len(memories)

        return int(await self._write(operation) or 0)

    async def clear_memories(self, *, chat_id: int | None = None) -> int:
        async def operation() -> int:
            if chat_id is None:
                cursor = await self.conn.execute("DELETE FROM memories")
            else:
                cursor = await self.conn.execute("DELETE FROM memories WHERE chat_id = ?", (chat_id,))
            return int(cursor.rowcount or 0)

        return int(await self._write(operation) or 0)

    async def count_memories(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) AS count FROM memories")
        return int(row["count"] if row else 0)

    async def set_chat_control(
        self,
        chat_id: int,
        *,
        enabled: bool | None = None,
        allowed: bool | None = None,
        denied: bool | None = None,
        display_name: str | None = None,
    ) -> None:
        existing = await self.get_chat_control(chat_id)
        payload = {
            "enabled": int(existing.get("enabled", 1) if enabled is None else bool(enabled)),
            "allowed": int(existing.get("allowed", 0) if allowed is None else bool(allowed)),
            "denied": int(existing.get("denied", 0) if denied is None else bool(denied)),
            "display_name": str(existing.get("display_name", "") if display_name is None else display_name),
        }

        async def operation() -> None:
            await self.conn.execute(
                "INSERT INTO chat_controls(chat_id, enabled, allowed, denied, display_name, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled, allowed=excluded.allowed, "
                "denied=excluded.denied, display_name=excluded.display_name, updated_at=excluded.updated_at",
                (chat_id, payload["enabled"], payload["allowed"], payload["denied"], payload["display_name"], now_ts()),
            )

        await self._write(operation)

    async def get_chat_control(self, chat_id: int) -> dict[str, Any]:
        row = await self._fetchone("SELECT * FROM chat_controls WHERE chat_id = ?", (chat_id,))
        return dict(row) if row else {}

    async def allowed_chat_ids(self) -> list[int]:
        rows = await self._fetchall("SELECT chat_id FROM chat_controls WHERE allowed = 1 AND denied = 0 ORDER BY chat_id")
        return [int(row["chat_id"]) for row in rows]

    async def denied_chat_ids(self) -> list[int]:
        rows = await self._fetchall("SELECT chat_id FROM chat_controls WHERE denied = 1 ORDER BY chat_id")
        return [int(row["chat_id"]) for row in rows]

    async def queue_created(self, *, generation_id: str, chat_id: int, sender_id: int, buffered_count: int, created_at: float) -> None:
        async def operation() -> None:
            await self.conn.execute(
                "INSERT OR REPLACE INTO queue_history(generation_id, chat_id, sender_id, buffered_count, created_at, status) "
                "VALUES (?, ?, ?, ?, ?, 'waiting')",
                (generation_id, chat_id, sender_id, buffered_count, created_at),
            )

        await self._write(operation)

    async def queue_status(self, generation_id: str, status: str, *, error_kind: str | None = None) -> None:
        now = now_ts()
        started_at = now if status == "running" else None
        finished_at = now if status in {"done", "failed", "cancelled"} else None

        async def operation() -> None:
            await self.conn.execute(
                "UPDATE queue_history SET status = ?, started_at = COALESCE(?, started_at), "
                "finished_at = COALESCE(?, finished_at), error_kind = ? WHERE generation_id = ?",
                (status, started_at, finished_at, error_kind, generation_id),
            )

        await self._write(operation)

    async def statistics(self, owner_id: int | None) -> dict[str, Any]:
        total = await self.count_messages()
        owner_messages = await self.count_messages(owner_id=owner_id) if owner_id is not None else 0
        return {
            "messages": total,
            "owner_messages": owner_messages,
            "dialogs": len(await self.chat_ids()),
            "examples": await self.count_examples(),
            "relationships": await self.count_relationships(),
            "rag_documents": await self.count_rag_documents(),
            "embeddings": await self.count_embeddings(),
            "memories": await self.count_memories(),
            "db_size": self.path.stat().st_size if self.path.exists() else 0,
        }
