from __future__ import annotations

import re
from collections.abc import Iterable


PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d() .-]{7,}\d)(?!\w)")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
TOKEN_RE = re.compile(r"\b(?:sk|rk|pk|ghp|xox[baprs])[-_A-Za-z0-9]{12,}\b", re.IGNORECASE)
AUTH_RE = re.compile(r"\b(?:bearer|token|password|session|api[_ -]?key)\s*[:=]\s*[^\s,;]{6,}", re.IGNORECASE)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|\b(?:парол|токен|ключ|сессия)\s*[:=]\s*[^\s,;]{4,}", re.IGNORECASE)
USERNAME_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{4,}")


class PromptSanitizer:
    def contains_sensitive(self, text: str) -> bool:
        return bool(PHONE_RE.search(text) or EMAIL_RE.search(text) or TOKEN_RE.search(text) or AUTH_RE.search(text) or PRIVATE_KEY_RE.search(text))

    def sanitize(self, text: str) -> str:
        value = PHONE_RE.sub("<PHONE>", text)
        value = EMAIL_RE.sub("<EMAIL>", value)
        value = TOKEN_RE.sub("<TOKEN>", value)
        value = AUTH_RE.sub("<SECRET>", value)
        return PRIVATE_KEY_RE.sub("<SECRET>", value)

    def anonymize_style_example(self, text: str, names: Iterable[str] = ()) -> str:
        value = self.sanitize(text)
        for name in sorted({str(item).strip() for item in names if str(item).strip()}, key=len, reverse=True):
            value = re.sub(re.escape(name), "<PERSON>", value, flags=re.IGNORECASE)
        return USERNAME_RE.sub("<PERSON>", value)
