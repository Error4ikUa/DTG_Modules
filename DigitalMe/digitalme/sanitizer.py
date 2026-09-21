from __future__ import annotations

import re
from collections.abc import Iterable


PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d() .-]{7,}\d)(?!\w)")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
TOKEN_RE = re.compile(r"\b(?:sk|rk|pk|ghp|xox[baprs])[-_A-Za-z0-9]{12,}\b", re.IGNORECASE)
AUTH_RE = re.compile(r"\b(?:bearer|token|password|session|api[_ -]?key)\s*[:=]\s*[^\s,;]{6,}", re.IGNORECASE)
USERNAME_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{4,}")


class PromptSanitizer:
    def sanitize(self, text: str) -> str:
        value = PHONE_RE.sub("<PHONE>", text)
        value = EMAIL_RE.sub("<EMAIL>", value)
        value = TOKEN_RE.sub("<TOKEN>", value)
        return AUTH_RE.sub("<SECRET>", value)

    def anonymize_style_example(self, text: str, names: Iterable[str] = ()) -> str:
        value = self.sanitize(text)
        for name in sorted({str(item).strip() for item in names if str(item).strip()}, key=len, reverse=True):
            value = re.sub(re.escape(name), "<PERSON>", value, flags=re.IGNORECASE)
        return USERNAME_RE.sub("<PERSON>", value)
