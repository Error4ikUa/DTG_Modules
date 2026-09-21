from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .utils import clean_text, clamp


SENSITIVE_FACT_RE = re.compile(
    r"\b(password|passcode|token|api[_ -]?key|secret|session|парол|токен|ключ|сесс)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ApprovedMemory:
    fact: str
    scope: str
    confidence: float
    chat_id: int | None
    contact_id: int | None
    expires_at: float | None = None


class MemoryEvaluator:
    """Applies conservative local policy before an LLM suggestion reaches SQLite."""

    def evaluate(self, candidates: list[dict[str, Any]], *, chat_id: int, contact_id: int) -> list[ApprovedMemory]:
        approved: list[ApprovedMemory] = []
        seen: set[str] = set()
        for item in candidates[:5]:
            fact = clean_text(item.get("fact"), limit=420)
            if len(fact) < 8 or SENSITIVE_FACT_RE.search(fact):
                continue
            key = fact.lower()
            if key in seen:
                continue
            seen.add(key)
            scope = str(item.get("scope") or "chat").lower()
            if scope not in {"chat", "person", "global"}:
                scope = "chat"
            try:
                confidence = float(item.get("confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.5
            confidence = float(clamp(confidence, 0.0, 1.0))
            if confidence < 0.55:
                continue
            # A model never gets to turn a private fact into a global one by itself.
            if scope == "global":
                scope = "chat"
            approved.append(
                ApprovedMemory(
                    fact=fact,
                    scope=scope,
                    confidence=confidence,
                    chat_id=chat_id if scope == "chat" else None,
                    contact_id=contact_id if scope == "person" else None,
                )
            )
        return approved
