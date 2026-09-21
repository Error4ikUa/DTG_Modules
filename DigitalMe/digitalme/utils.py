from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable


WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
ID_RE = re.compile(r"(-?\d+)$")


def now_ts() -> float:
    return time.time()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed


def clamp(value: int | float, low: int | float, high: int | float) -> int | float:
    return max(low, min(high, value))


def extract_numeric_id(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    raw = str(value).strip()
    match = ID_RE.search(raw)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def parse_timestamp(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def flatten_telegram_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(flatten_telegram_text(item) for item in value)
    if isinstance(value, dict):
        return flatten_telegram_text(value.get("text") or value.get("value") or "")
    return ""


def clean_text(value: Any, *, limit: int | None = None) -> str:
    text = CONTROL_RE.sub("", flatten_telegram_text(value)).strip()
    if limit is not None:
        return text[:limit]
    return text


def tokenize(text: str) -> list[str]:
    return [word.lower() for word in WORD_RE.findall(text) if len(word) > 1]


def estimate_tokens(text: str) -> int:
    """Conservative multilingual estimate when a provider tokenizer is unknown."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 3.2))


def short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()[:16]


def safe_display(value: Any, *, limit: int = 80) -> str:
    return clean_text(value, limit=limit).replace("\n", " ")


def message_is_cancellation(text: str) -> bool:
    normalized = " ".join(tokenize(text))
    return normalized in {
        "ne vazhno",
        "zabey",
        "zabei",
        "ne nado",
        "otboi",
        "ignore",
        "never mind",
    } or text.strip().lower() in {"неважно", "забей", "не надо", "отбой"}


def compact_lines(items: Iterable[str], *, limit: int) -> list[str]:
    selected: list[str] = []
    used = 0
    for item in items:
        value = clean_text(item)
        if not value:
            continue
        if used + len(value) > limit:
            remainder = max(0, limit - used)
            if remainder >= 24:
                selected.append(value[:remainder])
            break
        selected.append(value)
        used += len(value)
    return selected


def utc_iso(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(timestamp or now_ts(), tz=timezone.utc).isoformat()
