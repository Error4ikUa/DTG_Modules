from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .database import DigitalMeDatabase
from .utils import clean_text, compact_lines, json_loads, reply_key, tokenize


EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]", re.UNICODE)
UKRAINIAN_RE = re.compile("[\u0456\u0406\u0457\u0407\u0454\u0404\u0491\u0490]")
CYRILLIC_RE = re.compile("[\u0400-\u04FF]")
LATIN_RE = re.compile("[A-Za-z]")
PROFANITY_STEMS = ("бля", "бл", "сука", "хуй", "пизд", "еб", "fuck", "shit")
AFFECTION_TERMS = ("люб", "зай", "кот", "сонц", "мила", "серд")
TEASING_TERMS = ("лол", "кек", "слаб", "дур", "клоун", "рофл")


@dataclass(slots=True)
class TurnGrouper:
    owner_id: int
    chat_id: int
    contact_id: int
    gap_seconds: float
    max_context_messages: int = 20
    _context: list[dict[str, Any]] = field(default_factory=list, init=False)
    _response: list[dict[str, Any]] = field(default_factory=list, init=False)
    _last_owner_timestamp: float | None = field(default=None, init=False)

    def push(self, message: dict[str, Any]) -> dict[str, Any] | None:
        sender_id = message.get("sender_id")
        timestamp = float(message.get("timestamp") or 0.0)
        if sender_id == self.owner_id:
            if self._response and self._last_owner_timestamp is not None and timestamp - self._last_owner_timestamp <= self.gap_seconds:
                self._response.append(message)
                self._last_owner_timestamp = timestamp
                return None
            completed = self._flush()
            self._response = [message]
            self._last_owner_timestamp = timestamp
            return completed

        completed = self._flush()
        if self._context and timestamp - float(self._context[-1].get("timestamp") or 0.0) > self.gap_seconds:
            self._context = []
        self._context.append(message)
        self._context = self._context[-self.max_context_messages :]
        return completed

    def finish(self) -> dict[str, Any] | None:
        return self._flush()

    def _flush(self) -> dict[str, Any] | None:
        if not self._context or not self._response:
            self._response = []
            self._last_owner_timestamp = None
            return None
        example = {
            "chat_id": self.chat_id,
            "contact_id": self.contact_id,
            "timestamp": float(self._response[-1].get("timestamp") or 0.0),
            "context_messages": [clean_text(item.get("text")) for item in self._context if clean_text(item.get("text"))],
            "owner_response_messages": [clean_text(item.get("text")) for item in self._response if clean_text(item.get("text"))],
            "owner_message_ids": [item.get("message_id") for item in self._response if item.get("message_id") is not None],
        }
        self._context = []
        self._response = []
        self._last_owner_timestamp = None
        if not example["context_messages"] or not example["owner_response_messages"]:
            return None
        return example


def group_turns(messages: list[dict[str, Any]], *, owner_id: int, chat_id: int, contact_id: int, gap_seconds: float) -> list[dict[str, Any]]:
    grouper = TurnGrouper(owner_id=owner_id, chat_id=chat_id, contact_id=contact_id, gap_seconds=gap_seconds)
    examples: list[dict[str, Any]] = []
    for message in sorted(messages, key=lambda item: (float(item.get("timestamp") or 0), int(item.get("id") or 0))):
        completed = grouper.push(message)
        if completed:
            examples.append(completed)
    completed = grouper.finish()
    if completed:
        examples.append(completed)
    return examples


async def rebuild_conversation_examples(
    database: DigitalMeDatabase,
    *,
    owner_id: int,
    gap_seconds: float,
    progress_callback=None,
) -> int:
    await database.clear_examples_and_rag()
    total = 0
    chat_ids = await database.chat_ids()
    for position, chat_id in enumerate(chat_ids, start=1):
        grouper = TurnGrouper(owner_id=owner_id, chat_id=chat_id, contact_id=chat_id, gap_seconds=gap_seconds)
        batch: list[dict[str, Any]] = []
        async for message in database.iter_messages(chat_id=chat_id):
            completed = grouper.push(message)
            if completed:
                batch.append(completed)
            if len(batch) >= 500:
                await database.insert_examples(batch)
                total += len(batch)
                batch.clear()
        completed = grouper.finish()
        if completed:
            batch.append(completed)
        if batch:
            await database.insert_examples(batch)
            total += len(batch)
        if progress_callback:
            await progress_callback({"phase": "building_turns", "current": position, "total": len(chat_ids), "examples": total})
    return total


async def rebuild_reply_patterns(database: DigitalMeDatabase) -> int:
    """Build a compact same-chat index of historical incoming-to-owner turns."""
    patterns: list[dict[str, Any]] = []
    async for example in database.iter_examples():
        context = example.get("context_json")
        responses = example.get("response_json")
        if not isinstance(context, str) or not isinstance(responses, str):
            continue
        incoming_messages = json_loads(context, [])
        owner_responses = json_loads(responses, [])
        if not isinstance(incoming_messages, list) or not isinstance(owner_responses, list):
            continue
        incoming = clean_text(incoming_messages[-1] if incoming_messages else "", limit=500)
        reply_parts = [clean_text(item, limit=280) for item in owner_responses]
        reply_parts = [item for item in reply_parts if item]
        key = reply_key(incoming)
        response_text = "\n".join(reply_parts)
        if not key or not response_text or len(response_text) > 560:
            continue
        patterns.append(
            {
                "chat_id": int(example["chat_id"]),
                "input_key": key,
                "input_text": incoming,
                "responses": reply_parts[:3],
                "response_text": response_text,
                "timestamp": float(example.get("timestamp") or 0.0),
            }
        )
    await database.replace_reply_patterns(patterns)
    return len(patterns)


def _language_label(text: str) -> str:
    has_cyrillic = bool(CYRILLIC_RE.search(text))
    has_latin = bool(LATIN_RE.search(text))
    if has_cyrillic and UKRAINIAN_RE.search(text):
        return "uk" if not has_latin else "mixed"
    if has_cyrillic:
        return "ru" if not has_latin else "mixed"
    return "en" if has_latin else "other"


def _distribution(values: list[int]) -> dict[str, int]:
    if not values:
        return {"short": 0, "medium": 0, "long": 0}
    return {
        "short": sum(value <= 40 for value in values),
        "medium": sum(40 < value <= 180 for value in values),
        "long": sum(value > 180 for value in values),
    }


async def build_personality_profile(database: DigitalMeDatabase, *, owner_id: int) -> dict[str, Any]:
    words: Counter[str] = Counter()
    emojis: Counter[str] = Counter()
    languages: Counter[str] = Counter()
    punctuation: Counter[str] = Counter()
    lengths: list[int] = []
    profane: Counter[str] = Counter()
    uppercase_messages = 0
    total_messages = 0

    async for row in database.iter_messages(sender_id=owner_id):
        text = clean_text(row.get("text"))
        if not text:
            continue
        total_messages += 1
        lengths.append(len(text))
        words.update(tokenize(text))
        emojis.update(EMOJI_RE.findall(text))
        languages[_language_label(text)] += 1
        for mark in ("!", "?", ".", "...", ","):
            punctuation[mark] += text.count(mark)
        if text.isupper() and len(text) > 3:
            uppercase_messages += 1
        lowered = text.lower()
        for stem in PROFANITY_STEMS:
            if stem in lowered:
                profane[stem] += 1

    bubble_counts: Counter[str] = Counter()
    async for example in database.iter_examples():
        bubble_counts[str(example.get("bubble_count") or 1)] += 1
    average_length = round(sum(lengths) / len(lengths), 2) if lengths else 0.0
    profile = {
        "languages": dict(languages),
        "common_words": [word for word, _count in words.most_common(80)],
        "slang": [word for word, count in words.most_common(200) if len(word) <= 18 and count > 2][:40],
        "swear_patterns": dict(profane),
        "emoji_usage": dict(emojis.most_common(40)),
        "punctuation": dict(punctuation),
        "capitalization": {"all_caps_ratio": round(uppercase_messages / total_messages, 4) if total_messages else 0.0},
        "message_length_distribution": {"average": average_length, **_distribution(lengths)},
        "bubble_count_distribution": dict(bubble_counts),
        "response_delay_patterns": {},
        "humor_patterns": {"markers": [word for word in ("лол", "кек", "рофл", "ахах") if words[word] > 0]},
        "common_reactions": {},
        "question_patterns": {"ratio": round(sum(1 for value in lengths if value) / total_messages, 4) if total_messages else 0.0},
        "agreement_patterns": [word for word in ("да", "ага", "ок", "окей") if words[word] > 0],
        "disagreement_patterns": [word for word in ("не", "нет", "та") if words[word] > 0],
        "communication_style": {
            "average_length": average_length,
            "preferred_length": "short" if average_length <= 50 else "medium" if average_length <= 180 else "long",
            "sample_size": total_messages,
        },
    }
    await database.set_personality_profile(owner_id, profile)
    return profile


async def _profile_for_chat(database: DigitalMeDatabase, *, chat_id: int, owner_id: int) -> dict[str, Any]:
    words: Counter[str] = Counter()
    emojis: Counter[str] = Counter()
    lengths: list[int] = []
    owner_count = 0
    total_count = 0
    affection_hits = 0
    teasing_hits = 0
    profanity_hits = 0
    language_mix: Counter[str] = Counter()
    async for row in database.iter_messages(chat_id=chat_id):
        total_count += 1
        if row.get("sender_id") != owner_id:
            continue
        text = clean_text(row.get("text"))
        if not text:
            continue
        owner_count += 1
        lower = text.lower()
        words.update(tokenize(text))
        emojis.update(EMOJI_RE.findall(text))
        lengths.append(len(text))
        language_mix[_language_label(text)] += 1
        affection_hits += sum(term in lower for term in AFFECTION_TERMS)
        teasing_hits += sum(term in lower for term in TEASING_TERMS)
        profanity_hits += sum(term in lower for term in PROFANITY_STEMS)
    dialog = await database.get_dialog(chat_id)
    average_length = round(sum(lengths) / len(lengths), 2) if lengths else 0.0
    denominator = max(1, owner_count)
    affection = round(min(1.0, affection_hits / denominator * 3), 3)
    teasing = round(min(1.0, teasing_hits / denominator * 3), 3)
    profanity = round(min(1.0, profanity_hits / denominator * 3), 3)
    formality = round(max(0.0, 1.0 - (teasing + profanity) / 2), 3)
    relationship_type = "romantic_or_affectionate" if affection > 0.24 else "informal" if formality < 0.72 else "neutral"
    display_name = str(dialog.get("display_name") or "")
    return {
        "contact_id": chat_id,
        "display_names": [display_name] if display_name else [],
        "relationship_type": relationship_type,
        "affection_level": affection,
        "teasing_level": teasing,
        "formality": formality,
        "profanity_level": profanity,
        "emoji_frequency": dict(emojis.most_common(20)),
        "typical_message_length": {"average": average_length, **_distribution(lengths)},
        "typical_bubble_count": {},
        "common_terms": [word for word, _count in words.most_common(35)],
        "pet_names": [word for word in ("зая", "кот", "люб", "сонц") if any(word in candidate for candidate in words)],
        "common_topics": [word for word, _count in words.most_common(15)],
        "response_patterns": [],
        "language_mix": dict(language_mix),
        "summary": f"Local profile: {relationship_type}; {owner_count} owner messages; average {average_length} chars.",
        "sample_size": {"owner_messages": owner_count, "all_messages": total_count},
    }


async def rebuild_relationship_profiles(
    database: DigitalMeDatabase,
    *,
    owner_id: int,
    progress_callback=None,
) -> int:
    chat_ids = await database.chat_ids()
    count = 0
    for position, chat_id in enumerate(chat_ids, start=1):
        profile = await _profile_for_chat(database, chat_id=chat_id, owner_id=owner_id)
        await database.set_relationship_profile(chat_id, chat_id, profile)
        count += 1
        if progress_callback:
            await progress_callback({"phase": "building_relationships", "current": position, "total": len(chat_ids), "relationships": count})
    return count


async def refresh_rolling_summary(
    database: DigitalMeDatabase,
    *,
    chat_id: int,
    owner_id: int,
    recent_limit: int = 240,
) -> dict[str, Any]:
    messages = await database.get_recent_messages(chat_id, recent_limit)
    words: Counter[str] = Counter()
    owner_bubbles = 0
    incoming_bubbles = 0
    last_timestamp = 0.0
    for item in messages:
        text = clean_text(item.get("text"))
        words.update(tokenize(text))
        last_timestamp = max(last_timestamp, float(item.get("timestamp") or 0.0))
        if item.get("sender_id") == owner_id:
            owner_bubbles += 1
        else:
            incoming_bubbles += 1
    summary = {
        "topics": [word for word, _count in words.most_common(20)],
        "people": {},
        "important_events": [],
        "relationship_state": "local_chat_context",
        "emotional_context": "unknown",
        "unanswered_questions": [],
        "plans": [],
        "window": {"messages": len(messages), "owner_bubbles": owner_bubbles, "incoming_bubbles": incoming_bubbles},
        "last_timestamp": last_timestamp,
    }
    await database.set_summary(chat_id, summary, len(messages))
    return summary


async def rebuild_all_summaries(database: DigitalMeDatabase, *, owner_id: int, recent_limit: int = 240) -> None:
    for chat_id in await database.chat_ids():
        await refresh_rolling_summary(database, chat_id=chat_id, owner_id=owner_id, recent_limit=recent_limit)
