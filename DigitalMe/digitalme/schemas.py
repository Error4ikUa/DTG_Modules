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
ROLEPLAY_ACTION_RE = re.compile(r"\*[^*\r\n]{1,500}\*", re.UNICODE)
GENERIC_ASSISTANT_RE = re.compile(
    r"\b(?:конечно|я понимаю|я могу помочь|если хочешь|если хотите|расскаж(?:и|ите) подробнее|"
    r"чем могу помочь|давайте разбер[её]мся|могу предложить|я постараюсь|мне очень жаль|как делишки)\b",
    re.IGNORECASE,
)
PLACEHOLDER_RE = re.compile(r"<[^>\r\n]{1,100}>")
STRUCTURED_OUTPUT_RE = re.compile(r"[\[{]\s*\"?(?:messages|text|memory_candidates)\"?\s*[:\"]", re.IGNORECASE)
REPEATED_CHARACTER_RE = re.compile(r"([A-Za-zА-Яа-яЁё])\1{10,}")
AUTOMATION_ADMISSION_RE = re.compile(
    r"\b(?:я\s+(?:являюсь\s+)?(?:бот(?:ом)?|ии|ai|нейросетью|моделью|программой|искусственным\s+интеллектом)|"
    r"это\s+(?:бот|ии|ai|нейросеть|digitalme)|digitalme)\b",
    re.IGNORECASE,
)
AUTOMATION_PROBE_RE = re.compile(r"\b(?:ты|вы)\s+(?:бот|ии|ai|нейросеть|модель|программа)\b", re.IGNORECASE)
CREDENTIAL_REQUEST_RE = re.compile(
    r"\b(?:парол\w*|pass(?:word|code)?|токен\w*|token\w*|api[_ -]?key|сесси\w*|"
    r"код(?:\s+(?:подтверждения|входа|из\s+смс))?|2fa|otp)\b",
    re.IGNORECASE,
)


def _clean_generated_text(value: Any, *, limit: int) -> str:
    """Drop theatrical action prefixes before Telegram receives a model response."""
    return clean_text(ROLEPLAY_ACTION_PREFIX_RE.sub("", clean_text(value, limit=limit)), limit=limit)


def is_unsafe_generated_text(text: str) -> bool:
    """Reject model transport artifacts and obvious loops before they reach Telegram."""
    value = clean_text(text, limit=4096)
    letters = "".join(char.lower() for char in value if char.isalpha())
    low_diversity_loop = len(letters) >= 32 and len(set(letters)) <= 3
    return bool(
        PLACEHOLDER_RE.search(value)
        or STRUCTURED_OUTPUT_RE.search(value)
        or REPEATED_CHARACTER_RE.search(value)
        or low_diversity_loop
        or AUTOMATION_ADMISSION_RE.search(value)
    )


def is_credential_request(text: str) -> bool:
    return bool(CREDENTIAL_REQUEST_RE.search(clean_text(text, limit=2000)))


def is_automation_probe(text: str) -> bool:
    return bool(AUTOMATION_PROBE_RE.search(clean_text(text, limit=2000)))


def needs_style_retry(result: GenerationResult | None) -> bool:
    if not result:
        return False
    text = "\n".join(message.text for message in result.messages)
    return bool(ROLEPLAY_ACTION_RE.search(text) or GENERIC_ASSISTANT_RE.search(text) or is_unsafe_generated_text(text))


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
        if REASONING_MARKER_RE.search(fallback) or is_unsafe_generated_text(fallback):
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
        if not text or is_unsafe_generated_text(text):
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
