from __future__ import annotations

from collections.abc import Callable

from .database import DigitalMeDatabase
from .memory import MemoryEvaluator
from .prompt_builder import PromptBuilder
from .providers import ProviderError, ProviderRouter
from .rag import RAGService
from .schemas import GenerationResult, QueueItem, needs_style_retry, parse_generation_response


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
        personality = await self.database.get_personality_profile(self.owner_id)
        relationship = await self.database.get_relationship_profile(item.sender_id)
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
                personality=personality,
                relationship=relationship,
            )
            completion = await self._complete_with_recovery(
                prompt, item,
                thinking_override=thinking_override,
                max_tokens_override=self._int("fast_reply_max_tokens", 80, 64, 256),
            )
        else:
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
            completion = await self._complete_with_recovery(prompt, item, thinking_override=thinking_override)
        self.last_completion = completion
        result = await self._parse_completion(completion.content, item)
        if needs_style_retry(result):
            correction = [
                *prompt,
                {"role": "assistant", "content": completion.content},
                {"role": "user", "content": "Перепиши. Слишком похоже на AI. Коротко, без вежливых шаблонов и без действий в звёздочках. Верни только JSON."},
            ]
            completion = await self._complete_with_recovery(correction, item, thinking_override=False)
            self.last_completion = completion
            result = await self._parse_completion(completion.content, item)
        return result

    async def _parse_completion(self, content: str, item: QueueItem) -> GenerationResult | None:
        reply_ids = {bubble.message_id for bubble in item.messages if bubble.message_id is not None}
        strict_style = bool(self.config_get("strict_style_mode", True))
        configured_length = self._int("max_message_length", 280, 32, 4096)
        profile = await self.database.get_personality_profile(self.owner_id)
        observed = float(profile.get("message_length_distribution", {}).get("average") or 0)
        multiplier = self._float("max_style_length_multiplier", 3.0, 1.0, 8.0)
        style_length = int(max(32, observed * multiplier)) if observed else configured_length
        result = parse_generation_response(
            content,
            max_bubbles=min(self._int("max_message_bubbles", 1, 1, 12), 3) if strict_style else self._int("max_message_bubbles", 1, 1, 12),
            max_message_length=min(configured_length, style_length, 280) if strict_style else min(configured_length, style_length),
            min_delay_ms=self._int("min_delay_ms", 250, 0, 60000),
            max_delay_ms=self._int("max_delay_ms", 5000, 0, 60000),
            allowed_reply_ids={int(item) for item in reply_ids},
        )
        if result:
            approved = self.memory_evaluator.evaluate(result.memory_candidates, chat_id=item.chat_id, contact_id=item.sender_id)
            await self.database.add_memories(approved)
        return result

    async def _complete_with_recovery(
        self,
        prompt: list[dict[str, str]],
        item: QueueItem,
        *,
        thinking_override: bool | None,
        max_tokens_override: int | None = None,
    ):
        try:
            return await self.provider.complete(
                prompt,
                thinking_override=thinking_override,
                max_tokens_override=max_tokens_override,
            )
        except ProviderError as exc:
            if exc.kind != "empty_completion":
                raise
            # A compact recovery is far more likely to complete than repeating the
            # same large prompt after Ollama supplied no final text.
            recovery = self.prompt_builder.build_fast_reply(incoming=item.messages)
            return await self.provider.complete(
                recovery,
                thinking_override=False,
                max_tokens_override=self._int("fast_reply_max_tokens", 80, 64, 256),
            )

    def _int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config_get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _float(self, key: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(self.config_get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))
