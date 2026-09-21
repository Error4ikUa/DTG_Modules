from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from .utils import clean_text


MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PROBE_RESPONSE_BYTES = 96 * 1024
DEFAULT_BASE_URLS = {
    "ollama": "http://127.0.0.1:11434",
    "openrouter": "https://openrouter.ai/api/v1",
    "openai_compatible": "http://127.0.0.1:1234/v1",
    "lm_studio": "http://127.0.0.1:1234/v1",
}
THINK_TAG_RE = re.compile(r"</?(?P<tag>think|thinking)\b[^>]*>", re.IGNORECASE)


class ProviderError(RuntimeError):
    def __init__(self, kind: str, *, retryable: bool = True) -> None:
        super().__init__(kind)
        self.kind = kind
        self.retryable = retryable


@dataclass(slots=True)
class Completion:
    content: str
    provider: str
    model: str
    latency_ms: float
    ttft_ms: float | None = None
    thinking_requested: bool = False
    thinking_supported: bool | None = None
    warning: str | None = None


def strip_reasoning_blocks(text: str) -> str:
    """Remove only complete, explicit reasoning blocks; never guess at normal text."""
    stack: list[tuple[str, int]] = []
    spans: list[tuple[int, int]] = []
    for match in THINK_TAG_RE.finditer(text):
        tag = str(match.group("tag") or "").lower()
        if match.group(0).startswith("</"):
            if stack and stack[-1][0] == tag:
                _open_tag, start = stack.pop()
                if not stack:
                    spans.append((start, match.end()))
        else:
            stack.append((tag, match.start()))
    if not spans:
        return clean_text(text, limit=MAX_API_RESPONSE_BYTES)
    parts: list[str] = []
    position = 0
    for start, end in spans:
        parts.append(text[position:start])
        position = end
    parts.append(text[position:])
    return clean_text("".join(parts), limit=MAX_API_RESPONSE_BYTES)


class ProviderRouter:
    def __init__(self, session: aiohttp.ClientSession, config_get: Callable[[str, Any], Any]) -> None:
        self.session = session
        self.config_get = config_get
        self._cooldown_until = 0.0
        self._ollama_thinking_capabilities: dict[str, bool | None] = {}
        self.last_completion: Completion | None = None

    def _value(self, key: str, default: Any = None) -> Any:
        return self.config_get(key, default)

    def _provider(self) -> str:
        provider = str(self._value("provider", "ollama") or "ollama").strip().lower().replace("-", "_")
        return provider if provider in DEFAULT_BASE_URLS else "ollama"

    def _base_url(self, provider: str) -> str:
        configured = str(self._value("base_url", "") or "").strip()
        return (configured or DEFAULT_BASE_URLS[provider]).rstrip("/")

    def _models(self) -> list[str]:
        primary = str(self._value("model", "") or "").strip()
        fallback_raw = self._value("fallback_models", "")
        if isinstance(fallback_raw, str):
            fallback = [part.strip() for part in fallback_raw.split(",") if part.strip()]
        elif isinstance(fallback_raw, (list, tuple)):
            fallback = [str(part).strip() for part in fallback_raw if str(part).strip()]
        else:
            fallback = []
        return [item for item in [primary, *fallback] if item]

    def _timeout(self) -> aiohttp.ClientTimeout:
        try:
            total = float(self._value("timeout_seconds", 60))
        except (TypeError, ValueError):
            total = 60.0
        return aiohttp.ClientTimeout(total=max(5.0, min(300.0, total)))

    def _ollama_keep_alive(self) -> str | int:
        try:
            minutes = int(self._value("ollama_keep_alive_minutes", 10))
        except (TypeError, ValueError):
            minutes = 10
        return 0 if minutes <= 0 else f"{min(120, minutes)}m"

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        thinking_override: bool | None = None,
        max_tokens_override: int | None = None,
    ) -> Completion:
        now = time.monotonic()
        if self._cooldown_until > now:
            raise ProviderError("provider_cooldown", retryable=False)
        models = self._models()
        if not models:
            raise ProviderError("model_not_configured", retryable=False)
        try:
            retries = max(0, min(4, int(self._value("max_retries", 2))))
        except (TypeError, ValueError):
            retries = 2
        try:
            backoff = max(0.2, min(15.0, float(self._value("retry_backoff", 1.0))))
        except (TypeError, ValueError):
            backoff = 1.0
        last_error: ProviderError | None = None
        for model in models:
            for attempt in range(retries + 1):
                try:
                    result = await self._complete_once(
                        model,
                        messages,
                        thinking_override=thinking_override,
                        max_tokens_override=max_tokens_override,
                    )
                    self._cooldown_until = 0.0
                    self.last_completion = result
                    return result
                except ProviderError as exc:
                    last_error = exc
                    if not exc.retryable or attempt >= retries:
                        break
                    await asyncio.sleep(backoff * (attempt + 1))
        try:
            cooldown = max(0.0, min(120.0, float(self._value("provider_cooldown", 8))))
        except (TypeError, ValueError):
            cooldown = 8.0
        self._cooldown_until = time.monotonic() + cooldown
        raise last_error or ProviderError("provider_failed")

    async def _complete_once(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        thinking_override: bool | None = None,
        max_tokens_override: int | None = None,
    ) -> Completion:
        provider = self._provider()
        if provider == "ollama":
            return await self._ollama_complete(
                model, messages, thinking_override=thinking_override, max_tokens_override=max_tokens_override
            )
        return await self._openai_complete(
            provider, model, messages, thinking_override=thinking_override, max_tokens_override=max_tokens_override
        )

    @staticmethod
    def _status_error(status: int) -> ProviderError | None:
        if status == 429:
            return ProviderError("rate_limited")
        if status >= 500:
            return ProviderError("provider_server_error")
        if status < 200 or status >= 300:
            return ProviderError(f"http_{status}", retryable=status in {408, 409, 425})
        return None

    async def _read_json(self, response: aiohttp.ClientResponse, *, max_bytes: int = MAX_API_RESPONSE_BYTES) -> dict[str, Any]:
        payload = await response.content.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ProviderError("response_too_large", retryable=False)
        error = self._status_error(response.status)
        if error:
            raise error
        try:
            value = json.loads(payload.decode("utf-8", errors="replace"))
        except (TypeError, ValueError) as exc:
            raise ProviderError("invalid_provider_json") from exc
        if not isinstance(value, dict):
            raise ProviderError("invalid_provider_payload")
        return value

    async def _openai_complete(
        self,
        provider: str,
        model: str,
        messages: list[dict[str, str]],
        *,
        thinking_override: bool | None = None,
        max_tokens_override: int | None = None,
    ) -> Completion:
        started = time.monotonic()
        base_url = self._base_url(provider)
        endpoint = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        api_key = str(self._value("api_key", "") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": float(self._value("temperature", 0.8)),
            "top_p": float(self._value("top_p", 0.9)),
            "max_tokens": int(max_tokens_override or self._value("max_output_tokens", 1000)),
            "response_format": {"type": "json_object"},
        }
        try:
            async with self.session.post(endpoint, json=payload, headers=headers, timeout=self._timeout()) as response:
                data = await self._read_json(response)
        except asyncio.TimeoutError as exc:
            raise ProviderError("timeout") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError("network_unavailable") from exc
        try:
            message = data["choices"][0]["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("missing_completion") from exc
        if isinstance(content, list):
            content = "".join(str(item.get("text") or "") if isinstance(item, dict) else str(item) for item in content)
        text = strip_reasoning_blocks(str(content or ""))
        if not text:
            raise ProviderError("empty_completion")
        return Completion(
            content=text,
            provider=provider,
            model=model,
            latency_ms=round((time.monotonic() - started) * 1000, 2),
            ttft_ms=None,
            thinking_requested=bool(self._value("enable_thinking", False)) if thinking_override is None else bool(thinking_override),
            thinking_supported=None,
        )

    async def _ollama_thinking_capability(self, model: str) -> bool | None:
        base_url = self._base_url("ollama")
        cache_key = f"{base_url}|{model}"
        if cache_key in self._ollama_thinking_capabilities:
            return self._ollama_thinking_capabilities[cache_key]
        version_ok = False
        try:
            async with self.session.get(base_url + "/api/version", timeout=self._timeout()) as response:
                version_ok = 200 <= response.status < 300
                await response.content.read(MAX_PROBE_RESPONSE_BYTES)
        except (asyncio.TimeoutError, aiohttp.ClientError):
            version_ok = False
        capabilities: list[str] | None = None
        try:
            async with self.session.post(base_url + "/api/show", json={"name": model}, timeout=self._timeout()) as response:
                if 200 <= response.status < 300:
                    payload = await self._read_json(response, max_bytes=MAX_PROBE_RESPONSE_BYTES)
                    raw = payload.get("capabilities")
                    if isinstance(raw, list):
                        capabilities = [str(item).lower() for item in raw]
                else:
                    await response.content.read(MAX_PROBE_RESPONSE_BYTES)
        except (ProviderError, asyncio.TimeoutError, aiohttp.ClientError):
            capabilities = None
        supported: bool | None = "thinking" in capabilities if capabilities is not None else None
        # A reachable older server without /api/show remains usable; it gets a normal request.
        if not version_ok and capabilities is None:
            supported = None
        self._ollama_thinking_capabilities[cache_key] = supported
        return supported

    async def _ollama_complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        thinking_override: bool | None = None,
        max_tokens_override: int | None = None,
    ) -> Completion:
        started = time.monotonic()
        base_url = self._base_url("ollama")
        endpoint = base_url if base_url.endswith("/api/chat") else base_url + "/api/chat"
        thinking_requested = bool(self._value("enable_thinking", False)) if thinking_override is None else bool(thinking_override)
        thinking_supported = await self._ollama_thinking_capability(model)
        warning = None
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "format": "json",
            "keep_alive": self._ollama_keep_alive(),
            "options": {
                "temperature": float(self._value("temperature", 0.8)),
                "top_p": float(self._value("top_p", 0.9)),
                "num_predict": int(max_tokens_override or self._value("max_output_tokens", 1000)),
            },
        }
        if thinking_supported is True:
            # Native Ollama API flag; never emulate this with a prompt or CLI command.
            payload["think"] = thinking_requested
        else:
            warning = "ollama_thinking_parameter_unavailable"
        parts: list[str] = []
        ttft_ms: float | None = None
        total_duration_ms: float | None = None
        received_bytes = 0
        try:
            async with self.session.post(endpoint, json=payload, timeout=self._timeout()) as response:
                error = self._status_error(response.status)
                if error:
                    await response.content.read(MAX_PROBE_RESPONSE_BYTES)
                    raise error
                async for raw_line in response.content:
                    received_bytes += len(raw_line)
                    if received_bytes > MAX_API_RESPONSE_BYTES:
                        raise ProviderError("response_too_large", retryable=False)
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line.decode("utf-8", errors="replace"))
                    except (TypeError, ValueError) as exc:
                        raise ProviderError("invalid_stream_json") from exc
                    if not isinstance(chunk, dict):
                        continue
                    message = chunk.get("message")
                    if isinstance(message, dict):
                        # Ignore separate thinking/reasoning fields. Only final content is retained.
                        content = str(message.get("content") or "")
                        if content:
                            if ttft_ms is None:
                                ttft_ms = round((time.monotonic() - started) * 1000, 2)
                            parts.append(content)
                    if chunk.get("done") and isinstance(chunk.get("total_duration"), (int, float)):
                        total_duration_ms = round(float(chunk["total_duration"]) / 1_000_000, 2)
        except asyncio.TimeoutError as exc:
            raise ProviderError("timeout") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError("ollama_unavailable") from exc
        text = strip_reasoning_blocks("".join(parts))
        if not text:
            raise ProviderError("empty_completion")
        return Completion(
            content=text,
            provider="ollama",
            model=model,
            latency_ms=total_duration_ms or round((time.monotonic() - started) * 1000, 2),
            ttft_ms=ttft_ms,
            thinking_requested=thinking_requested,
            thinking_supported=thinking_supported,
            warning=warning,
        )

    async def embed(self, texts: list[str], model: str) -> list[list[float]]:
        provider = self._provider()
        if provider == "ollama":
            return await self._ollama_embed(texts, model)
        return await self._openai_embed(provider, texts, model)

    async def _openai_embed(self, provider: str, texts: list[str], model: str) -> list[list[float]]:
        base_url = self._base_url(provider)
        endpoint = base_url if base_url.endswith("/embeddings") else base_url + "/embeddings"
        headers = {"Content-Type": "application/json"}
        api_key = str(self._value("api_key", "") or "").strip()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        try:
            async with self.session.post(endpoint, json={"model": model, "input": texts}, headers=headers, timeout=self._timeout()) as response:
                data = await self._read_json(response)
        except asyncio.TimeoutError as exc:
            raise ProviderError("embedding_timeout") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError("embedding_network_unavailable") from exc
        rows = data.get("data")
        if not isinstance(rows, list):
            raise ProviderError("invalid_embedding_payload")
        vectors = [item.get("embedding") for item in rows if isinstance(item, dict)]
        if len(vectors) != len(texts) or not all(isinstance(vector, list) for vector in vectors):
            raise ProviderError("invalid_embedding_payload")
        return [[float(value) for value in vector] for vector in vectors]

    async def _ollama_embed(self, texts: list[str], model: str) -> list[list[float]]:
        base_url = self._base_url("ollama")
        endpoint = base_url + "/api/embed"
        try:
            async with self.session.post(endpoint, json={"model": model, "input": texts}, timeout=self._timeout()) as response:
                data = await self._read_json(response)
        except asyncio.TimeoutError as exc:
            raise ProviderError("embedding_timeout") from exc
        except aiohttp.ClientError as exc:
            raise ProviderError("ollama_unavailable") from exc
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ProviderError("invalid_embedding_payload")
        if not all(isinstance(vector, list) for vector in vectors):
            raise ProviderError("invalid_embedding_payload")
        return [[float(value) for value in vector] for vector in vectors]
