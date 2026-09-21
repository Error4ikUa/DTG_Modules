from __future__ import annotations

from collections.abc import Callable

from .database import DigitalMeDatabase
from .memory import MemoryEvaluator
from .prompt_builder import PromptBuilder
from .providers import ProviderRouter
from .rag import RAGService
from .schemas import GenerationResult, QueueItem, parse_generation_response


class GenerationEngine:
    def __init__(
        self,
        database: DigitalMeDatabase,
        provider: ProviderRouter,
        rag: RAGService,
        prompt_builder: PromptBuilder,
        memory_evaluator: MemoryEvaluator,
        config_get: Callable[[str, object], object],
        owner_id: int,
    ) -> None:
        self.database = database
        self.provider = provider
        self.rag = rag
        self.prompt_builder = prompt_builder
        self.memory_evaluator = memory_evaluator
        self.config_get = config_get
        self.owner_id = owner_id
        self.last_completion = None

    async def generate(self, item: QueueItem, *, thinking_override: bool | None = None) -> GenerationResult | None:
        current_text = "\n".join(bubble.text for bubble in item.messages)
        recent_limit = self._int("recent_messages_limit", 40, 6, 100)
        recent = await self.database.get_recent_messages(item.chat_id, recent_limit)
        candidates = []
        if bool(self.config_get("retrieval_first", True)):
            candidates = await self.database.find_reply_candidates(
                chat_id=item.chat_id,
                incoming=current_text,
                limit=self._int("retrieval_candidate_limit", 4, 1, 8),
            )
        if candidates:
            prompt = self.prompt_builder.build_retrieval_reply(
                incoming=item.messages,
                recent_messages=recent,
                candidates=candidates,
                owner_id=self.owner_id,
            )
            completion = await self.provider.complete(
                prompt,
                thinking_override=thinking_override,
                max_tokens_override=self._int("fast_reply_max_tokens", 80, 64, 256),
            )
        else:
            personality = await self.database.get_personality_profile(self.owner_id)
            relationship = await self.database.get_relationship_profile(item.sender_id)
            summary = await self.database.get_summary(item.chat_id)
            memories = await self.database.memory_for_chat(chat_id=item.chat_id, contact_id=item.sender_id, limit=12)
            rag_limit = self._int("rag_result_count", 10, 0, 20)
            examples = await self.rag.search(current_text, chat_id=item.chat_id, contact_id=item.sender_id, limit=rag_limit) if rag_limit else []
            prompt = self.prompt_builder.build(
                owner_id=self.owner_id,
                chat_id=item.chat_id,
                personality=personality,
                relationship=relationship,
                summary=summary,
                memories=memories,
                recent_messages=recent,
                incoming=item.messages,
                rag_examples=examples,
            )
            completion = await self.provider.complete(prompt, thinking_override=thinking_override)
        self.last_completion = completion
        reply_ids = {bubble.message_id for bubble in item.messages if bubble.message_id is not None}
        strict_style = bool(self.config_get("strict_style_mode", True))
        configured_length = self._int("max_message_length", 280, 64, 4096)
        result = parse_generation_response(
            completion.content,
            max_bubbles=min(self._int("max_message_bubbles", 1, 1, 12), 3) if strict_style else self._int("max_message_bubbles", 1, 1, 12),
            max_message_length=min(configured_length, 280) if strict_style else configured_length,
            min_delay_ms=self._int("min_delay_ms", 250, 0, 60000),
            max_delay_ms=self._int("max_delay_ms", 5000, 0, 60000),
            allowed_reply_ids={int(item) for item in reply_ids},
        )
        if result:
            approved = self.memory_evaluator.evaluate(result.memory_candidates, chat_id=item.chat_id, contact_id=item.sender_id)
            await self.database.add_memories(approved)
        return result

    def _int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config_get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))
