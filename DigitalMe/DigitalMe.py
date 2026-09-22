# meta developer: @DeathTerror
# meta name: DigitalMe
# requires: aiohttp aiosqlite ijson

from __future__ import annotations

import asyncio
import contextlib
import html
import random
import time
from pathlib import Path

import aiohttp

from deathtg.command import command
from deathtg.loader import ConfigValue, Module, ModuleConfig, validators, watcher

from .digitalme.database import DigitalMeDatabase
from .digitalme.debounce import PerChatDebounce
from .digitalme.embeddings import EmbeddingService
from .digitalme.importer import ImportCancelled, ImportFormatError, TelegramExportImporter
from .digitalme.llm import GenerationEngine
from .digitalme.memory import MemoryEvaluator
from .digitalme.personality import (
    build_personality_profile,
    rebuild_all_summaries,
    rebuild_conversation_examples,
    rebuild_reply_patterns,
    rebuild_relationship_profiles,
    refresh_rolling_summary,
)
from .digitalme.prompt_builder import PromptBuilder
from .digitalme.providers import ProviderError, ProviderRouter
from .digitalme.queue_manager import GlobalFIFOQueue
from .digitalme.rag import RAGService
from .digitalme.sanitizer import PromptSanitizer
from .digitalme.schemas import InboundBubble, QueueItem
from .digitalme.utils import clean_text, message_is_cancellation, now_ts, safe_display


def _float_validator(minimum: float, maximum: float):
    def validate(value):
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected number") from exc
        if number < minimum or number > maximum:
            raise ValueError(f"expected number between {minimum} and {maximum}")
        return number

    return validate


class DigitalMeMod(Module):
    """Private-chat AI persona backed by local SQLite and one global generation worker."""

    strings = {"name": "DigitalMe"}

    def __init__(self) -> None:
        super().__init__()
        self.config = ModuleConfig(
            ConfigValue("enabled", False, "Global AI autopilot", validators.Boolean()),
            ConfigValue("private_only", True, "Reply only to direct user chats", validators.Boolean()),
            ConfigValue("provider", "ollama", "ollama, openrouter, openai_compatible, lm_studio", validators.Choice(("ollama", "openrouter", "openai_compatible", "lm_studio"))),
            ConfigValue("base_url", "http://127.0.0.1:11434", "Provider base URL", validators.String(max_len=500)),
            ConfigValue("enable_thinking", False, "Use a provider-native thinking mode when it is supported", validators.Boolean()),
            ConfigValue("ollama_json_mode", False, "Force Ollama JSON mode when the selected model supports it", validators.Boolean()),
            ConfigValue("api_key", "", "Remote provider API key", validators.String(max_len=1000), secret=True),
            ConfigValue("model", "runeweaver", "Primary model", validators.String(min_len=1, max_len=200)),
            ConfigValue("owner_name", "Вова", "Name used for direct identity questions", validators.String(min_len=1, max_len=80)),
            ConfigValue("fallback_models", "", "Comma-separated fallback model IDs", validators.String(max_len=1000)),
            ConfigValue("temperature", 0.55, "Sampling temperature", _float_validator(0.0, 2.0)),
            ConfigValue("top_p", 0.9, "Top-p sampling", _float_validator(0.0, 1.0)),
            ConfigValue("max_output_tokens", 160, "Maximum completion tokens", validators.Integer(minimum=64, maximum=8192)),
            ConfigValue("timeout_seconds", 60, "Provider timeout", validators.Integer(minimum=5, maximum=300)),
            ConfigValue("ollama_keep_alive_minutes", 10, "Keep the local Ollama model loaded after a reply", validators.Integer(minimum=0, maximum=120)),
            ConfigValue("context_window_override", 4096, "Model context budget", validators.Integer(minimum=2048, maximum=131072)),
            ConfigValue("max_retries", 2, "Retries per model", validators.Integer(minimum=0, maximum=4)),
            ConfigValue("retry_backoff", 1.0, "Retry backoff seconds", _float_validator(0.2, 15.0)),
            ConfigValue("provider_cooldown", 8, "Cooldown after provider failure", validators.Integer(minimum=0, maximum=120)),
            ConfigValue("debounce_seconds", 2.5, "Per-chat debounce", _float_validator(0.1, 30.0)),
            ConfigValue("training_turn_gap_seconds", 60, "Owner turn grouping gap", validators.Integer(minimum=5, maximum=600)),
            ConfigValue("recent_messages_limit", 16, "Recent context bubbles", validators.Integer(minimum=6, maximum=100)),
            ConfigValue("rag_result_count", 3, "RAG examples", validators.Integer(minimum=0, maximum=20)),
            ConfigValue("retrieval_first", True, "Use matching past turns before full AI generation", validators.Boolean()),
            ConfigValue("retrieval_candidate_limit", 4, "Past reply patterns supplied to fast generation", validators.Integer(minimum=1, maximum=8)),
            ConfigValue("fast_reply_max_tokens", 80, "Fast reply generation token budget", validators.Integer(minimum=64, maximum=256)),
            ConfigValue("max_message_bubbles", 3, "Maximum reply bubbles", validators.Integer(minimum=1, maximum=12)),
            ConfigValue("max_message_length", 280, "Maximum bubble length", validators.Integer(minimum=32, maximum=4096)),
            ConfigValue("max_style_length_multiplier", 3.0, "Maximum length relative to observed owner style", _float_validator(1.0, 8.0)),
            ConfigValue("strict_style_mode", True, "Keep replies short and block roleplay actions", validators.Boolean()),
            ConfigValue("min_delay_ms", 250, "Minimum natural delay", validators.Integer(minimum=0, maximum=60000)),
            ConfigValue("max_delay_ms", 5000, "Maximum natural delay", validators.Integer(minimum=0, maximum=60000)),
            ConfigValue("max_parallel_generations", 1, "Fixed global generation parallelism", validators.Integer(minimum=1, maximum=1)),
            ConfigValue("sanitize_cloud_prompts", True, "Mask common secrets before cloud requests", validators.Boolean()),
            ConfigValue("debug_log_prompts", False, "Never log prompt contents by default", validators.Boolean()),
            ConfigValue("debug_mode", False, "Show owner-only generation diagnostics", validators.Boolean()),
            ConfigValue("cancel_stale_tasks", True, "Cancel pending 'never mind' tasks", validators.Boolean()),
            ConfigValue("cross_contact_style_examples", True, "Use anonymized examples from other chats", validators.Boolean()),
            ConfigValue("observe_manual_messages", True, "Learn from new manual owner messages locally", validators.Boolean()),
            ConfigValue("observation_rebuild_seconds", 300, "Delay before rebuilding local training data", validators.Integer(minimum=30, maximum=3600)),
            ConfigValue("twin_chat_id", 0, "Private twin chat ID allowed to receive model-initiated messages", validators.Integer(minimum=0)),
            ConfigValue("twin_active_mode", False, "Let DigitalMe independently continue the configured twin chat", validators.Boolean()),
            ConfigValue("twin_idle_seconds", 180, "Quiet time before the twin may initiate a message", validators.Integer(minimum=30, maximum=86400)),
            ConfigValue("twin_pulse_min_seconds", 90, "Shortest internal twin initiative check", validators.Integer(minimum=30, maximum=3600)),
            ConfigValue("twin_pulse_max_seconds", 240, "Longest internal twin initiative check", validators.Integer(minimum=30, maximum=7200)),
            ConfigValue("embedding_mode", "off", "off, local, remote", validators.Choice(("off", "local", "remote"))),
            ConfigValue("embedding_model", "", "Optional embedding model", validators.String(max_len=300)),
            ConfigValue("import_max_mb", 2048, "Largest accepted Telegram export", validators.Integer(minimum=10, maximum=16384)),
        )
        self._module_root = Path(__file__).resolve().parent
        self._database: DigitalMeDatabase | None = None
        self._session: aiohttp.ClientSession | None = None
        self._provider: ProviderRouter | None = None
        self._engine: GenerationEngine | None = None
        self._queue: GlobalFIFOQueue | None = None
        self._debounce: PerChatDebounce | None = None
        self._importer: TelegramExportImporter | None = None
        self._import_task: asyncio.Task | None = None
        self._owner_id = 0
        self._started = False
        self._shutting_down = False
        self._last_owner_notice = 0.0
        self._last_completion = None
        self._generated_signatures: dict[tuple[int, str], float] = {}
        self._observation_task: asyncio.Task | None = None
        self._twin_task: asyncio.Task | None = None

    async def client_ready(self, client, db=None) -> None:
        if self._started:
            return
        self.client = client
        self._database = DigitalMeDatabase(self._module_root / "data" / "digitalme.sqlite3")
        await self._database.connect()
        me = await client.get_me()
        self._owner_id = int(getattr(me, "id", 0) or 0)
        if not self._owner_id:
            raise RuntimeError("DigitalMe could not identify the current Telegram owner")
        await self._database.set_owner_id(self._owner_id)
        self._session = aiohttp.ClientSession(raise_for_status=False)
        sanitizer = PromptSanitizer()
        self._provider = ProviderRouter(self._session, self._config_value)
        embeddings = EmbeddingService(self._provider, self._config_value)
        rag = RAGService(self._database, embeddings, sanitizer, self._config_value)
        self._engine = GenerationEngine(
            self._database,
            self._provider,
            rag,
            PromptBuilder(sanitizer, self._config_value),
            MemoryEvaluator(),
            self._config_value,
            self._owner_id,
        )
        self._queue = GlobalFIFOQueue(self._process_queue_item)
        await self._queue.start()
        self._debounce = PerChatDebounce(lambda: self._float("debounce_seconds", 2.5, 0.1, 30.0), self._enqueue_debounced)
        self._importer = TelegramExportImporter(self._database)
        self._started = True
        self._start_twin_loop()

    async def on_unload(self) -> None:
        self._shutting_down = True
        if self._twin_task:
            self._twin_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._twin_task
        if self._importer:
            self._importer.cancel()
        if self._import_task and not self._import_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._import_task), timeout=4)
            except (asyncio.TimeoutError, ImportCancelled):
                self._import_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._import_task
        if self._observation_task and not self._observation_task.done():
            self._observation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._observation_task
        if self._debounce:
            await self._debounce.close()
        if self._queue:
            await self._queue.close()
        if self._session and not self._session.closed:
            await self._session.close()
        if self._database:
            await self._database.close()
        self._started = False

    def _config_value(self, key: str, default=None):
        return self.config.get(key, default)

    def _float(self, key: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _set_config(self, key: str, value) -> None:
        self.config[key] = value
        self.save_config()

    def _ready(self) -> bool:
        return bool(self._started and self._database and self._engine and self._queue and self._debounce)

    async def _edit(self, event, text: str) -> None:
        with contextlib.suppress(Exception):
            await event.edit(text, parse_mode="html", link_preview=False)

    @command("aistart", description="Start DigitalMe autopilot", usage=".aistart", security="owner")
    async def aistart_cmd(self, event, args) -> None:
        if not self._ready():
            await self._edit(event, "<b>DigitalMe is still starting.</b>")
            return
        self._set_config("enabled", True)
        await self._edit(
            event,
            "<b>DigitalMe started.</b>\n"
            f"Provider: <code>{html.escape(str(self.config.get('provider')))}</code>\n"
            f"Model: <code>{html.escape(str(self.config.get('model')))}</code>\n"
            "Only direct user chats are eligible.",
        )

    @command("aistop", description="Stop DigitalMe autopilot", usage=".aistop", security="owner")
    async def aistop_cmd(self, event, args) -> None:
        self._set_config("enabled", False)
        removed = await self._queue.clear_pending() if self._queue else []
        if self._database:
            for item in removed:
                await self._database.queue_status(item.generation_id, "cancelled")
        await self._edit(event, f"<b>DigitalMe stopped.</b>\nCancelled pending tasks: <code>{len(removed)}</code>")

    @command("twinlab", description="Control model-initiated conversation with the configured twin chat", usage=".twinlab [on|off|status]", security="owner")
    async def twinlab_cmd(self, event, args) -> None:
        action = str(args[0]).lower() if args else "status"
        if action not in {"on", "off", "status"}:
            await self._edit(event, "<b>Usage:</b> <code>.twinlab [on|off|status]</code>")
            return
        chat_id = self._twin_chat_id()
        if action == "on":
            if not chat_id:
                await self._edit(event, "<b>Set a twin chat ID in DigitalMe settings first.</b>")
                return
            self._set_config("twin_active_mode", True)
            self._start_twin_loop()
        elif action == "off":
            self._set_config("twin_active_mode", False)
        if self._database and chat_id:
            state = await self._database.get_setting(self._twin_state_key(chat_id), {})
        else:
            state = {}
        await self._edit(
            event,
            "<b>DigitalMe TwinLab</b>\n"
            f"Chat: <code>{chat_id or 'not configured'}</code>\n"
            f"Mode: <code>{'ON' if self.config.get('twin_active_mode') else 'OFF'}</code>\n"
            f"Last model initiative: <code>{int(state.get('last_sent_at') or 0) or 'never'}</code>",
        )

    @command("aitoken", description="Save remote provider key or select local Ollama", usage=".aitoken <key> | .aitoken local [model]", security="owner")
    async def aitoken_cmd(self, event, args) -> None:
        if not args:
            await self._edit(event, "<b>Usage:</b> <code>.aitoken &lt;key&gt;</code> or <code>.aitoken local runeweaver</code>")
            return
        if str(args[0]).lower() in {"local", "ollama"}:
            model = " ".join(args[1:]).strip() or "runeweaver"
            self._set_config("provider", "ollama")
            self._set_config("base_url", "http://127.0.0.1:11434")
            self._set_config("model", model)
            await self._edit(event, f"<b>Local Ollama selected.</b>\nModel: <code>{html.escape(model)}</code>\nNo API key is required.")
            return
        self._set_config("api_key", " ".join(args).strip())
        await self._edit(event, "<b>Remote provider key saved.</b>\nThe key is masked in the panel and is never echoed to Telegram.")

    @command("aitakeinfo", description="Import a replied Telegram result.json", usage=".aitakeinfo [status|cancel]", security="owner")
    async def aitakeinfo_cmd(self, event, args) -> None:
        if not self._ready() or not self._database or not self._importer:
            await self._edit(event, "<b>DigitalMe is still starting.</b>")
            return
        action = str(args[0]).lower() if args else "start"
        if action == "status":
            await self._edit(event, self._import_status_text(await self._database.get_import_status()))
            return
        if action == "cancel":
            if self._import_task and not self._import_task.done():
                self._importer.cancel()
                await self._edit(event, "<b>DigitalMe import cancellation requested.</b>")
            else:
                await self._edit(event, "<b>No active import.</b>")
            return
        if self._import_task and not self._import_task.done():
            await self._edit(event, "<b>An import is already running.</b> Use <code>.aitakeinfo status</code>.")
            return
        reply = await event.get_reply_message()
        if not reply:
            await self._edit(event, "<b>Reply to Telegram Desktop's <code>result.json</code> file, then send <code>.aitakeinfo</code>.</b>")
            return
        document = getattr(reply, "document", None)
        attributes = list(getattr(document, "attributes", []) or [])
        attribute_name = next((getattr(item, "file_name", "") for item in attributes if getattr(item, "file_name", "")), "")
        name = str(attribute_name or getattr(getattr(reply, "file", None), "name", "") or "")
        mime = str(getattr(getattr(reply, "file", None), "mime_type", "") or "").lower()
        size = int(getattr(getattr(reply, "file", None), "size", 0) or 0)
        if not name.lower().endswith(".json"):
            await self._edit(event, "<b>Only a JSON Telegram export is accepted.</b>")
            return
        if mime and mime not in {"application/json", "text/json", "application/octet-stream"}:
            await self._edit(event, "<b>That document does not look like JSON.</b>")
            return
        if size and size > int(self.config.get("import_max_mb", 2048)) * 1024 * 1024:
            await self._edit(event, "<b>The export exceeds DigitalMe's configured size limit.</b>")
            return
        imports_dir = self._module_root / "data" / "imports"
        imports_dir.mkdir(parents=True, exist_ok=True)
        target = imports_dir / f"{int(time.time())}_result.json"
        await self._edit(event, "<b>DigitalMe:</b> downloading Telegram export...")
        downloaded = await self.client.download_media(reply, file=str(target))
        if not downloaded:
            await self._edit(event, "<b>DigitalMe could not download that document.</b>")
            return
        self._import_task = asyncio.create_task(self._run_import(event, Path(downloaded)))

    @command("aistatus", description="Show DigitalMe status and queue", usage=".aistatus", security="owner")
    async def aistatus_cmd(self, event, args) -> None:
        if not self._ready():
            await self._edit(event, "<b>DigitalMe is still starting.</b>")
            return
        await self.inline_send(
            event,
            await self._status_text(),
            reply_markup=self.inline_buttons(
                [{"text": "Refresh", "callback": self.status_callback, "args": ()}],
                [{"text": "Start", "callback": self.start_callback, "args": ()}, {"text": "Stop", "callback": self.stop_callback, "args": ()}],
                [{"text": "Queue", "callback": self.queue_callback, "args": ()}],
                [{"text": "Close", "callback": self.close_callback, "args": ()}],
            ),
            parse_mode="html",
            link_preview=False,
            ttl=3600,
        )

    @command("aitest", description="Test the configured AI provider without messaging anyone", usage=".aitest", security="owner")
    async def aitest_cmd(self, event, args) -> None:
        if not self._provider or not self._database or not self._queue:
            await self._edit(event, "<b>DigitalMe is still starting.</b>")
            return
        stats = await self._database.statistics(self._owner_id)
        running, waiting = await self._queue.snapshot()
        data_state = (
            f"Database: <code>{stats['messages']} messages / {stats['examples']} examples</code>\n"
            f"RAG: <code>{stats['rag_documents']} docs</code> | Relationships: <code>{stats['relationships']}</code>\n"
            f"Queue: <code>{len(waiting)} waiting{' + running' if running else ''}</code>\n"
        )
        try:
            completion = await self._provider.complete(
                [
                    {"role": "system", "content": "Return a compact JSON object only."},
                    {"role": "user", "content": 'Return {"ok":true}.'},
                ],
                thinking_override=False,
            )
            self._last_completion = completion
            ttft = f"\nTTFT: <code>{completion.ttft_ms / 1000:.2f} sec</code>" if completion.ttft_ms is not None else ""
            warning = "\nWarning: <code>thinking parameter unavailable; normal request used</code>" if completion.warning else ""
            await self._edit(
                event,
                "<b>DigitalMe provider test</b>\n"
                f"Provider: <code>{html.escape(completion.provider)}</code>\n"
                f"Model: <code>{html.escape(completion.model)}</code>\n"
                f"Thinking: <code>{'ON' if completion.thinking_requested else 'OFF'}</code>\n"
                f"Latency: <code>{completion.latency_ms / 1000:.2f} sec</code>{ttft}\n"
                + data_state
                + f"Status: <code>OK</code>{warning}",
            )
        except ProviderError as exc:
            await self._edit(
                event,
                "<b>DigitalMe provider test</b>\n"
                f"Provider: <code>{html.escape(str(self.config.get('provider')))}</code>\n"
                f"Model: <code>{html.escape(str(self.config.get('model')))}</code>\n"
                "Thinking: <code>OFF</code>\n"
                + data_state
                + f"Status: <code>{html.escape(exc.kind)}</code>",
            )

    @command("clone", description="Preview a DigitalMe reply without sending it", usage=".clone [--chat id] text", security="owner")
    async def clone_cmd(self, event, args) -> None:
        if not self._ready() or not self._engine:
            await self._edit(event, "<b>DigitalMe is still starting.</b>")
            return
        parts = list(args)
        chat_id = self._target_chat_id(event, []) or self._owner_id
        if len(parts) >= 2 and parts[0] == "--chat":
            try:
                chat_id = int(parts[1])
            except (TypeError, ValueError):
                await self._edit(event, "<b>Usage:</b> <code>.clone --chat &lt;id&gt; text</code>")
                return
            parts = parts[2:]
        text = clean_text(" ".join(parts), limit=4000)
        if not text:
            await self._edit(event, "<b>Usage:</b> <code>.clone [--chat id] text</code>")
            return
        await self._preview_reply(event, chat_id, text)

    @command("clonechat", description="Preview a reply with another chat's style", usage=".clonechat <chat_id> text", security="owner")
    async def clonechat_cmd(self, event, args) -> None:
        if len(args) < 2:
            await self._edit(event, "<b>Usage:</b> <code>.clonechat &lt;chat_id&gt; text</code>")
            return
        try:
            chat_id = int(args[0])
        except (TypeError, ValueError):
            await self._edit(event, "<b>Chat ID must be numeric.</b>")
            return
        text = clean_text(" ".join(args[1:]), limit=4000)
        if not text:
            await self._edit(event, "<b>Usage:</b> <code>.clonechat &lt;chat_id&gt; text</code>")
            return
        await self._preview_reply(event, chat_id, text)

    async def _preview_reply(self, event, chat_id: int, text: str) -> None:
        if not self._ready() or not self._engine:
            await self._edit(event, "<b>DigitalMe is still starting.</b>")
            return
        item = QueueItem(
            chat_id=chat_id,
            sender_id=chat_id,
            messages=[InboundBubble(message_id=None, text=text, timestamp=now_ts())],
            generation_id="preview",
        )
        try:
            result = await self._engine.generate(item, thinking_override=False)
        except ProviderError as exc:
            await self._edit(event, f"<b>DigitalMe preview unavailable:</b> <code>{html.escape(exc.kind)}</code>")
            return
        if not result:
            await self._edit(event, "<b>DigitalMe preview returned no valid response.</b>")
            return
        self._last_completion = self._engine.last_completion
        bubbles = "\n".join(html.escape(bubble.text) for bubble in result.messages)
        await self._edit(event, f"<b>DigitalMe preview</b>\n<blockquote>{bubbles}</blockquote>")

    @command("aistyle", description="Show the imported style profile for a chat", usage=".aistyle [chat_id]", security="owner")
    async def aistyle_cmd(self, event, args) -> None:
        if not self._database:
            await self._edit(event, "<b>DigitalMe is still starting.</b>")
            return
        chat_id = self._target_chat_id(event, args)
        if chat_id is None:
            await self._edit(event, "<b>Use this in a private chat or pass its numeric chat ID.</b>")
            return
        dialog = await self._database.get_dialog(chat_id)
        relationship = await self._database.get_relationship_profile(chat_id)
        examples = await self._database.count_examples(chat_id=chat_id)
        documents = await self._database.count_rag_documents(chat_id=chat_id)
        average = relationship.get("typical_message_length", {}).get("average", 0)
        await self._edit(
            event,
            "<b>DigitalMe style profile</b>\n"
            f"Chat: <code>{chat_id}</code> {html.escape(str(dialog.get('display_name') or ''))}\n"
            f"Examples: <code>{examples}</code> | RAG: <code>{documents}</code>\n"
            f"Tone: <code>{html.escape(str(relationship.get('relationship_type') or 'unknown'))}</code>\n"
            f"Average owner message: <code>{average}</code> chars\n"
            f"Terms: <code>{html.escape(', '.join(relationship.get('common_terms', [])[:12]) or 'none')}</code>",
        )

    @command("aidebug", description="Show or toggle owner-only diagnostics", usage=".aidebug [on|off]", security="owner")
    async def aidebug_cmd(self, event, args) -> None:
        if args:
            value = str(args[0]).lower()
            if value not in {"on", "off"}:
                await self._edit(event, "<b>Usage:</b> <code>.aidebug [on|off]</code>")
                return
            self._set_config("debug_mode", value == "on")
        completion = self._last_completion
        details = "No generation has completed since this module started."
        if completion:
            ttft = f", TTFT {completion.ttft_ms / 1000:.2f}s" if completion.ttft_ms is not None else ""
            details = f"{html.escape(completion.provider)} / {html.escape(completion.model)}: {completion.latency_ms / 1000:.2f}s{ttft}"
            if completion.warning:
                details += "; thinking fallback used"
        await self._edit(
            event,
            "<b>DigitalMe diagnostics</b>\n"
            f"Mode: <code>{'ON' if self.config.get('debug_mode') else 'OFF'}</code>\n"
            f"Last completion: <code>{details}</code>\n"
            "Prompt and message contents are never shown here.",
        )

    @command("aiallow", description="Allow only this private chat when an allowlist is used", usage=".aiallow [chat_id]", security="owner")
    async def aiallow_cmd(self, event, args) -> None:
        chat_id = self._target_chat_id(event, args)
        if chat_id is None or not self._database:
            await self._edit(event, "<b>Use this in a private chat or pass its numeric chat ID.</b>")
            return
        await self._database.set_chat_control(chat_id, allowed=True, denied=False, enabled=True)
        await self._edit(event, f"<b>DigitalMe allowed chat:</b> <code>{chat_id}</code>")

    @command("aideny", description="Deny a private chat", usage=".aideny [chat_id]", security="owner")
    async def aideny_cmd(self, event, args) -> None:
        chat_id = self._target_chat_id(event, args)
        if chat_id is None or not self._database:
            await self._edit(event, "<b>Use this in a private chat or pass its numeric chat ID.</b>")
            return
        await self._database.set_chat_control(chat_id, allowed=False, denied=True, enabled=False)
        await self._edit(event, f"<b>DigitalMe denied chat:</b> <code>{chat_id}</code>")

    @command("aiqueue", description="Show global DigitalMe FIFO queue", usage=".aiqueue", security="owner")
    async def aiqueue_cmd(self, event, args) -> None:
        if not self._queue:
            await self._edit(event, "<b>DigitalMe is still starting.</b>")
            return
        await self._edit(event, await self._queue_text())

    @watcher("in", no_commands=True)
    async def incoming_watcher(self, event) -> None:
        try:
            if not self._ready() or self._shutting_down or not self._database:
                return
            if not getattr(event, "is_private", False) or getattr(event, "is_group", False) or getattr(event, "is_channel", False):
                return
            if getattr(event, "out", False):
                return
            if getattr(getattr(event, "message", None), "action", None):
                return
            chat_id = int(getattr(event, "chat_id", 0) or 0)
            if not chat_id or chat_id == self._owner_id:
                return
            sender = await event.get_sender()
            sender_id = int(getattr(sender, "id", 0) or getattr(event, "sender_id", 0) or 0)
            if not sender_id or sender_id == self._owner_id or bool(getattr(sender, "bot", False)):
                return
            text = clean_text(getattr(event, "raw_text", ""))
            if not text:
                return
            reply_to = getattr(getattr(event, "message", None), "reply_to_msg_id", None)
            reply_text = ""
            if reply_to:
                with contextlib.suppress(Exception):
                    replied = await event.get_reply_message()
                    reply_text = clean_text(getattr(replied, "raw_text", ""), limit=1000)
            display_name = " ".join(
                part for part in (getattr(sender, "first_name", ""), getattr(sender, "last_name", "")) if part
            ).strip()
            bubble = InboundBubble(
                message_id=int(getattr(event, "id", 0) or 0) or None,
                text=text,
                timestamp=now_ts(),
                reply_to_message_id=int(reply_to) if reply_to else None,
                reply_text=reply_text,
            )
            await self._database.insert_live_message(
                chat_id=chat_id,
                sender_id=sender_id,
                message_id=bubble.message_id,
                timestamp=bubble.timestamp,
                text=text,
                reply_to_message_id=bubble.reply_to_message_id,
                display_name=display_name,
            )
            if not bool(self.config.get("enabled", False)) or not await self._chat_is_eligible(chat_id):
                return
            await self._debounce.add(chat_id, sender_id, bubble)
        except Exception:
            # Watchers must never surface an exception to a private correspondent.
            return

    @watcher("out", no_commands=True)
    async def outgoing_observer(self, event) -> None:
        """Persist only manual owner messages; generated replies must never train the persona."""
        try:
            if not self._ready() or self._shutting_down or not self._database:
                return
            if not bool(self.config.get("observe_manual_messages", True)):
                return
            if not getattr(event, "is_private", False) or getattr(event, "is_group", False) or getattr(event, "is_channel", False):
                return
            chat_id = int(getattr(event, "chat_id", 0) or 0)
            if not chat_id or chat_id == self._owner_id:
                return
            text = clean_text(getattr(event, "raw_text", ""))
            if not text or self._is_recent_generated(chat_id, text):
                return
            message_id = int(getattr(event, "id", 0) or 0) or None
            reply_to = getattr(getattr(event, "message", None), "reply_to_msg_id", None)
            dialog = await self._database.get_dialog(chat_id)
            await self._database.insert_live_message(
                chat_id=chat_id,
                sender_id=self._owner_id,
                message_id=message_id,
                timestamp=now_ts(),
                text=text,
                reply_to_message_id=int(reply_to) if reply_to else None,
                display_name=str(dialog.get("display_name") or ""),
                message_type="manual_owner",
            )
            self._schedule_observation_rebuild()
        except Exception:
            return

    async def _enqueue_debounced(self, chat_id: int, sender_id: int, bubbles: list[InboundBubble]) -> None:
        if not self._queue or not self._database or not bool(self.config.get("enabled", False)):
            return
        if not await self._chat_is_eligible(chat_id):
            return
        item, merged = await self._queue.enqueue(chat_id, sender_id, bubbles)
        if not merged:
            await self._database.queue_created(
                generation_id=item.generation_id,
                chat_id=item.chat_id,
                sender_id=item.sender_id,
                buffered_count=item.buffered_count,
                created_at=item.created_at,
            )

    async def _chat_is_eligible(self, chat_id: int) -> bool:
        if not self._database:
            return False
        control = await self._database.get_chat_control(chat_id)
        if int(control.get("denied", 0)) or not int(control.get("enabled", 1)):
            return False
        allowed = await self._database.allowed_chat_ids()
        return not allowed or chat_id in allowed

    async def _process_queue_item(self, item: QueueItem) -> None:
        if not self._database or not self._engine:
            return
        if not bool(self.config.get("enabled", False)) or not await self._chat_is_eligible(item.chat_id):
            await self._database.queue_status(item.generation_id, "cancelled")
            return
        if bool(self.config.get("cancel_stale_tasks", True)) and item.messages and message_is_cancellation(item.messages[-1].text):
            await self._database.queue_status(item.generation_id, "cancelled")
            return
        await self._database.queue_status(item.generation_id, "running")
        try:
            result = await self._generate_with_typing(item)
        except ProviderError as exc:
            await self._database.queue_status(item.generation_id, "failed", error_kind=exc.kind)
            await self._notify_owner(f"generation unavailable ({exc.kind})")
            return
        except Exception as exc:
            await self._database.queue_status(item.generation_id, "failed", error_kind=type(exc).__name__)
            await self._notify_owner("generation failed")
            return
        self._last_completion = self._engine.last_completion
        if self._last_completion and self._last_completion.warning:
            await self._notify_owner("Ollama thinking parameter is unavailable; a normal request was used")
        if not result or not result.messages:
            await self._database.queue_status(item.generation_id, "failed", error_kind="invalid_response")
            await self._notify_owner("model returned no valid reply")
            return
        if not bool(self.config.get("enabled", False)) or not await self._chat_is_eligible(item.chat_id):
            await self._database.queue_status(item.generation_id, "cancelled")
            return
        dialog = await self._database.get_dialog(item.chat_id)
        for index, bubble in enumerate(result.messages):
            if index:
                await asyncio.sleep(max(0, bubble.delay_ms) / 1000)
            # A normal conversation continues in the chat; automatic reply cards reveal the automation.
            reply_to = None
            try:
                self._remember_generated(item.chat_id, bubble.text)
                sent = await self.client.send_message(item.chat_id, bubble.text, reply_to=reply_to)
            except Exception as exc:
                await self._database.queue_status(item.generation_id, "failed", error_kind=type(exc).__name__)
                await self._notify_owner("Telegram delivery failed")
                return
            await self._database.insert_live_message(
                chat_id=item.chat_id,
                sender_id=self._owner_id,
                message_id=int(getattr(sent, "id", 0) or 0) or None,
                timestamp=now_ts(),
                text=bubble.text,
                reply_to_message_id=reply_to,
                display_name=str(dialog.get("display_name") or ""),
                message_type="digitalme_generated",
            )
        await refresh_rolling_summary(
            self._database,
            chat_id=item.chat_id,
            owner_id=self._owner_id,
            recent_limit=int(self.config.get("recent_messages_limit", 40)) * 4,
        )
        await self._database.queue_status(item.generation_id, "done")

    def _remember_generated(self, chat_id: int, text: str) -> None:
        now = time.monotonic()
        self._generated_signatures = {
            key: timestamp for key, timestamp in self._generated_signatures.items() if now - timestamp < 90
        }
        self._generated_signatures[(chat_id, text)] = now

    def _is_recent_generated(self, chat_id: int, text: str) -> bool:
        timestamp = self._generated_signatures.get((chat_id, text))
        return timestamp is not None and time.monotonic() - timestamp < 90

    def _schedule_observation_rebuild(self) -> None:
        if self._observation_task and not self._observation_task.done():
            return
        self._observation_task = asyncio.create_task(self._rebuild_observed_style())

    async def _rebuild_observed_style(self) -> None:
        try:
            await asyncio.sleep(int(self.config.get("observation_rebuild_seconds", 300)))
            if not self._shutting_down and self._database and self._engine:
                await self._rebuild_analysis()
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._notify_owner("local style observer could not rebuild training data")
        finally:
            self._observation_task = None

    def _twin_chat_id(self) -> int:
        try:
            return max(0, int(self.config.get("twin_chat_id", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _twin_state_key(chat_id: int) -> str:
        return f"twin_activity:{int(chat_id)}"

    def _start_twin_loop(self) -> None:
        if self._twin_task and not self._twin_task.done():
            return
        self._twin_task = asyncio.create_task(self._twin_presence_loop())

    async def _twin_presence_loop(self) -> None:
        """Use random model-check pulses, never a fixed timer that blindly sends a message."""
        try:
            while not self._shutting_down:
                minimum = int(self.config.get("twin_pulse_min_seconds", 90))
                maximum = int(self.config.get("twin_pulse_max_seconds", 240))
                minimum = max(30, min(3600, minimum))
                maximum = max(minimum, min(7200, maximum))
                await asyncio.sleep(random.uniform(minimum, maximum))
                if not bool(self.config.get("twin_active_mode", False)):
                    continue
                with contextlib.suppress(ProviderError, asyncio.TimeoutError, ValueError):
                    await self._run_twin_initiative()
        except asyncio.CancelledError:
            raise
        finally:
            self._twin_task = None

    async def _run_twin_initiative(self) -> None:
        if not self._database or not self._engine or not bool(self.config.get("enabled", False)):
            return
        chat_id = self._twin_chat_id()
        if not chat_id or not await self._chat_is_eligible(chat_id):
            return
        running, waiting = await self._queue.snapshot() if self._queue else (None, [])
        if (running and running.chat_id == chat_id) or any(item.chat_id == chat_id for item in waiting):
            return
        last_real = await self._database.last_real_message(chat_id)
        if not last_real:
            return
        state = await self._database.get_setting(self._twin_state_key(chat_id), {})
        state = state if isinstance(state, dict) else {}
        baseline = max(float(last_real.get("timestamp") or 0), float(state.get("last_sent_at") or 0))
        idle_seconds = int(max(0, now_ts() - baseline))
        if idle_seconds < int(self.config.get("twin_idle_seconds", 180)):
            return
        result = await self._engine.generate_twin_initiative(chat_id=chat_id, idle_seconds=idle_seconds)
        self._last_completion = self._engine.last_completion
        state["last_checked_at"] = now_ts()
        state["last_idle_seconds"] = idle_seconds
        if not result or not result.messages:
            await self._database.set_setting(self._twin_state_key(chat_id), state)
            return
        dialog = await self._database.get_dialog(chat_id)
        for index, bubble in enumerate(result.messages):
            if index:
                await asyncio.sleep(max(0, bubble.delay_ms) / 1000)
            self._remember_generated(chat_id, bubble.text)
            sent = await self.client.send_message(chat_id, bubble.text)
            await self._database.insert_live_message(
                chat_id=chat_id,
                sender_id=self._owner_id,
                message_id=int(getattr(sent, "id", 0) or 0) or None,
                timestamp=now_ts(),
                text=bubble.text,
                reply_to_message_id=None,
                display_name=str(dialog.get("display_name") or ""),
                message_type="digitalme_generated",
            )
        state["last_sent_at"] = now_ts()
        await self._database.set_setting(self._twin_state_key(chat_id), state)

    async def _generate_with_typing(self, item: QueueItem):
        action = getattr(self.client, "action", None)
        if action is not None:
            try:
                context = action(item.chat_id, "typing")
            except (AttributeError, TypeError):
                context = None
            if context is not None:
                async with context:
                    return await self._engine.generate(item)
        return await self._engine.generate(item)

    async def _notify_owner(self, message: str) -> None:
        if time.monotonic() - self._last_owner_notice < 45:
            return
        self._last_owner_notice = time.monotonic()
        with contextlib.suppress(Exception):
            await self.client.send_message("me", f"DigitalMe: {message}.")

    async def _run_import(self, event, path: Path) -> None:
        if not self._importer or not self._database:
            return
        try:
            async def progress(payload):
                await self._database.set_import_status(payload)
                await self._edit(event, self._import_status_text(payload))

            stats = await self._importer.import_file(path, owner_id=self._owner_id, progress_callback=progress)
            await self._edit(event, "<b>DigitalMe:</b> building dialogue turns, profiles, and RAG...")
            await self._rebuild_analysis(event)
            await self._database.set_import_status({**stats.payload(), "phase": "ready"})
            await self._edit(event, "<b>DigitalMe import complete.</b>\nUse <code>.aistart</code> when you are ready.")
        except ImportCancelled:
            await self._edit(event, "<b>DigitalMe import cancelled.</b>")
        except ImportFormatError:
            await self._database.set_import_status({"phase": "failed", "error": "incomplete_export"})
            await self._edit(
                event,
                "<b>DigitalMe import stopped.</b>\n"
                "<code>result.json</code> is incomplete. Create a new Telegram Desktop export, wait for it to finish, "
                "then reply to the new file with <code>.aitakeinfo</code>.",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._database.set_import_status({"phase": "failed", "error": type(exc).__name__})
            await self._edit(event, "<b>DigitalMe import failed.</b> Check the file and try again.")
        finally:
            self._import_task = None

    async def _rebuild_analysis(self, event=None) -> None:
        if not self._database or not self._engine:
            return

        async def progress(payload):
            await self._database.set_import_status(payload)
            if event is not None:
                await self._edit(event, self._import_status_text(payload))

        await rebuild_conversation_examples(
            self._database,
            owner_id=self._owner_id,
            gap_seconds=float(self.config.get("training_turn_gap_seconds", 60)),
            progress_callback=progress,
        )
        await progress({"phase": "building_reply_patterns"})
        await rebuild_reply_patterns(self._database)
        await progress({"phase": "building_personality"})
        await build_personality_profile(self._database, owner_id=self._owner_id)
        await rebuild_relationship_profiles(self._database, owner_id=self._owner_id, progress_callback=progress)
        await rebuild_all_summaries(
            self._database,
            owner_id=self._owner_id,
            recent_limit=int(self.config.get("recent_messages_limit", 40)) * 6,
        )
        rag = self._engine.rag
        await rag.rebuild_documents(progress_callback=progress)
        if str(self.config.get("embedding_mode", "off")) != "off" and str(self.config.get("embedding_model", "")).strip():
            await rag.rebuild_embeddings(progress_callback=progress)

    async def _status_text(self) -> str:
        if not self._database or not self._queue:
            return "<b>DigitalMe is unavailable.</b>"
        stats = await self._database.statistics(self._owner_id)
        running, waiting = await self._queue.snapshot()
        return (
            "<b>DigitalMe</b>\n"
            f"Autopilot: <code>{'ON' if self.config.get('enabled') else 'OFF'}</code>\n"
            f"Provider: <code>{html.escape(str(self.config.get('provider')))}</code>\n"
            f"Model: <code>{html.escape(str(self.config.get('model')))}</code>\n"
            f"Thinking: <code>{'ON' if self.config.get('enable_thinking') else 'OFF'}</code>\n"
            f"Messages: <code>{stats['messages']}</code> | Owner: <code>{stats['owner_messages']}</code>\n"
            f"Examples: <code>{stats['examples']}</code> | Relationships: <code>{stats['relationships']}</code>\n"
            f"RAG: <code>{stats['rag_documents']}</code> | Memories: <code>{stats['memories']}</code>\n"
            f"Queue: <code>{len(waiting)}</code> waiting | Running: <code>{running.chat_id if running else 'none'}</code>"
        )

    async def _queue_text(self) -> str:
        if not self._queue:
            return "<b>DigitalMe queue is unavailable.</b>"
        running, waiting = await self._queue.snapshot()
        lines = ["<b>DigitalMe global FIFO queue</b>", ""]
        if running:
            lines.append(f"Running: <code>{running.chat_id}</code> ({running.buffered_count} bubbles)")
        else:
            lines.append("Running: <code>none</code>")
        lines.append("Waiting:")
        for index, item in enumerate(waiting, start=1):
            age = max(0, int(now_ts() - item.created_at))
            lines.append(f"{index}. <code>{item.chat_id}</code> | {item.buffered_count} bubbles | {age}s")
        if not waiting:
            lines.append("<i>empty</i>")
        return "\n".join(lines)

    def _import_status_text(self, status: dict) -> str:
        phase = html.escape(str(status.get("phase") or "idle"))
        progress = status.get("progress")
        return (
            "<b>DigitalMe import</b>\n"
            f"Phase: <code>{phase}</code>\n"
            f"Progress: <code>{progress if progress is not None else 0}%</code>\n"
            f"Messages: <code>{status.get('imported_messages', status.get('messages', 0))}</code>\n"
            f"Owner messages: <code>{status.get('owner_messages', 0)}</code>\n"
            f"Dialogs: <code>{status.get('dialogs', 0)}</code>\n"
            f"Examples: <code>{status.get('examples', 0)}</code>\n"
            f"Relationships: <code>{status.get('relationships', 0)}</code>\n"
            f"RAG: <code>{status.get('documents', 0)}</code>"
        )

    def _target_chat_id(self, event, args) -> int | None:
        if args:
            try:
                return int(args[0])
            except (TypeError, ValueError):
                return None
        try:
            chat_id = int(getattr(event, "chat_id", 0) or 0)
        except (TypeError, ValueError):
            return None
        return chat_id if chat_id and chat_id != self._owner_id else None

    async def status_callback(self, call) -> None:
        await call.edit(await self._status_text(), reply_markup=self.inline_buttons(
            [{"text": "Refresh", "callback": self.status_callback, "args": ()}],
            [{"text": "Start", "callback": self.start_callback, "args": ()}, {"text": "Stop", "callback": self.stop_callback, "args": ()}],
            [{"text": "Queue", "callback": self.queue_callback, "args": ()}],
            [{"text": "Close", "callback": self.close_callback, "args": ()}],
        ))

    async def start_callback(self, call) -> None:
        self._set_config("enabled", True)
        await self.status_callback(call)

    async def stop_callback(self, call) -> None:
        self._set_config("enabled", False)
        if self._queue:
            await self._queue.clear_pending()
        await self.status_callback(call)

    async def queue_callback(self, call) -> None:
        await call.edit(await self._queue_text(), reply_markup=self.inline_buttons(
            [{"text": "Back", "callback": self.status_callback, "args": ()}],
            [{"text": "Close", "callback": self.close_callback, "args": ()}],
        ))

    async def close_callback(self, call) -> None:
        await call.edit("Closed.", reply_markup=None)
