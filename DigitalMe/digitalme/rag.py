from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .database import DigitalMeDatabase
from .embeddings import EmbeddingService, cosine_similarity
from .sanitizer import PromptSanitizer
from .utils import clean_text, json_loads


class RAGService:
    def __init__(
        self,
        database: DigitalMeDatabase,
        embeddings: EmbeddingService,
        sanitizer: PromptSanitizer,
        config_get: Callable[[str, object], object],
    ) -> None:
        self.database = database
        self.embeddings = embeddings
        self.sanitizer = sanitizer
        self.config_get = config_get

    async def rebuild_documents(self, progress_callback=None) -> int:
        await self.database.clear_rag_documents()
        batch: list[dict[str, Any]] = []
        count = 0
        async for example in self.database.iter_examples():
            context = json_loads(example.get("context_json"), [])
            response = json_loads(example.get("response_json"), [])
            if not isinstance(context, list) or not isinstance(response, list):
                continue
            content = "Incoming:\n" + "\n".join(clean_text(item) for item in context)
            content += "\nOwner response:\n" + "\n".join(clean_text(item) for item in response)
            batch.append(
                {
                    "source_example_id": int(example["id"]),
                    "kind": "style",
                    "chat_id": int(example["chat_id"]),
                    "contact_id": int(example["contact_id"]),
                    "content": content[:12000],
                    "metadata": {"bubble_count": int(example.get("bubble_count") or 1)},
                    "timestamp": float(example.get("timestamp") or 0.0),
                }
            )
            if len(batch) >= 500:
                await self.database.insert_rag_documents(batch)
                count += len(batch)
                batch.clear()
                if progress_callback:
                    await progress_callback({"phase": "building_rag", "documents": count})
        if batch:
            await self.database.insert_rag_documents(batch)
            count += len(batch)
        await self.database.rebuild_fts()
        return count

    async def rebuild_embeddings(self, progress_callback=None) -> int:
        model = str(self.config_get("embedding_model", "") or "").strip()
        if not model:
            return 0
        last_id = 0
        done = 0
        while True:
            rows = await self.database.documents_without_embeddings(model, after_id=last_id, limit=32)
            if not rows:
                return done
            vectors = await self.embeddings.encode([str(item["content"]) for item in rows])
            if len(vectors) != len(rows):
                return done
            await self.database.store_embeddings(model, [(int(item["id"]), vector) for item, vector in zip(rows, vectors)])
            last_id = int(rows[-1]["id"])
            done += len(rows)
            if progress_callback:
                await progress_callback({"phase": "building_embeddings", "documents": done})

    async def search(
        self,
        query: str,
        *,
        chat_id: int,
        contact_id: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        same_chat_only = not bool(self.config_get("cross_contact_style_examples", False))
        lexical = await self.database.search_documents(query, chat_id=chat_id, same_chat_only=same_chat_only, limit=max(limit * 3, 12))
        scores: dict[int, float] = {}
        documents: dict[int, dict[str, Any]] = {}
        for rank, document in enumerate(lexical):
            doc_id = int(document["id"])
            documents[doc_id] = document
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (rank + 1)

        query_vectors = await self.embeddings.encode([query])
        if query_vectors:
            for document in await self.database.embedding_rows(chat_id=chat_id, same_chat_only=same_chat_only, limit=1200):
                vector = json_loads(document.get("vector_json"), [])
                if not isinstance(vector, list):
                    continue
                semantic = cosine_similarity(query_vectors[0], [float(value) for value in vector])
                if semantic <= 0:
                    continue
                doc_id = int(document["id"])
                documents[doc_id] = document
                scores[doc_id] = scores.get(doc_id, 0.0) + semantic

        ranked = sorted(
            documents.values(),
            key=lambda item: (
                scores.get(int(item["id"]), 0.0)
                + (0.18 if int(item.get("contact_id") or 0) == contact_id else 0.0)
            ),
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        seen_content: set[str] = set()
        for document in ranked:
            content = str(document.get("content") or "")
            marker = content[:240]
            if not content or marker in seen_content:
                continue
            seen_content.add(marker)
            if not same_chat_only:
                content = self.sanitizer.anonymize_style_example(content)
            item = dict(document)
            item["content"] = content
            selected.append(item)
            if len(selected) >= max(1, limit):
                break
        return selected
