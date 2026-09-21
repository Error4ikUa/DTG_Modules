from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    import ijson
except ImportError:  # Dependency is installed with the module; keeping import errors actionable helps manual installs.
    ijson = None

from .database import DigitalMeDatabase
from .utils import clean_text, extract_numeric_id, flatten_telegram_text, parse_timestamp


class ImportCancelled(RuntimeError):
    pass


class ImportFormatError(RuntimeError):
    """The Telegram export is syntactically incomplete or otherwise malformed."""


@dataclass(slots=True)
class ImportStats:
    phase: str = "idle"
    bytes_read: int = 0
    file_size: int = 0
    messages: int = 0
    owner_messages: int = 0
    imported_messages: int = 0
    dialogs: int = 0
    skipped: int = 0

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data["progress"] = round((self.bytes_read / self.file_size) * 100, 1) if self.file_size else 0.0
        return data


class _EventObjectBuilder:
    """Builds one ijson object only; the export itself is never materialized."""

    def __init__(self) -> None:
        self.value: Any = None
        self._stack: list[list[Any]] = []

    def feed(self, event: str, value: Any) -> None:
        if event == "start_map":
            container: dict[str, Any] = {}
            self._append(container)
            self._stack.append([container, None])
        elif event == "start_array":
            container = []
            self._append(container)
            self._stack.append([container, None])
        elif event == "map_key":
            if self._stack and isinstance(self._stack[-1][0], dict):
                self._stack[-1][1] = value
        elif event in {"string", "number", "boolean", "null"}:
            self._append(value)
        elif event in {"end_map", "end_array"} and self._stack:
            self._stack.pop()

    def _append(self, value: Any) -> None:
        if not self._stack:
            self.value = value
            return
        parent, key = self._stack[-1]
        if isinstance(parent, list):
            parent.append(value)
        elif key is not None:
            parent[key] = value
            self._stack[-1][1] = None


def is_private_export_chat(chat_type: Any) -> bool:
    return str(chat_type or "").lower() in {"personal_chat", "private_chat"}


def normalize_export_message(chat: dict[str, Any], message: dict[str, Any], owner_id: int) -> dict[str, Any] | None:
    """Normalize Telegram Desktop export records without using display names as identity."""
    if not is_private_export_chat(chat.get("type")):
        return None
    if str(message.get("type") or "message").lower() != "message":
        return None
    text = clean_text(message.get("text"))
    if not text:
        return None
    chat_id = extract_numeric_id(chat.get("id"))
    if chat_id is None or chat_id == owner_id:
        return None
    sender_id = extract_numeric_id(message.get("from_id") or message.get("sender_id"))
    message_id = extract_numeric_id(message.get("id"))
    reply_id = extract_numeric_id(message.get("reply_to_message_id") or message.get("reply_to"))
    timestamp = parse_timestamp(message.get("date_unixtime") or message.get("date"))
    return {
        "chat_id": chat_id,
        "contact_id": chat_id,
        "display_name": clean_text(chat.get("name"), limit=160),
        "dialog_type": str(chat.get("type") or "personal_chat"),
        "sender_id": sender_id,
        "message_id": message_id,
        "timestamp": timestamp,
        "text": text,
        "reply_to_message_id": reply_id,
        "forwarded_from": message.get("forwarded_from"),
        "reactions": message.get("reactions") or [],
        "message_type": "message",
        "is_owner": sender_id == owner_id,
    }


class TelegramExportImporter:
    def __init__(self, database: DigitalMeDatabase, *, batch_size: int = 500) -> None:
        self.database = database
        self.batch_size = max(50, batch_size)
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    async def import_file(
        self,
        path: Path,
        *,
        owner_id: int,
        progress_callback=None,
    ) -> ImportStats:
        if ijson is None:
            raise RuntimeError("ijson is required for Telegram JSON import; reinstall DigitalMe requirements")
        if not path.exists() or path.suffix.lower() != ".json":
            raise ValueError("Expected a local Telegram result.json export")
        self._cancel_event.clear()
        stats = ImportStats(phase="parsing", file_size=path.stat().st_size)
        import_run_id = time.time()
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=6)
        loop = asyncio.get_running_loop()
        producer = asyncio.create_task(asyncio.to_thread(self._produce, path, owner_id, loop, queue, stats))
        last_progress = 0.0
        seen_chats: set[int] = set()
        try:
            while True:
                kind, value = await queue.get()
                if kind == "batch":
                    batch = value
                    inserted = await self.database.insert_import_batch(batch, import_run_id=import_run_id)
                    stats.imported_messages += inserted
                    seen_chats.update(int(record["chat_id"]) for record in batch)
                    stats.dialogs = len(seen_chats)
                elif kind == "progress":
                    stats.bytes_read = int(value)
                elif kind == "done":
                    stats.bytes_read = stats.file_size
                    break
                elif kind == "cancelled":
                    raise ImportCancelled("Import cancelled by owner")
                elif kind == "error":
                    if value == "incomplete_json":
                        raise ImportFormatError("Telegram result.json ended before the JSON document was complete")
                    raise RuntimeError("Telegram export parser failed")
                if progress_callback and time.monotonic() - last_progress > 1.0:
                    last_progress = time.monotonic()
                    await progress_callback(stats.payload())
            stats.phase = "parsed"
            await self.database.set_import_status(stats.payload())
            if progress_callback:
                await progress_callback(stats.payload())
            return stats
        except ImportCancelled:
            await self.database.rollback_import_run(import_run_id)
            stats.phase = "cancelled"
            await self.database.set_import_status(stats.payload())
            raise
        except Exception:
            await self.database.rollback_import_run(import_run_id)
            stats.phase = "failed"
            await self.database.set_import_status(stats.payload())
            raise
        finally:
            if not producer.done():
                self._cancel_event.set()
                producer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await producer

    def _produce(
        self,
        path: Path,
        owner_id: int,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[tuple[str, Any]],
        stats: ImportStats,
    ) -> None:
        def put(kind: str, value: Any) -> None:
            future = asyncio.run_coroutine_threadsafe(queue.put((kind, value)), loop)
            while True:
                try:
                    future.result(timeout=0.2)
                    return
                except FutureTimeoutError:
                    if self._cancel_event.is_set():
                        future.cancel()
                        raise ImportCancelled()

        batch: list[dict[str, Any]] = []
        last_progress = 0.0
        try:
            for record, bytes_read in self._stream_records(path, owner_id):
                if self._cancel_event.is_set():
                    put("cancelled", None)
                    return
                stats.bytes_read = bytes_read
                if record is None:
                    stats.skipped += 1
                    continue
                stats.messages += 1
                if record.get("is_owner"):
                    stats.owner_messages += 1
                record.pop("is_owner", None)
                batch.append(record)
                if len(batch) >= self.batch_size:
                    put("batch", list(batch))
                    batch.clear()
                current = time.monotonic()
                if current - last_progress >= 0.75:
                    last_progress = current
                    put("progress", bytes_read)
            if batch:
                put("batch", list(batch))
            put("done", None)
        except ImportCancelled:
            put("cancelled", None)
        except Exception as exc:
            incomplete = ijson is not None and isinstance(exc, ijson.common.IncompleteJSONError)
            put("error", "incomplete_json" if incomplete else "parser_error")

    def _stream_records(self, path: Path, owner_id: int):
        if ijson is None:
            raise RuntimeError("ijson is unavailable")
        current_chat: dict[str, Any] = {}
        builder: _EventObjectBuilder | None = None
        message_prefix = "chats.list.item.messages.item"
        with path.open("rb") as handle:
            for prefix, event, value in ijson.parse(handle):
                if self._cancel_event.is_set():
                    raise ImportCancelled()
                if builder is not None:
                    builder.feed(event, value)
                    if prefix == message_prefix and event == "end_map":
                        message = builder.value
                        builder = None
                        record = normalize_export_message(current_chat, message if isinstance(message, dict) else {}, owner_id)
                        yield record, handle.tell()
                    continue
                if prefix == "chats.list.item" and event == "start_map":
                    current_chat = {}
                    continue
                if prefix == "chats.list.item" and event == "end_map":
                    current_chat = {}
                    continue
                if prefix in {"chats.list.item.id", "chats.list.item.name", "chats.list.item.type"} and event in {
                    "string",
                    "number",
                    "boolean",
                    "null",
                }:
                    current_chat[prefix.rsplit(".", 1)[-1]] = value
                    continue
                if prefix == message_prefix and event == "start_map":
                    builder = _EventObjectBuilder()
                    builder.feed(event, value)
