# meta developer: @DeathTerror
# meta name: AdminToolsDtg
# requires: telethon

import asyncio
import logging
import re
import time
from collections import defaultdict, deque

from telethon.errors import ChatAdminRequiredError, UserAdminInvalidError, UserNotParticipantError
from telethon.tl.functions.channels import EditAdminRequest, EditBannedRequest, EditTitleRequest, ToggleSlowModeRequest
from telethon.tl.functions.messages import EditChatDefaultBannedRightsRequest
from telethon.tl.types import ChatAdminRights, ChatBannedRights

from .. import loader, utils

logger = logging.getLogger("deathtg.modules.AdminToolsDtg")

BLUE = "🔵"
OK = "🔷"
INFO = "💎"
SHIELD = "🛡️"
HAMMER = "🔨"
CLOCK = "🕘"
WARN = "🌀"


def now():
    return int(time.time())


def esc(text):
    return utils.escape_html((text or "").strip() or "без причины")


def parse_time(value=None, default=3600):
    if not value:
        return default
    match = re.fullmatch(r"(\d+)([smhdwмчдн]?)", value.strip().lower())
    if not match:
        return default
    amount, unit = int(match.group(1)), match.group(2)
    return max(1, amount * {"": 60, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "м": 60, "ч": 3600, "д": 86400, "н": 604800}.get(unit, 60))


def human(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} сек."
    if seconds < 3600:
        return f"{seconds // 60} мин."
    if seconds < 86400:
        return f"{seconds // 3600} ч."
    return f"{seconds // 86400} д."


class AdminToolsDtgMod(loader.Module):
    """🔵 AdminToolsDtg — админский набор команд для групп и каналов."""

    strings = {
        "name": "AdminToolsDtg",
        "description": "🔵 Управление группами/каналами: бан, мут, варн, кик, админка, антиспам, slowmode, чистка и инлайн-панель.",
        "help": (
            "🔵 <b>Модуль AdminToolsDtg</b>\n"
            "Создан для управления группами и каналами: модерация, админки, антиспам, варны, муты, баны и быстрые кнопки.\n\n"
            "<b>🔷 Модерация</b>\n"
            "<code>.ban [текст - причина]</code> — банит пользователя\n"
            "<code>.unban</code> — снимает бан\n"
            "<code>.kick [причина]</code> — кикает пользователя\n"
            "<code>.mute [время] [причина]</code> — мутит, пример: <code>.mute 30m флуд</code>\n"
            "<code>.unmute</code> — снимает мут\n"
            "<code>.warn [причина]</code> — выдаёт варн\n"
            "<code>.unwarn</code> — снимает один варн\n"
            "<code>.warns</code> — показывает варны\n"
            "<code>.resetwarns</code> — сбрасывает варны\n\n"
            "<b>🛡️ Админка</b>\n"
            "<code>.admin [титул]</code> — выдаёт админку\n"
            "<code>.demote</code> — снимает с админки\n"
            "<code>.rights</code> — статус модуля\n\n"
            "<b>🌀 Чат</b>\n"
            "<code>.purge</code> — чистит сообщения от ответа до команды\n"
            "<code>.del</code> — удаляет сообщение по ответу\n"
            "<code>.pin [текст]</code> — закрепляет\n"
            "<code>.unpin</code> — открепляет\n"
            "<code>.slowmode [сек]</code> — задержка отправки\n"
            "<code>.lock</code> — закрыть чат\n"
            "<code>.unlock</code> — открыть чат\n"
            "<code>.settitle [текст]</code> — сменить название\n\n"
            "<b>🔵 Антиспам</b>\n"
            "<code>.antispam on/off/status</code> — управление\n"
            "<code>.antispam limit 5 7 10m</code> — 5 сообщений за 7 сек = мут на 10 мин\n\n"
            "<b>💎 Инлайн</b>\n"
            "<code>.adminpanel</code> — красивая панель действий\n"
            "<code>.poll Вопрос | Да | Нет | Возможно</code> — опрос с кнопками\n"
        ),
    }
    strings_ru = strings

    def __init__(self):
        self.spam = defaultdict(lambda: deque(maxlen=25))
        self.locks = defaultdict(asyncio.Lock)

    async def client_ready(self, client, db):
        self.client = client
        self.db = db
        me = await client.get_me()
        self.owner_id = int(getattr(me, "id", 0) or 0)

    async def owner_callback(self, call, expected_owner: int) -> bool:
        actor_id = int(getattr(call, "sender_id", 0) or 0)
        if actor_id == int(expected_owner) == self.owner_id:
            return True
        try:
            await call.answer("Только владелец панели", show_alert=True)
        except Exception:
            logger.debug("Unable to show owner-only callback alert", exc_info=True)
        return False

    def cfg(self):
        return self.db.get("AdminToolsDtg", "cfg", {"antispam": {}, "limit": 5, "window": 7, "mute": 600, "warn_limit": 3})

    def save_cfg(self, cfg):
        self.db.set("AdminToolsDtg", "cfg", cfg)

    def warns(self):
        return self.db.get("AdminToolsDtg", "warns", {})

    def save_warns(self, warns):
        self.db.set("AdminToolsDtg", "warns", warns)

    async def target(self, message):
        reply = await message.get_reply_message()
        args = utils.get_args_raw(message)
        if reply:
            return await reply.get_sender(), args
        parts = args.split(maxsplit=1)
        if not parts:
            return None, args
        try:
            return await self.client.get_entity(parts[0]), parts[1] if len(parts) > 1 else ""
        except Exception:
            return None, args

    async def chat_only(self, message):
        if message.is_private:
            await utils.answer(message, f"{BLUE} <b>Это работает только в группе/канале.</b>")
            return False
        return True

    def ban_rights(self, until=None):
        return ChatBannedRights(until_date=until, view_messages=True)

    def mute_rights(self, until=None):
        return ChatBannedRights(until_date=until, send_messages=True, send_media=True, send_stickers=True, send_gifs=True, send_games=True, send_inline=True, embed_links=True)

    def free_rights(self):
        return ChatBannedRights(until_date=None, view_messages=False, send_messages=False, send_media=False, send_stickers=False, send_gifs=False, send_games=False, send_inline=False, embed_links=False)

    async def bancmd(self, message):
        """[причина] — банит пользователя по ответу или username/id."""
        if not await self.chat_only(message):
            return
        user, reason = await self.target(message)
        if not user:
            return await utils.answer(message, f"{BLUE} <b>Ответь на пользователя или укажи username/id.</b>")
        try:
            await self.client(EditBannedRequest(message.chat_id, user.id, self.ban_rights()))
            await utils.answer(message, f"{HAMMER} <b>Пользователь забанен.</b>\n{INFO} <b>Причина:</b> <code>{esc(reason)}</code>")
        except (ChatAdminRequiredError, UserAdminInvalidError):
            await utils.answer(message, f"{BLUE} <b>Не хватает прав или цель выше меня по правам.</b>")
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Не смог забанить:</b> <code>{utils.escape_html(str(e))}</code>")

    async def unbancmd(self, message):
        """— снимает бан."""
        if not await self.chat_only(message):
            return
        user, _ = await self.target(message)
        if not user:
            return await utils.answer(message, f"{BLUE} <b>Ответь на пользователя или укажи username/id.</b>")
        try:
            await self.client(EditBannedRequest(message.chat_id, user.id, self.free_rights()))
            await utils.answer(message, f"{OK} <b>Бан снят.</b>")
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Не смог снять бан:</b> <code>{utils.escape_html(str(e))}</code>")

    async def kickcmd(self, message):
        """[причина] — кикает пользователя."""
        if not await self.chat_only(message):
            return
        user, reason = await self.target(message)
        if not user:
            return await utils.answer(message, f"{BLUE} <b>Ответь на пользователя или укажи username/id.</b>")
        try:
            await self.client(EditBannedRequest(message.chat_id, user.id, self.ban_rights(now() + 45)))
            await asyncio.sleep(1)
            await self.client(EditBannedRequest(message.chat_id, user.id, self.free_rights()))
            await utils.answer(message, f"{HAMMER} <b>Пользователь кикнут.</b>\n{INFO} <b>Причина:</b> <code>{esc(reason)}</code>")
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Не смог кикнуть:</b> <code>{utils.escape_html(str(e))}</code>")

    async def mutecmd(self, message):
        """[время] [причина] — мутит пользователя. Пример: .mute 30m флуд"""
        if not await self.chat_only(message):
            return
        user, args = await self.target(message)
        if not user:
            return await utils.answer(message, f"{BLUE} <b>Ответь на пользователя или укажи username/id.</b>")
        parts = args.split(maxsplit=1)
        seconds = parse_time(parts[0], 3600) if parts else 3600
        reason = parts[1] if len(parts) > 1 else "без причины"
        try:
            await self.client(EditBannedRequest(message.chat_id, user.id, self.mute_rights(now() + seconds)))
            await utils.answer(message, f"{CLOCK} <b>Мут на {human(seconds)}.</b>\n{INFO} <b>Причина:</b> <code>{esc(reason)}</code>")
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Не смог выдать мут:</b> <code>{utils.escape_html(str(e))}</code>")

    async def unmutecmd(self, message):
        """— снимает мут."""
        if not await self.chat_only(message):
            return
        user, _ = await self.target(message)
        if not user:
            return await utils.answer(message, f"{BLUE} <b>Ответь на пользователя или укажи username/id.</b>")
        try:
            await self.client(EditBannedRequest(message.chat_id, user.id, self.free_rights()))
            await utils.answer(message, f"{OK} <b>Мут снят.</b>")
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Не смог снять мут:</b> <code>{utils.escape_html(str(e))}</code>")

    async def warncmd(self, message):
        """[причина] — выдаёт предупреждение, на лимите мутит."""
        if not await self.chat_only(message):
            return
        user, reason = await self.target(message)
        if not user:
            return await utils.answer(message, f"{BLUE} <b>Ответь на пользователя или укажи username/id.</b>")
        cfg, warns = self.cfg(), self.warns()
        chat, uid = str(message.chat_id), str(user.id)
        warns.setdefault(chat, {}).setdefault(uid, []).append({"reason": reason or "без причины", "time": now()})
        count, limit = len(warns[chat][uid]), int(cfg.get("warn_limit", 3))
        text = f"{WARN} <b>Варн выдан.</b>\n{BLUE} <b>Варны:</b> <code>{count}/{limit}</code>\n{INFO} <b>Причина:</b> <code>{esc(reason)}</code>"
        if count >= limit:
            mute = int(cfg.get("mute", 600))
            try:
                await self.client(EditBannedRequest(message.chat_id, user.id, self.mute_rights(now() + mute)))
                warns[chat][uid] = []
                text += f"\n{CLOCK} <b>Лимит варнов: мут на {human(mute)}.</b>"
            except Exception:
                text += f"\n{BLUE} <b>Лимит достигнут, но мут не выдался.</b>"
        self.save_warns(warns)
        await utils.answer(message, text)

    async def unwarncmd(self, message):
        """— снимает один варн."""
        if not await self.chat_only(message):
            return
        user, _ = await self.target(message)
        if not user:
            return await utils.answer(message, f"{BLUE} <b>Ответь на пользователя или укажи username/id.</b>")
        warns, chat, uid = self.warns(), str(message.chat_id), str(user.id)
        if warns.get(chat, {}).get(uid):
            warns[chat][uid].pop()
            self.save_warns(warns)
            return await utils.answer(message, f"{OK} <b>Один варн снят.</b>")
        await utils.answer(message, f"{BLUE} <b>У пользователя нет варнов.</b>")

    async def warnscmd(self, message):
        """— показывает варны."""
        if not await self.chat_only(message):
            return
        user, _ = await self.target(message)
        if not user:
            return await utils.answer(message, f"{BLUE} <b>Ответь на пользователя или укажи username/id.</b>")
        items = self.warns().get(str(message.chat_id), {}).get(str(user.id), [])
        if not items:
            return await utils.answer(message, f"{OK} <b>Варнов нет.</b>")
        lines = [f"{BLUE} <b>Варны:</b> <code>{len(items)}</code>"] + [f"{i}. <code>{esc(x.get('reason'))}</code>" for i, x in enumerate(items[-10:], 1)]
        await utils.answer(message, "\n".join(lines))

    async def resetwarnscmd(self, message):
        """— сбрасывает варны."""
        if not await self.chat_only(message):
            return
        user, _ = await self.target(message)
        if not user:
            return await utils.answer(message, f"{BLUE} <b>Ответь на пользователя или укажи username/id.</b>")
        warns = self.warns()
        warns.setdefault(str(message.chat_id), {})[str(user.id)] = []
        self.save_warns(warns)
        await utils.answer(message, f"{OK} <b>Варны сброшены.</b>")

    async def admincmd(self, message):
        """[титул] — выдаёт админку."""
        if not await self.chat_only(message):
            return
        user, title = await self.target(message)
        if not user:
            return await utils.answer(message, f"{BLUE} <b>Ответь на пользователя или укажи username/id.</b>")
        rights = ChatAdminRights(change_info=True, post_messages=True, edit_messages=True, delete_messages=True, ban_users=True, invite_users=True, pin_messages=True, add_admins=False, anonymous=False, manage_call=True, other=True)
        try:
            await self.client(EditAdminRequest(message.chat_id, user.id, rights, (title or "Admin")[:16]))
            await utils.answer(message, f"{SHIELD} <b>Админка выдана.</b>")
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Не смог выдать админку:</b> <code>{utils.escape_html(str(e))}</code>")

    async def demotecmd(self, message):
        """— снимает с админки."""
        if not await self.chat_only(message):
            return
        user, _ = await self.target(message)
        if not user:
            return await utils.answer(message, f"{BLUE} <b>Ответь на пользователя или укажи username/id.</b>")
        rights = ChatAdminRights(change_info=False, post_messages=False, edit_messages=False, delete_messages=False, ban_users=False, invite_users=False, pin_messages=False, add_admins=False, anonymous=False, manage_call=False, other=False)
        try:
            await self.client(EditAdminRequest(message.chat_id, user.id, rights, ""))
            await utils.answer(message, f"{OK} <b>Админка снята.</b>")
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Не смог снять админку:</b> <code>{utils.escape_html(str(e))}</code>")

    async def purgecmd(self, message):
        """— удаляет сообщения от ответа до команды."""
        if not await self.chat_only(message):
            return
        reply = await message.get_reply_message()
        if not reply:
            return await utils.answer(message, f"{BLUE} <b>Ответь на сообщение, откуда чистить.</b>")
        ids = list(range(reply.id, message.id + 1))
        try:
            for i in range(0, len(ids), 100):
                await self.client.delete_messages(message.chat_id, ids[i:i + 100], revoke=True)
            done = await message.respond(f"{OK} <b>Удалено:</b> <code>{len(ids)}</code>")
            await asyncio.sleep(3)
            await done.delete()
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Не смог почистить:</b> <code>{utils.escape_html(str(e))}</code>")

    async def delcmd(self, message):
        """— удаляет сообщение по ответу."""
        reply = await message.get_reply_message()
        if not reply:
            return await utils.answer(message, f"{BLUE} <b>Ответь на сообщение.</b>")
        await reply.delete()
        await message.delete()

    async def pincmd(self, message):
        """[текст] — закрепляет ответ или новый текст."""
        if not await self.chat_only(message):
            return
        reply, text = await message.get_reply_message(), utils.get_args_raw(message)
        try:
            target = reply or await message.respond(text or "🔵 Закреплено")
            await self.client.pin_message(message.chat_id, target, notify=False)
            await utils.answer(message, f"{OK} <b>Закреплено.</b>")
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Не смог закрепить:</b> <code>{utils.escape_html(str(e))}</code>")

    async def unpincmd(self, message):
        """— открепляет ответ или последний закреп."""
        if not await self.chat_only(message):
            return
        reply = await message.get_reply_message()
        try:
            await self.client.unpin_message(message.chat_id, reply.id if reply else None)
            await utils.answer(message, f"{OK} <b>Откреплено.</b>")
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Не смог открепить:</b> <code>{utils.escape_html(str(e))}</code>")

    async def slowmodecmd(self, message):
        """[сек] — ставит задержку сообщений."""
        if not await self.chat_only(message):
            return
        try:
            seconds = max(0, min(int(utils.get_args_raw(message) or 0), 3600))
            await self.client(ToggleSlowModeRequest(message.chat_id, seconds))
            await utils.answer(message, f"{CLOCK} <b>Slowmode:</b> <code>{seconds} сек.</code>")
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Не смог изменить slowmode:</b> <code>{utils.escape_html(str(e))}</code>")

    async def lockcmd(self, message):
        """— закрывает чат для обычных участников."""
        if not await self.chat_only(message):
            return
        try:
            await self.client(EditChatDefaultBannedRightsRequest(message.chat_id, ChatBannedRights(until_date=None, send_messages=True)))
            await utils.answer(message, f"{SHIELD} <b>Чат закрыт. Пишут только админы.</b>")
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Не смог закрыть чат:</b> <code>{utils.escape_html(str(e))}</code>")

    async def unlockcmd(self, message):
        """— открывает чат."""
        if not await self.chat_only(message):
            return
        try:
            await self.client(EditChatDefaultBannedRightsRequest(message.chat_id, ChatBannedRights(until_date=None, send_messages=False)))
            await utils.answer(message, f"{OK} <b>Чат открыт.</b>")
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Не смог открыть чат:</b> <code>{utils.escape_html(str(e))}</code>")

    async def settitlecmd(self, message):
        """[текст] — меняет название чата."""
        if not await self.chat_only(message):
            return
        title = utils.get_args_raw(message)
        if not title:
            return await utils.answer(message, f"{BLUE} <b>Укажи новое название.</b>")
        try:
            await self.client(EditTitleRequest(message.chat_id, title))
            await utils.answer(message, f"{OK} <b>Название изменено:</b> <code>{utils.escape_html(title)}</code>")
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Не смог изменить название:</b> <code>{utils.escape_html(str(e))}</code>")

    async def rightscmd(self, message):
        """— показывает статус модуля."""
        cfg = self.cfg()
        enabled = cfg.get("antispam", {}).get(str(message.chat_id), False)
        await utils.answer(message, f"{SHIELD} <b>AdminToolsDtg</b>\n{BLUE} <b>Антиспам:</b> <code>{'on' if enabled else 'off'}</code>\n{BLUE} <b>Лимит:</b> <code>{cfg.get('limit')} / {cfg.get('window')} сек.</code>\n{CLOCK} <b>Автомут:</b> <code>{human(cfg.get('mute'))}</code>\n{WARN} <b>Лимит варнов:</b> <code>{cfg.get('warn_limit')}</code>")

    async def antispamcmd(self, message):
        """on/off/status/limit [сообщения] [сек] [мут] — антиспам."""
        if not await self.chat_only(message):
            return
        args, cfg, chat = utils.get_args_raw(message).split(), self.cfg(), str(message.chat_id)
        cfg.setdefault("antispam", {})
        if not args or args[0].lower() == "status":
            return await utils.answer(message, f"{BLUE} <b>Антиспам:</b> <code>{'on' if cfg['antispam'].get(chat) else 'off'}</code>\n{INFO} <b>Правило:</b> <code>{cfg.get('limit')} сообщений / {cfg.get('window')} сек.</code>\n{CLOCK} <b>Наказание:</b> <code>мут {human(cfg.get('mute'))}</code>")
        action = args[0].lower()
        if action in {"on", "enable", "вкл"}:
            cfg["antispam"][chat] = True
        elif action in {"off", "disable", "выкл"}:
            cfg["antispam"][chat] = False
        elif action == "limit":
            try:
                cfg["limit"] = max(2, int(args[1]))
                cfg["window"] = max(2, int(args[2]))
                cfg["mute"] = parse_time(args[3], 600) if len(args) > 3 else int(cfg.get("mute", 600))
            except Exception:
                return await utils.answer(message, f"{BLUE} <b>Формат:</b> <code>.antispam limit 5 7 10m</code>")
        else:
            return await utils.answer(message, f"{BLUE} <b>Команды:</b> <code>.antispam on/off/status/limit</code>")
        self.save_cfg(cfg)
        await utils.answer(message, f"{OK} <b>Антиспам обновлён.</b>")

    async def pollcmd(self, message):
        """Вопрос | Ответ 1 | Ответ 2 — опрос с кнопками."""
        parts = [x.strip() for x in utils.get_args_raw(message).split("|") if x.strip()]
        if len(parts) < 3:
            return await utils.answer(message, f"{BLUE} <b>Формат:</b> <code>.poll Вопрос | Да | Нет | Возможно</code>")
        question, answers = parts[0], parts[1:11]
        text = f"{BLUE} <b>{utils.escape_html(question)}</b>\n\n" + "\n".join(f"{i}. {utils.escape_html(a)}" for i, a in enumerate(answers, 1))
        buttons = [[{"text": f"🔵 {i}", "callback": self.poll_vote, "args": (answer,)} for i, answer in enumerate(answers, 1)]]
        try:
            await self.inline.form(text, message=message, reply_markup=buttons, ttl=86400)
        except Exception:
            await utils.answer(message, text + "\n\n🔵 <i>Инлайн недоступен, отправил текстом.</i>")

    async def poll_vote(self, call, answer):
        await call.answer(f"Голос принят: {answer}", show_alert=False)

    async def adminpanelcmd(self, message):
        """— инлайн-панель быстрых действий."""
        if not await self.chat_only(message):
            return
        cfg = self.cfg()
        enabled = cfg.get("antispam", {}).get(str(message.chat_id), False)
        text = f"{SHIELD} <b>AdminToolsDtg Panel</b>\n\n{BLUE} <b>Чат:</b> <code>{message.chat_id}</code>\n{BLUE} <b>Антиспам:</b> <code>{'on' if enabled else 'off'}</code>\n{INFO} <b>Быстрые действия ниже.</b>"
        markup = [
            [{"text": "🔵 Antispam ON", "callback": self.panel_antispam, "args": (message.chat_id, True, message.sender_id)}, {"text": "⚪ Antispam OFF", "callback": self.panel_antispam, "args": (message.chat_id, False, message.sender_id)}],
            [{"text": "🛡️ Lock chat", "callback": self.panel_lock, "args": (message.chat_id, True, message.sender_id)}, {"text": "🔷 Unlock chat", "callback": self.panel_lock, "args": (message.chat_id, False, message.sender_id)}],
            [{"text": "💎 Status", "callback": self.panel_status, "args": (message.chat_id, message.sender_id)}],
        ]
        try:
            await self.inline.form(text, message=message, reply_markup=markup, ttl=3600)
        except Exception as e:
            await utils.answer(message, f"{BLUE} <b>Инлайн недоступен:</b> <code>{utils.escape_html(str(e))}</code>")

    async def panel_antispam(self, call, chat_id, state, expected_owner):
        if not await self.owner_callback(call, expected_owner):
            return
        cfg = self.cfg()
        cfg.setdefault("antispam", {})[str(chat_id)] = state
        self.save_cfg(cfg)
        await call.answer(f"Антиспам {'включён' if state else 'выключен'}", show_alert=False)
        await call.edit(f"{OK} <b>Антиспам {'включён' if state else 'выключен'}.</b>")

    async def panel_lock(self, call, chat_id, locked, expected_owner):
        if not await self.owner_callback(call, expected_owner):
            return
        try:
            await self.client(EditChatDefaultBannedRightsRequest(chat_id, ChatBannedRights(until_date=None, send_messages=locked)))
            await call.answer("Готово", show_alert=False)
            await call.edit(f"{SHIELD if locked else OK} <b>Чат {'закрыт' if locked else 'открыт'}.</b>")
        except Exception as e:
            await call.answer(str(e), show_alert=True)

    async def panel_status(self, call, chat_id, expected_owner):
        if not await self.owner_callback(call, expected_owner):
            return
        cfg = self.cfg()
        enabled = cfg.get("antispam", {}).get(str(chat_id), False)
        await call.answer("Статус обновлён", show_alert=False)
        await call.edit(f"{SHIELD} <b>AdminToolsDtg Status</b>\n{BLUE} <b>Антиспам:</b> <code>{'on' if enabled else 'off'}</code>\n{BLUE} <b>Лимит:</b> <code>{cfg.get('limit')} / {cfg.get('window')} сек.</code>\n{CLOCK} <b>Мут:</b> <code>{human(cfg.get('mute'))}</code>")

    async def watcher(self, message):
        if (not getattr(message, "is_group", False) and not getattr(message, "is_channel", False)) or getattr(message, "out", False) or not getattr(message, "sender_id", None):
            return
        cfg, chat = self.cfg(), str(message.chat_id)
        if not cfg.get("antispam", {}).get(chat):
            return
        key = (message.chat_id, message.sender_id)
        async with self.locks[key]:
            bucket, t = self.spam[key], now()
            bucket.append(t)
            while bucket and t - bucket[0] > int(cfg.get("window", 7)):
                bucket.popleft()
            if len(bucket) < int(cfg.get("limit", 5)):
                return
            bucket.clear()
            try:
                mute = int(cfg.get("mute", 600))
                await self.client(EditBannedRequest(message.chat_id, message.sender_id, self.mute_rights(t + mute)))
                await message.respond(f"{BLUE} <b>Антиспам сработал.</b>\n{CLOCK} <b>Мут на {human(mute)}.</b>")
            except (ChatAdminRequiredError, UserAdminInvalidError, UserNotParticipantError):
                logger.debug("Antispam cannot mute user in chat %s", message.chat_id)
            except Exception:
                logger.exception("Antispam mute failed in chat %s", message.chat_id)
