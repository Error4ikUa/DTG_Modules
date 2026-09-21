from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import deque
from collections.abc import Awaitable, Callable

from .schemas import InboundBubble, QueueItem


Processor = Callable[[QueueItem], Awaitable[None]]


class GlobalFIFOQueue:
    """One global worker. A pending chat can be coalesced but never reprioritized."""

    def __init__(self, processor: Processor) -> None:
        self._processor = processor
        self._items: deque[QueueItem] = deque()
        self._pending_by_chat: dict[int, QueueItem] = {}
        self._running: QueueItem | None = None
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self._closed = False
        self._worker: asyncio.Task | None = None

    async def start(self) -> None:
        async with self._lock:
            if self._worker and not self._worker.done():
                return
            self._worker = asyncio.create_task(self._run())

    async def enqueue(self, chat_id: int, sender_id: int, messages: list[InboundBubble]) -> tuple[QueueItem, bool]:
        async with self._lock:
            if self._closed:
                raise RuntimeError("DigitalMe queue is closed")
            pending = self._pending_by_chat.get(chat_id)
            if pending is not None:
                pending.messages.extend(messages)
                return pending, True
            item = QueueItem(
                chat_id=chat_id,
                sender_id=sender_id,
                messages=list(messages),
                generation_id=uuid.uuid4().hex,
            )
            self._items.append(item)
            self._pending_by_chat[chat_id] = item
            self._ready.set()
            return item, False

    async def _next(self) -> QueueItem | None:
        while True:
            await self._ready.wait()
            async with self._lock:
                if self._closed:
                    return None
                if not self._items:
                    self._ready.clear()
                    continue
                item = self._items.popleft()
                self._pending_by_chat.pop(item.chat_id, None)
                self._running = item
                if not self._items:
                    self._ready.clear()
                return item

    async def _finish(self, item: QueueItem) -> None:
        async with self._lock:
            if self._running is item:
                self._running = None

    async def _run(self) -> None:
        while True:
            item = await self._next()
            if item is None:
                return
            try:
                await self._processor(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The module processor records a safe owner-visible diagnostic.
                pass
            finally:
                await self._finish(item)

    async def snapshot(self) -> tuple[QueueItem | None, list[QueueItem]]:
        async with self._lock:
            return self._running, list(self._items)

    async def clear_pending(self) -> list[QueueItem]:
        async with self._lock:
            removed = list(self._items)
            self._items.clear()
            self._pending_by_chat.clear()
            self._ready.clear()
            return removed

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            worker = self._worker
            self._items.clear()
            self._pending_by_chat.clear()
            self._ready.set()
        if worker:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
