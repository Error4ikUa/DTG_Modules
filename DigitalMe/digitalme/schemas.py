from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .utils import clean_text, clamp, now_ts


REASONING_MARKER_RE = re.compile(
    r"</?think(?:ing)?\b|\b(?:analysis|reasoning(?:_content)?|thinking)\s*[:=]",
    re.IGNORECASE,
)
ROLEPLAY_ACTION_PREFIX_RE = re.compile(r"^\s*\*[^*\r\n]{1,500}\*\s*", re.UNICODE)


def _clean_generated_text(value: Any, *, limit: int) -> str:
    """Drop theatrical action prefixes before Telegram receives a model response."""
    return clean_text(ROLEPLAY_ACTION_PREFIX_RE.sub("", clean_text(value, limit=limit)), limit=limit)


@dataclass(slots=True)
class InboundBubble:
    message_id: int | None
    text: str
    timestamp: float
    reply_to_message_id: int | None = None
    reply_text: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "text": self.text,
            "timestamp": self.timestamp,
            "reply_to_message_id": self.reply_to_message_id,
            "reply_text": self.reply_text,
        }


@dataclass(slots=True)
class QueueItem:
    chat_id: int
    sender_id: int
    messages: list[InboundBubble]
    created_at: float = field(default_factory=now_ts)
    priority: int = 0
    generation_id: str = ""
    started_at: float | None = None

    @property
    def buffered_count(self) -> int:
        return len(self.messages)


@dataclass(slots=True)
class GeneratedBubble:
    text: str
    reply_to_message_id: int | None = None
    delay_ms: int = 0


@dataclass(slots=True)
class GenerationResult:
    messages: list[GeneratedBubble]
    memory_candidates: list[dict[str, Any]] = field(default_factory=list)


def _decode_object(raw: str) -> dict[str, Any] | None:
    payload = raw.strip()
    if payload.startswith("```"):
        payload = payload.split("\n", 1)[1] if "\n" in payload else ""
        if payload.rstrip().endswith("```"):
            payload = payload.rstrip()[:-3]
    decoder = json.JSONDecoder()
    for index, char in enumerate(payload):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(payload[index:])
        except (TypeError, ValueError):
            continue
        return value if isinstance(value, dict) else None
    return None


def parse_generation_response(
    raw: str,
    *,
    max_bubbles: int,
    max_message_length: int,
    min_delay_ms: int,
    max_delay_ms: int,
    allowed_reply_ids: set[int],
) -> GenerationResult | None:
    """Validate an untrusted LLM result without ever evaluating generated code."""
    parsed = _decode_object(raw)
    if parsed is None:
        fallback = _clean_generated_text(raw, limit=max_message_length)
        if REASONING_MARKER_RE.search(fallback):
            return None
        return GenerationResult([GeneratedBubble(fallback)]) if fallback else None

    source_messages = parsed.get("messages")
    if not isinstance(source_messages, list):
        return None

    messages: list[GeneratedBubble] = []
    for value in source_messages[: max(1, max_bubbles)]:
        if not isinstance(value, dict):
            continue
        text = _clean_generated_text(value.get("text"), limit=max_message_length)
        if not text:
            continue
        raw_delay = value.get("delay_ms", 0)
        try:
            delay_ms = int(raw_delay)
        except (TypeError, ValueError):
            delay_ms = min_delay_ms
        delay_ms = int(clamp(delay_ms, min_delay_ms, max_delay_ms))
        raw_reply = value.get("reply_to_message_id")
        try:
            reply_id = int(raw_reply) if raw_reply is not None else None
        except (TypeError, ValueError):
            reply_id = None
        if reply_id not in allowed_reply_ids:
            reply_id = None
        messages.append(GeneratedBubble(text=text, reply_to_message_id=reply_id, delay_ms=delay_ms))

    if not messages:
        return None
    candidates = parsed.get("memory_candidates")
    safe_candidates = [item for item in candidates if isinstance(item, dict)] if isinstance(candidates, list) else []
    return GenerationResult(messages=messages, memory_candidates=safe_candidates[:5])
