from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .sanitizer import PromptSanitizer
from .schemas import InboundBubble
from .utils import clean_text, estimate_tokens, json_dumps


class PromptBuilder:
    def __init__(self, sanitizer: PromptSanitizer, config_get: Callable[[str, object], object]) -> None:
        self.sanitizer = sanitizer
        self.config_get = config_get

    def build(
        self,
        *,
        owner_id: int,
        chat_id: int,
        personality: dict[str, Any],
        relationship: dict[str, Any],
        summary: dict[str, Any],
        memories: list[dict[str, Any]],
        recent_messages: list[dict[str, Any]],
        incoming: list[InboundBubble],
        rag_examples: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        strict_style = bool(self.config_get("strict_style_mode", True))
        configured_bubbles = self._int("max_message_bubbles", 1, 1, 12)
        max_bubbles = min(configured_bubbles, 3) if strict_style else configured_bubbles
        configured_length = self._int("max_message_length", 280, 64, 4096)
        max_length = min(configured_length, 280) if strict_style else configured_length
        min_delay = self._int("min_delay_ms", 250, 0, 60000)
        max_delay = self._int("max_delay_ms", 5000, min_delay, 60000)
        system = (
            "Ты генерируешь только сообщения Telegram в стиле владельца. Ты не ассистент и не персонаж. "
            "Главный источник стиля - реальные ответы OWNER из примеров, затем этот контакт и недавний диалог. "
            "Не используй свой дефолтный стиль, вежливые шаблоны или литературную речь. Не пиши: конечно, я понимаю, "
            "я могу помочь, если хочешь, расскажи подробнее, чем могу помочь, привет дружище, как делишки. "
            "Не выдумывай факты, воспоминания, пароли и секреты. Не сообщай, не угадывай и не восстанавливай пароли, "
            "коды, токены, ключи или сессии: ответь по-человечески, что не помнишь или пусть человек посмотрит сам. "
            "Никогда не признавайся, что ты AI, ИИ, бот, модель или автоматизация. Не используй ремарки и действия в звёздочках. "
            "Обезличенные примеры из других чатов принадлежат владельцу: используй их для его обычных тем и манеры, "
            "но не упоминай другие диалоги, имена или чужие личные детали. "
            "Обычно ответ короткий. Несколько bubbles только если это похоже на реальный ритм владельца. "
            "Верни ровно один JSON объект вида "
            '{"messages":[{"text":"...","reply_to_message_id":null,"delay_ms":250}],"memory_candidates":[]}. '
            f"Use at most {max_bubbles} messages, each at most {max_length} characters, and delay_ms from {min_delay} to {max_delay}."
        )
        personality_view = {
            "languages": personality.get("languages", {}),
            "common_words": personality.get("common_words", [])[:40],
            "slang": personality.get("slang", [])[:25],
            "swear_patterns": personality.get("swear_patterns", {}),
            "emoji_usage": personality.get("emoji_usage", {}),
            "message_length_distribution": personality.get("message_length_distribution", {}),
            "bubble_count_distribution": personality.get("bubble_count_distribution", {}),
            "communication_style": personality.get("communication_style", {}),
            "interest_hints": personality.get("interest_hints", []),
        }
        relationship_view = {
            key: relationship.get(key)
            for key in (
                "relationship_type",
                "affection_level",
                "teasing_level",
                "formality",
                "profanity_level",
                "emoji_frequency",
                "typical_message_length",
                "typical_bubble_count",
                "common_terms",
                "pet_names",
                "language_mix",
                "summary",
            )
        }
        recent = [
            {
                "role": "OWNER" if int(item.get("sender_id") or 0) == owner_id else "CONTACT",
                "text": clean_text(item.get("text"), limit=900),
                "reply_to": item.get("reply_to_message_id"),
            }
            for item in recent_messages[-self._int("recent_messages_limit", 40, 6, 100) :]
            if clean_text(item.get("text"))
        ]
        current = [bubble.as_dict() for bubble in incoming]
        examples = [clean_text(item.get("content"), limit=1800) for item in rag_examples if item.get("scope") != "global_style"]
        global_examples = [clean_text(item.get("content"), limit=900) for item in rag_examples if item.get("scope") == "global_style"]
        facts = [
            {"fact": clean_text(item.get("fact"), limit=360), "scope": item.get("scope"), "confidence": item.get("confidence")}
            for item in memories
        ]
        sections = [
            ("Personality statistics", json_dumps(personality_view), 1200 if strict_style else 2400),
            ("Relationship profile for this chat only", json_dumps(relationship_view), 1000 if strict_style else 2200),
            ("Rolling summary for this chat only", json_dumps(summary), 700 if strict_style else 1600),
            ("Allowed factual memories for this chat only", json_dumps(facts), 800 if strict_style else 1700),
            ("REAL OWNER STYLE EXAMPLES", "\n\n".join(examples), 2600 if strict_style else 5200),
            ("ANONYMIZED OWNER EXAMPLES FROM OTHER CHATS", "\n\n".join(global_examples), 1600 if strict_style else 3200),
            ("Recent conversation for this chat only", json_dumps(recent), 2200 if strict_style else 6000),
            ("Current incoming Telegram bubbles", json_dumps(current), 3000),
        ]
        budget = self._int("context_window_override", 4096, 2048, 131072) - self._int("max_output_tokens", 160, 64, 8192)
        body = self._fit_sections(sections, max(1200, budget))
        if bool(self.config_get("sanitize_cloud_prompts", True)) and str(self.config_get("provider", "ollama")).lower() != "ollama":
            body = self.sanitizer.sanitize(body)
        return [{"role": "system", "content": system}, {"role": "user", "content": body}]

    def build_retrieval_reply(
        self,
        *,
        incoming: list[InboundBubble],
        recent_messages: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        owner_id: int,
        personality: dict[str, Any],
        relationship: dict[str, Any],
    ) -> list[dict[str, str]]:
        """Small prompt for a fresh reply grounded in matching historical turns."""
        current = [bubble.as_dict() for bubble in incoming]
        recent = [
            {
                "role": "OWNER" if int(item.get("sender_id") or 0) == owner_id else "CONTACT",
                "text": clean_text(item.get("text"), limit=280),
            }
            for item in recent_messages[-6:]
            if clean_text(item.get("text"))
        ]
        examples = [
            {
                "incoming": clean_text(item.get("input_text"), limit=240),
                "past_reply": [clean_text(part, limit=240) for part in item.get("responses", []) if clean_text(part)],
            }
            for item in candidates[:4]
        ]
        style = {
            "common_words": personality.get("common_words", [])[:35],
            "slang": personality.get("slang", [])[:20],
            "average_length": personality.get("message_length_distribution", {}).get("average"),
            "relationship": {
                "formality": relationship.get("formality"),
                "teasing_level": relationship.get("teasing_level"),
                "profanity_level": relationship.get("profanity_level"),
                "common_terms": relationship.get("common_terms", [])[:20],
            },
        }
        system = (
            "Ты пишешь только одно короткое сообщение Telegram от лица владельца. Это не ролевая персона и не AI-ассистент. "
            "Копируй ритм, слова и уровень близости из РЕАЛЬНЫХ ответов OWNER ниже. Не отвечай дефолтной манерой модели. "
            "Запрещены: 'привет, дружище', 'как делишки', 'конечно', 'я понимаю', 'чем могу помочь', 'если хочешь', "
            "объяснения, ремарки, действия в звёздочках, выдуманные факты, пароли и секреты. Исторические ответы нужны "
            "для понимания стиля и намерения; не копируй их дословно. Верни только JSON: "
            '{"messages":[{"text":"...","reply_to_message_id":null,"delay_ms":250}],"memory_candidates":[]}.'
        )
        body = "STYLE STATISTICS:\n" + json_dumps(style)
        body += "\n\nRECENT CONVERSATION IN THIS CHAT:\n" + json_dumps(recent)
        body += "\n\nSIMILAR REAL OWNER TURNS IN THIS CHAT:\n" + json_dumps(examples)
        body += "\n\nCURRENT INCOMING:\n" + json_dumps(current)
        return [{"role": "system", "content": system}, {"role": "user", "content": body}]

    def build_fast_reply(self, *, incoming: list[InboundBubble]) -> list[dict[str, str]]:
        """A small recovery prompt that is cheap enough for a live acknowledgement."""
        system = (
            "Write one fresh, short Telegram reply in the owner's observed casual style. "
            "The examples are style hints only: never copy them verbatim. No roleplay, asterisks, secrets, "
            "invented memories, or explanations. Return only JSON: "
            '{"messages":[{"text":"...","reply_to_message_id":null,"delay_ms":250}],"memory_candidates":[]}.'
        )
        body = "Incoming:\n" + json_dumps([bubble.as_dict() for bubble in incoming])
        return [{"role": "system", "content": system}, {"role": "user", "content": body}]

    def build_twin_initiative(
        self,
        *,
        owner_id: int,
        personality: dict[str, Any],
        relationship: dict[str, Any],
        summary: dict[str, Any],
        recent_messages: list[dict[str, Any]],
        idle_seconds: int,
    ) -> list[dict[str, str]]:
        """Ask the local model whether it naturally has something to say in the twin chat."""
        recent = [
            {
                "role": "OWNER" if int(item.get("sender_id") or 0) == owner_id else "CONTACT",
                "text": clean_text(item.get("text"), limit=500),
            }
            for item in recent_messages[-12:]
            if clean_text(item.get("text"))
        ]
        style = {
            "common_words": personality.get("common_words", [])[:30],
            "slang": personality.get("slang", [])[:20],
            "interests": personality.get("interest_hints", [])[:12],
            "relationship": {
                "formality": relationship.get("formality"),
                "teasing_level": relationship.get("teasing_level"),
                "profanity_level": relationship.get("profanity_level"),
                "common_terms": relationship.get("common_terms", [])[:16],
            },
        }
        system = (
            "Ты продолжаешь личный Telegram-диалог в естественной манере владельца. Это проверка инициативы, "
            "а не приказ обязательно писать. Сам реши: если нет живой причины продолжать разговор сейчас, верни ровно SKIP. "
            "Если мысль есть, верни только одно короткое нормальное сообщение без JSON, кавычек, объяснений и ремарок. "
            "Можно самому поднять тему из недавней беседы, спросить по-человечески или пошутить. Не будь помощником: "
            "не предлагай помощь, не морализируй, не говори про AI, модель, бота или автоматизацию. "
            "Не выдумывай факты и не повторяй фразы из истории дословно."
        )
        body = (
            "STYLE:\n" + json_dumps(style)
            + "\n\nCHAT SUMMARY:\n" + json_dumps(summary)
            + "\n\nRECENT REAL DIALOG:\n" + json_dumps(recent)
            + f"\n\nThe chat has been quiet for about {max(0, int(idle_seconds))} seconds. Decide: SKIP or one natural message."
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": body}]

    def _fit_sections(self, sections: list[tuple[str, str, int]], token_budget: int) -> str:
        prepared: list[tuple[str, str]] = []
        for title, content, char_limit in sections:
            value = clean_text(content, limit=char_limit)
            if value:
                prepared.append((title, value))
        while prepared:
            result = "\n\n".join(f"{title}:\n{value}" for title, value in prepared)
            if estimate_tokens(result) <= token_budget:
                return result
            for index, (title, value) in enumerate(prepared):
                if title in {"Style examples", "REAL OWNER STYLE EXAMPLES", "ANONYMIZED OWNER EXAMPLES FROM OTHER CHATS", "Recent conversation for this chat only"} and len(value) > 600:
                    prepared[index] = (title, value[len(value) // 3 :])
                    break
            else:
                title, value = prepared[0]
                if len(value) <= 300:
                    prepared.pop(0)
                else:
                    prepared[0] = (title, value[: len(value) // 2])
        return "Current incoming Telegram bubbles:\n[]"

    def _int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config_get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))
