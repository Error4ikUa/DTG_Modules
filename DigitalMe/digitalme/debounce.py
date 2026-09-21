from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Awaitable, Callable

from .schemas import InboundBubble


FlushCallback = Callable[[int, int, list[InboundBubble]], Awaitable[None]]


class PerChatDebounce:
    """Keeps Telegram message bubbles intact while coalescing short bursts."""

    def __init__(self, delay_seconds: Callable[[], float], flush_callback: FlushCallback) -> None:
        self._delay_seconds = delay_seconds
        self._flush_callback = flush_callback
        self._buffers: dict[int, list[InboundBubble]] = defaultdict(list)
        self._sender_ids: dict[int, int] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def add(self, chat_id: int, sender_id: int, bubble: InboundBubble) -> None:
        async with self._lock:
            if self._closed:
                return
            self._buffers[chat_id].append(bubble)
            self._sender_ids[chat_id] = sender_id
            previous = self._tasks.get(chat_id)
            if previous and not previous.done():
                previous.cancel()
            self._tasks[chat_id] = asyncio.create_task(self._wait_and_flush(chat_id))

    async def _wait_and_flush(self, chat_id: int) -> None:
        try:
            await asyncio.sleep(max(0.1, float(self._delay_seconds())))
        except asyncio.CancelledError:
            return
        async with self._lock:
            if self._closed:
                return
            bubbles = self._buffers.pop(chat_id, [])
            sender_id = self._sender_ids.pop(chat_id, 0)
            self._tasks.pop(chat_id, None)
        if bubbles and sender_id:
            await self._flush_callback(chat_id, sender_id, bubbles)

    async def pending_count(self, chat_id: int) -> int:
        async with self._lock:
            return len(self._buffers.get(chat_id, []))

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            tasks = list(self._tasks.values())
            self._tasks.clear()
            self._buffers.clear()
            self._sender_ids.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
