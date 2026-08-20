# meta developer: @DeathTerror
# meta name: AIAnswerDtg
# requires: aiohttp

import asyncio
import json
import time
from collections import defaultdict

import aiohttp

from .. import loader, utils

BLUE = "🔵"
OK = "🔷"
INFO = "💎"
BOT = "🤖"
CLOCK = "🕘"
LOCK = "🛡️"
FIRE = "🔥"

DEEPSEEK_BASE_URL = "https://api.deepseek.com/chat/completions"
LIMIT_ERROR = "__AI_LIMIT_REACHED__"
MAX_API_RESPONSE_BYTES = 1024 * 1024

DEFAULT_TOXIC_PROMPT = (
    "Ты Telegram-автоответчик в стиле живого токсичного чела из чата. "
    "Отвечай коротко: 1-2 фразы, без лекций и без официоза. "
    "Пиши как человек: сарказм, подъёбы, маты, сухие добивания, иногда 'ну ты выдал', 'слабо', 'не дотягиваешь', 'иди просрись, мысль воняет'. "
    "Если собеседник агрится или несёт бред — отвечай дерзко и токсично, но не растягивай. "
    "Не используй угрозы физической расправы, призывы к самоповреждению, доксинг, травлю по национальности, расе, религии, полу, ориентации, инвалидности или другим защищённым признакам. "
    "Не пытайся быть моралистом. Если вопрос нормальный — ответь нормально, но всё равно живо и коротко. "
    "Стиль: грубый чатовый русский, можно с ошибками и разговорными словами, будто пишет реальный человек."
)


class AIAnswerDtgMod(loader.Module):
    """🔵 AIAnswerDtg — токсичный автоответчик через DeepSeek API для выбранного чата."""

    strings = {
        "name": "AIAnswerDtg",
        "description": "🔵 AI-автоответчик для чата на DeepSeek: токен, токсичный промпт, модель, старт/стоп, cooldown и короткие ответы.",
        "help": (
            "🔵 <b>Модуль AIAnswerDtg</b>\n"
            "Автоответчик на DeepSeek API. Работает только в том чате, где включили <code>.aistart</code>.\n\n"
            "<b>🔷 Настройка</b>\n"
            "<code>.aitoken sk-...</code> — сохранить DeepSeek API токен\n"
            "<code>.aiprompt [текст]</code> — задать свой промпт/роль\n"
            "<code>.aitoxic</code> — поставить готовый токсичный промпт\n"
            "<code>.ailimitreply [текст]</code> — текст вместо ошибки лимита\n"
            "<code>.aimodel [модель]</code> — выбрать модель, по умолчанию <code>deepseek-v4-flash</code>\n"
            "<code>.aicooldown [сек]</code> — задержка ответов, по умолчанию 12 сек\n\n"
            "<b>💎 Управление</b>\n"
            "<code>.aistart</code> — включить автоответчик в текущем чате\n"
            "<code>.aistop</code> — выключить в текущем чате\n"
            "<code>.aistatus</code> — статус настроек\n"
            "<code>.aitest [текст]</code> — проверить ответ без включения автоответчика\n"
            "<code>.aiclear</code> — очистить короткую память чата\n\n"
            "<b>🛡️ Логика</b>\n"
            "Отвечает на входящие сообщения в активном чате, не отвечает сам себе, не трогает команды с точкой/слэшем, держит cooldown, хранит короткий контекст.\n"
            "Если DeepSeek упёрся в лимит/баланс — в чат не кидает ошибку API, а отвечает заданной заглушкой.\n\n"
            "<b>🔵 Где взять токен:</b> https://platform.deepseek.com/api_keys\n"
        ),
    }
    strings_ru = strings

    def __init__(self):
        self.last_answer = defaultdict(lambda: 0)
        self.locks = defaultdict(asyncio.Lock)
        self.memory = defaultdict(list)

    async def client_ready(self, client, db):
        self.client = client
        self.db = db

    def cfg(self):
        return self.db.get("AIAnswerDtg", "cfg", {
            # Empty user-provided runtime setting; this is not a credential.
            "token": "",  # nosec B105
            "prompt": DEFAULT_TOXIC_PROMPT,
            "model": "deepseek-v4-flash",
            "cooldown": 12,
            "max_memory": 8,
            "limit_reply": "иди нахуй",
            "enabled_chats": {},
        })

    def save_cfg(self, cfg):
        self.db.set("AIAnswerDtg", "cfg", cfg)

    def token_mask(self, token):
        if not token:
            return "не задан"
        if len(token) <= 12:
            return "скрыт"
        return f"{token[:7]}...{token[-4:]}"

    def is_limit_error(self, status, msg):
        text = (msg or "").lower()
        markers = (
            "rate limit", "rate_limit", "too many requests", "quota", "insufficient_quota",
            "balance", "insufficient balance", "exceeded", "limit", "billing", "credits",
        )
        return status == 429 or any(marker in text for marker in markers)

    def normalize_cfg(self, cfg):
        if "enabled_chats" not in cfg:
            cfg["enabled_chats"] = {}
        if not cfg.get("model") or str(cfg.get("model", "")).startswith("gpt-"):
            cfg["model"] = "deepseek-v4-flash"
        if not cfg.get("prompt"):
            cfg["prompt"] = DEFAULT_TOXIC_PROMPT
        if not cfg.get("cooldown"):
            cfg["cooldown"] = 12
        if "limit_reply" not in cfg:
            cfg["limit_reply"] = "иди нахуй"
        return cfg

    @staticmethod
    def memory_limit(cfg):
        try:
            value = int(cfg.get("max_memory", 8) or 8)
        except (TypeError, ValueError):
            value = 8
        return max(1, min(value, 20))

    async def ask_ai(self, cfg, chat_id, user_text):
        cfg = self.normalize_cfg(cfg)
        token = cfg.get("token", "").strip()
        if not token:
            return None, "DeepSeek API токен не задан. Используй .aitoken sk-..."

        prompt = cfg.get("prompt") or DEFAULT_TOXIC_PROMPT
        model = cfg.get("model") or "deepseek-v4-flash"
        memory_limit = self.memory_limit(cfg)
        history = self.memory[str(chat_id)][-memory_limit:]

        messages = [{"role": "system", "content": prompt}]
        for item in history:
            messages.append({"role": item["role"], "content": item["content"]})
        messages.append({"role": "user", "content": str(user_text)[:4000]})

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": 1.05,
            "max_tokens": 180,
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=45, connect=10, sock_read=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(DEEPSEEK_BASE_URL, json=payload, headers=headers) as resp:
                    if resp.content_length and resp.content_length > MAX_API_RESPONSE_BYTES:
                        return None, "DeepSeek вернул слишком большой ответ."
                    raw = await resp.content.read(MAX_API_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_API_RESPONSE_BYTES:
                        return None, "DeepSeek вернул слишком большой ответ."
                    try:
                        data = json.loads(raw.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        return None, f"DeepSeek вернул некорректный JSON ({resp.status})."
                    if resp.status >= 400:
                        err = data.get("error", {}) if isinstance(data, dict) else {}
                        msg = err.get("message") or str(data)[:500]
                        if self.is_limit_error(resp.status, msg):
                            return None, LIMIT_ERROR
                        return None, f"DeepSeek ошибка {resp.status}: {msg}"
        except Exception as e:
            return None, f"Ошибка запроса к DeepSeek: {e}"

        text = ""
        try:
            text = data["choices"][0]["message"].get("content", "").strip()
        except Exception:
            text = ""

        if not text:
            return None, "DeepSeek вернул пустой ответ."

        # чтобы не лил простыни, даже если модель разогналась
        text = text.strip()
        if len(text) > 900:
            text = text[:900].rsplit(" ", 1)[0] + "..."

        mem = self.memory[str(chat_id)]
        mem.append({"role": "user", "content": user_text[:900]})
        mem.append({"role": "assistant", "content": text[:900]})
        max_items = memory_limit * 2
        if len(mem) > max_items:
            del mem[:-max_items]

        return text, None

    async def aitokencmd(self, message):
        """sk-... — сохранить DeepSeek API токен."""
        token = utils.get_args_raw(message).strip()
        if not token:
            return await utils.answer(message, f"{BLUE} <b>Формат:</b> <code>.aitoken sk-...</code>\n{INFO} Токен: <code>https://platform.deepseek.com/api_keys</code>")
        cfg = self.normalize_cfg(self.cfg())
        cfg["token"] = token
        self.save_cfg(cfg)
        await utils.answer(message, f"{OK} <b>DeepSeek API токен сохранён.</b>\n{LOCK} <i>В чат токен не вывожу, чтобы не спалить ключ.</i>")

    async def aipromptcmd(self, message):
        """[текст] — задать промпт автоответчика."""
        prompt = utils.get_args_raw(message).strip()
        cfg = self.normalize_cfg(self.cfg())
        if not prompt:
            return await utils.answer(message, f"{BLUE} <b>Текущий промпт:</b>\n<code>{utils.escape_html(cfg.get('prompt', ''))}</code>")
        cfg["prompt"] = prompt
        self.save_cfg(cfg)
        await utils.answer(message, f"{OK} <b>Промпт сохранён.</b>\n{INFO} <code>{utils.escape_html(prompt[:700])}</code>")

    async def aitoxiccmd(self, message):
        """— поставить готовый токсичный короткий промпт."""
        cfg = self.normalize_cfg(self.cfg())
        cfg["prompt"] = DEFAULT_TOXIC_PROMPT
        self.save_cfg(cfg)
        await utils.answer(message, f"{FIRE} <b>Токсичный короткий промпт установлен.</b>\n{BLUE}<code>{utils.escape_html(DEFAULT_TOXIC_PROMPT[:700])}</code>")

    async def ailimitreplycmd(self, message):
        """[текст] — что писать вместо ошибки лимита DeepSeek."""
        text = utils.get_args_raw(message).strip()
        cfg = self.normalize_cfg(self.cfg())
        if not text:
            return await utils.answer(message, f"{BLUE} <b>Текущий ответ при лимите:</b> <code>{utils.escape_html(cfg.get('limit_reply', 'иди нахуй'))}</code>")
        cfg["limit_reply"] = text[:300]
        self.save_cfg(cfg)
        await utils.answer(message, f"{OK} <b>Ответ при лимите сохранён:</b> <code>{utils.escape_html(cfg['limit_reply'])}</code>")

    async def aimodelcmd(self, message):
        """[модель] — выбрать модель. Пример: .aimodel deepseek-v4-flash"""
        model = utils.get_args_raw(message).strip()
        cfg = self.normalize_cfg(self.cfg())
        if not model:
            return await utils.answer(message, f"{BLUE} <b>Текущая модель:</b> <code>{utils.escape_html(cfg.get('model', 'deepseek-v4-flash'))}</code>\n{INFO} Для чата советую: <code>deepseek-v4-flash</code>")
        cfg["model"] = model
        self.save_cfg(cfg)
        await utils.answer(message, f"{OK} <b>Модель установлена:</b> <code>{utils.escape_html(model)}</code>")

    async def aicooldowncmd(self, message):
        """[сек] — задержка между автоответами."""
        raw = utils.get_args_raw(message).strip()
        cfg = self.normalize_cfg(self.cfg())
        if not raw:
            return await utils.answer(message, f"{CLOCK} <b>Cooldown:</b> <code>{cfg.get('cooldown', 12)} сек.</code>")
        try:
            sec = max(1, min(int(raw), 3600))
        except Exception:
            return await utils.answer(message, f"{BLUE} <b>Нужно число секунд.</b>")
        cfg["cooldown"] = sec
        self.save_cfg(cfg)
        await utils.answer(message, f"{OK} <b>Cooldown установлен:</b> <code>{sec} сек.</code>")

    async def aistartcmd(self, message):
        """— включить автоответчик в текущем чате."""
        cfg = self.normalize_cfg(self.cfg())
        cfg.setdefault("enabled_chats", {})[str(message.chat_id)] = True
        self.save_cfg(cfg)
        await utils.answer(message, f"{BOT} <b>AI-автоответчик DeepSeek включён в этом чате.</b>\n{BLUE} <b>Модель:</b> <code>{utils.escape_html(cfg.get('model'))}</code>\n{FIRE} <b>Стиль:</b> токсичный короткий чатовый")

    async def aistopcmd(self, message):
        """— выключить автоответчик в текущем чате."""
        cfg = self.normalize_cfg(self.cfg())
        cfg.setdefault("enabled_chats", {})[str(message.chat_id)] = False
        self.save_cfg(cfg)
        await utils.answer(message, f"{OK} <b>AI-автоответчик выключен в этом чате.</b>")

    async def aistatuscmd(self, message):
        """— показать статус."""
        cfg = self.normalize_cfg(self.cfg())
        enabled = cfg.get("enabled_chats", {}).get(str(message.chat_id), False)
        await utils.answer(
            message,
            f"{BOT} <b>AIAnswerDtg DeepSeek status</b>\n"
            f"{BLUE} <b>В этом чате:</b> <code>{'on' if enabled else 'off'}</code>\n"
            f"{BLUE} <b>Токен:</b> <code>{utils.escape_html(self.token_mask(cfg.get('token', '')))}</code>\n"
            f"{BLUE} <b>Модель:</b> <code>{utils.escape_html(cfg.get('model', 'deepseek-v4-flash'))}</code>\n"
            f"{CLOCK} <b>Cooldown:</b> <code>{cfg.get('cooldown', 12)} сек.</code>\n"
            f"{INFO} <b>API:</b> <code>DeepSeek /chat/completions</code>\n"
            f"{FIRE} <b>Лимит-ответ:</b> <code>{utils.escape_html(cfg.get('limit_reply', 'иди нахуй'))}</code>\n"
            f"{FIRE} <b>Промпт:</b> <code>{utils.escape_html(cfg.get('prompt', '')[:500])}</code>"
        )

    async def aitestcmd(self, message):
        """[текст] — тестовый запрос к DeepSeek без включения автоответчика."""
        text = utils.get_args_raw(message).strip()
        if not text:
            return await utils.answer(message, f"{BLUE} <b>Формат:</b> <code>.aitest ну что ты несёшь?</code>")
        loading = await utils.answer(message, f"{BOT} <b>DeepSeek думает...</b>")
        answer, err = await self.ask_ai(self.cfg(), message.chat_id, text)
        if err == LIMIT_ERROR:
            cfg = self.normalize_cfg(self.cfg())
            return await utils.answer(loading, f"{FIRE} <b>Лимит DeepSeek. В чате бот ответит:</b> <code>{utils.escape_html(cfg.get('limit_reply', 'иди нахуй'))}</code>")
        if err:
            return await utils.answer(loading, f"{BLUE} <b>{utils.escape_html(err)}</b>")
        await utils.answer(loading, f"{BOT} <b>Ответ:</b>\n{utils.escape_html(answer)}")

    async def aiclearcmd(self, message):
        """— очистить короткую память текущего чата."""
        self.memory[str(message.chat_id)] = []
        await utils.answer(message, f"{OK} <b>Память этого чата очищена.</b>")

    async def watcher(self, message):
        if not getattr(message, "chat_id", None):
            return
        if getattr(message, "out", False):
            return
        if not getattr(message, "raw_text", None):
            return

        text = message.raw_text.strip()
        if not text or text.startswith(".") or text.startswith("/"):
            return

        cfg = self.normalize_cfg(self.cfg())
        chat = str(message.chat_id)
        if not cfg.get("enabled_chats", {}).get(chat, False):
            return

        cooldown = int(cfg.get("cooldown", 12))
        current = int(time.time())
        if current - self.last_answer[chat] < cooldown:
            return

        async with self.locks[chat]:
            current = int(time.time())
            if current - self.last_answer[chat] < cooldown:
                return
            self.last_answer[chat] = current

            try:
                async with self.client.action(message.chat_id, "typing"):
                    answer, err = await self.ask_ai(cfg, message.chat_id, text)
            except Exception:
                answer, err = await self.ask_ai(cfg, message.chat_id, text)

            if err == LIMIT_ERROR:
                await message.reply(utils.escape_html(cfg.get("limit_reply", "иди нахуй")))
                return

            if err:
                await message.reply(f"{BLUE} <b>AIAnswerDtg:</b> <code>{utils.escape_html(err)}</code>")
                return

            if answer:
                await message.reply(utils.escape_html(answer))
