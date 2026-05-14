# meta developer: @DeathTerror
# meta name: AIAnswerDtg
# requires: aiohttp

import asyncio
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


class AIAnswerDtgMod(loader.Module):
    """🔵 AIAnswerDtg — автоответчик через OpenAI API для выбранного чата."""

    strings = {
        "name": "AIAnswerDtg",
        "description": "🔵 AI-автоответчик для чата: OpenAI токен, промпт, модель, старт/стоп, cooldown и ответы по заданному стилю.",
        "help": (
            "🔵 <b>Модуль AIAnswerDtg</b>\n"
            "Автоответчик на OpenAI API. Работает только в том чате, где включили <code>.aistart</code>.\n\n"
            "<b>🔷 Настройка</b>\n"
            "<code>.aitoken sk-...</code> — сохранить OpenAI API токен\n"
            "<code>.aiprompt [текст]</code> — задать промпт/роль автоответчика\n"
            "<code>.aimodel [модель]</code> — выбрать модель, по умолчанию <code>gpt-4o-mini</code>\n"
            "<code>.aicooldown [сек]</code> — задержка ответов, по умолчанию 8 сек\n\n"
            "<b>💎 Управление</b>\n"
            "<code>.aistart</code> — включить автоответчик в текущем чате\n"
            "<code>.aistop</code> — выключить в текущем чате\n"
            "<code>.aistatus</code> — статус настроек\n"
            "<code>.aitest [текст]</code> — проверить ответ без включения автоответчика\n"
            "<code>.aiclear</code> — очистить короткую память чата\n\n"
            "<b>🛡️ Логика</b>\n"
            "Отвечает на входящие сообщения в активном чате, не отвечает сам себе, не трогает команды с точкой, держит cooldown, хранит короткий контекст последних сообщений.\n\n"
            "<b>🔵 Где взять токен:</b> OpenAI Platform → API keys → Create new secret key.\n"
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
            "token": "",
            "prompt": "Ты дружелюбный автоответчик. Отвечай коротко, понятно и по делу.",
            "model": "gpt-4o-mini",
            "cooldown": 8,
            "max_memory": 8,
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

    async def ask_openai(self, cfg, chat_id, user_text):
        token = cfg.get("token", "").strip()
        if not token:
            return None, "OpenAI API токен не задан. Используй .aitoken sk-..."

        prompt = cfg.get("prompt") or "Отвечай коротко и по делу."
        model = cfg.get("model") or "gpt-4o-mini"
        history = self.memory[str(chat_id)][-int(cfg.get("max_memory", 8)):]

        input_items = [{"role": "system", "content": prompt}]
        for item in history:
            input_items.append({"role": item["role"], "content": item["content"]})
        input_items.append({"role": "user", "content": user_text})

        payload = {
            "model": model,
            "input": input_items,
            "max_output_tokens": 500,
            "temperature": 0.7,
        }

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=45)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post("https://api.openai.com/v1/responses", json=payload, headers=headers) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status >= 400:
                        err = data.get("error", {}) if isinstance(data, dict) else {}
                        msg = err.get("message") or str(data)[:500]
                        return None, f"OpenAI ошибка {resp.status}: {msg}"
        except Exception as e:
            return None, f"Ошибка запроса: {e}"

        text = ""
        try:
            if data.get("output_text"):
                text = data["output_text"]
            else:
                parts = []
                for out in data.get("output", []):
                    for content in out.get("content", []):
                        if content.get("type") in {"output_text", "text"}:
                            parts.append(content.get("text", ""))
                text = "\n".join(x for x in parts if x).strip()
        except Exception:
            text = ""

        if not text:
            return None, "OpenAI вернул пустой ответ."

        mem = self.memory[str(chat_id)]
        mem.append({"role": "user", "content": user_text[:1200]})
        mem.append({"role": "assistant", "content": text[:1200]})
        max_items = int(cfg.get("max_memory", 8)) * 2
        if len(mem) > max_items:
            del mem[:-max_items]

        return text.strip(), None

    async def aitokencmd(self, message):
        """sk-... — сохранить OpenAI API токен."""
        token = utils.get_args_raw(message).strip()
        if not token:
            return await utils.answer(message, f"{BLUE} <b>Формат:</b> <code>.aitoken sk-...</code>\n{INFO} Токен можно взять на OpenAI Platform → API keys.")
        cfg = self.cfg()
        cfg["token"] = token
        self.save_cfg(cfg)
        await utils.answer(message, f"{OK} <b>OpenAI API токен сохранён.</b>\n{LOCK} <i>В чат токен не вывожу, чтобы не спалить ключ.</i>")

    async def aipromptcmd(self, message):
        """[текст] — задать промпт автоответчика."""
        prompt = utils.get_args_raw(message).strip()
        if not prompt:
            cfg = self.cfg()
            return await utils.answer(message, f"{BLUE} <b>Текущий промпт:</b>\n<code>{utils.escape_html(cfg.get('prompt', ''))}</code>")
        cfg = self.cfg()
        cfg["prompt"] = prompt
        self.save_cfg(cfg)
        await utils.answer(message, f"{OK} <b>Промпт сохранён.</b>\n{INFO} <code>{utils.escape_html(prompt[:700])}</code>")

    async def aimodelcmd(self, message):
        """[модель] — выбрать модель. Пример: .aimodel gpt-4o-mini"""
        model = utils.get_args_raw(message).strip()
        cfg = self.cfg()
        if not model:
            return await utils.answer(message, f"{BLUE} <b>Текущая модель:</b> <code>{utils.escape_html(cfg.get('model', 'gpt-4o-mini'))}</code>")
        cfg["model"] = model
        self.save_cfg(cfg)
        await utils.answer(message, f"{OK} <b>Модель установлена:</b> <code>{utils.escape_html(model)}</code>")

    async def aicooldowncmd(self, message):
        """[сек] — задержка между автоответами."""
        raw = utils.get_args_raw(message).strip()
        cfg = self.cfg()
        if not raw:
            return await utils.answer(message, f"{CLOCK} <b>Cooldown:</b> <code>{cfg.get('cooldown', 8)} сек.</code>")
        try:
            sec = max(1, min(int(raw), 3600))
        except Exception:
            return await utils.answer(message, f"{BLUE} <b>Нужно число секунд.</b>")
        cfg["cooldown"] = sec
        self.save_cfg(cfg)
        await utils.answer(message, f"{OK} <b>Cooldown установлен:</b> <code>{sec} сек.</code>")

    async def aistartcmd(self, message):
        """— включить автоответчик в текущем чате."""
        cfg = self.cfg()
        cfg.setdefault("enabled_chats", {})[str(message.chat_id)] = True
        self.save_cfg(cfg)
        await utils.answer(message, f"{BOT} <b>AI-автоответчик включён в этом чате.</b>\n{BLUE} <b>Промпт:</b> <code>{utils.escape_html(cfg.get('prompt', '')[:300])}</code>")

    async def aistopcmd(self, message):
        """— выключить автоответчик в текущем чате."""
        cfg = self.cfg()
        cfg.setdefault("enabled_chats", {})[str(message.chat_id)] = False
        self.save_cfg(cfg)
        await utils.answer(message, f"{OK} <b>AI-автоответчик выключен в этом чате.</b>")

    async def aistatuscmd(self, message):
        """— показать статус."""
        cfg = self.cfg()
        enabled = cfg.get("enabled_chats", {}).get(str(message.chat_id), False)
        await utils.answer(
            message,
            f"{BOT} <b>AIAnswerDtg status</b>\n"
            f"{BLUE} <b>В этом чате:</b> <code>{'on' if enabled else 'off'}</code>\n"
            f"{BLUE} <b>Токен:</b> <code>{utils.escape_html(self.token_mask(cfg.get('token', '')))}</code>\n"
            f"{BLUE} <b>Модель:</b> <code>{utils.escape_html(cfg.get('model', 'gpt-4o-mini'))}</code>\n"
            f"{CLOCK} <b>Cooldown:</b> <code>{cfg.get('cooldown', 8)} сек.</code>\n"
            f"{INFO} <b>Промпт:</b> <code>{utils.escape_html(cfg.get('prompt', '')[:500])}</code>"
        )

    async def aitestcmd(self, message):
        """[текст] — тестовый запрос к OpenAI без включения автоответчика."""
        text = utils.get_args_raw(message).strip()
        if not text:
            return await utils.answer(message, f"{BLUE} <b>Формат:</b> <code>.aitest привет, кто ты?</code>")
        loading = await utils.answer(message, f"{BOT} <b>Думаю...</b>")
        answer, err = await self.ask_openai(self.cfg(), message.chat_id, text)
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

        cfg = self.cfg()
        chat = str(message.chat_id)
        if not cfg.get("enabled_chats", {}).get(chat, False):
            return

        cooldown = int(cfg.get("cooldown", 8))
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
                    answer, err = await self.ask_openai(cfg, message.chat_id, text)
            except Exception:
                answer, err = await self.ask_openai(cfg, message.chat_id, text)

            if err:
                await message.reply(f"{BLUE} <b>AIAnswerDtg:</b> <code>{utils.escape_html(err)}</code>")
                return

            if answer:
                await message.reply(utils.escape_html(answer))
