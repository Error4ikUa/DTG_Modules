from __future__ import annotations

import asyncio
import math
from collections.abc import Callable

from .providers import ProviderError, ProviderRouter

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    SentenceTransformer = None


class EmbeddingService:
    def __init__(self, provider: ProviderRouter, config_get: Callable[[str, object], object]) -> None:
        self.provider = provider
        self.config_get = config_get
        self._model_name = ""
        self._model = None
        self._lock = asyncio.Lock()

    async def encode(self, texts: list[str]) -> list[list[float]]:
        mode = str(self.config_get("embedding_mode", "local") or "local").lower()
        model = str(self.config_get("embedding_model", "") or "").strip()
        if not texts or not model:
            return []
        if mode == "off":
            return []
        if mode == "remote":
            try:
                return await self.provider.embed(texts, model)
            except ProviderError:
                return []
        if SentenceTransformer is None:
            return []
        async with self._lock:
            if self._model is None or self._model_name != model:
                try:
                    self._model = await asyncio.to_thread(SentenceTransformer, model)
                    self._model_name = model
                except Exception:
                    self._model = None
                    return []
            try:
                encoded = await asyncio.to_thread(self._model.encode, texts, normalize_embeddings=True)
            except Exception:
                return []
        return [[float(value) for value in row] for row in encoded]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)
