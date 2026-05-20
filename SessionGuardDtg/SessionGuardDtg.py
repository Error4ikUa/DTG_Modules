# meta developer: @DeathTerror
# meta name: SessionGuardDtg
# meta description: Telegram session guard with allowed devices, inline whitelist and auto-kick mode.
# meta category: security
# meta version: 1.1.0
# meta author: DeathTerror
# requires: none

from __future__ import annotations

import asyncio
import html
import time
from typing import Any, List, Tuple

from telethon import functions
from telethon.errors import RPCError

from deathtg.loader import Module
from deathtg.command import command


class SessionGuardDtgMod(Module):
    """Telegram active sessions guard for DeathTG."""

    strings = {
        "name": "SessionGuardDtg",
        "title": "SessionGuardDtg",
        "description": "Protect Telegram account from unknown active sessions with inline whitelist and auto-kick.",
        "category": "security",
        "version": "1.1.0",
        "author": "DeathTerror",
        "commands": ".sg, .sglist, .sgmode, .sgscan, .sgkick, .sglog, .sgallowed, .sgallow, .sgdel",
        "usage": ".sg | .sglist | .sgmode on/off | .sgscan | .sgkick | .sglog here/off",
        "permissions": "owner",
    }

    DEFAULTS = {
        "enabled": False,
        "interval": 90,
        "allowed_hashes": [],
        "allowed_names": [],
        "log_chat_id": None,
        "last_seen": {},
    }

    def __init__(self) -> None:
        super().__init__()
        self._task: asyncio.Task | None = None
        self._last_sessions: list[dict] = []
        self._lock = asyncio.Lock()

    # -------------------- lifecycle --------------------

    async def client_ready(self, client, db=None):
        if not self.get("state"):
            self.set("state", dict(self.DEFAULTS))
        await self.ensure_current_allowed()
        self.start_task()

    async def on_unload(self):
        if self._task:
            self._task.cancel()
            self._task = None

    def start_task(self) -> None:
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self.watch_loop())

    async def watch_loop(self) -> None:
        while True:
            try:
                state = self.state()
                await asyncio.sleep(max(20, int(state.get("interval", 90))))
                if state.get("enabled"):
                    await self.scan_and_kick(notify=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(30)

    # -------------------- state/helpers --------------------

    def state(self) -> dict:
        state = self.get("state", None)
        if not isinstance(state, dict):
            state = dict(self.DEFAULTS)
        for key, value in self.DEFAULTS.items():
            state.setdefault(key, value.copy() if isinstance(value, (list, dict)) else value)
        self.set("state", state)
        return state

    def save_state(self, state: dict) -> None:
        self.set("state", state)

    @staticmethod
    def args_text(args) -> str:
        if isinstance(args, (list, tuple)):
            return " ".join(str(x) for x in args).strip()
        return str(args or "").strip()

    @staticmethod
    def esc(value: Any) -> str:
        return html.escape(str(value or ""), quote=False)

    @staticmethod
    def norm(value: Any) -> str:
        return " ".join(str(value or "").lower().strip().split())

    def session_name(self, session) -> str:
        parts = [
            getattr(session, "device_model", "") or "Unknown device",
            getattr(session, "platform", "") or "",
            getattr(session, "system_version", "") or "",
            getattr(session, "app_name", "") or "Telegram",
            getattr(session, "app_version", "") or "",
        ]
        return " ".join(str(x).strip() for x in parts if str(x).strip())

    def session_to_dict(self, session) -> dict:
        return {
            "hash": int(getattr(session, "hash", 0) or 0),
            "current": bool(getattr(session, "current", False)),
            "name": self.session_name(session),
            "device_model": getattr(session, "device_model", "") or "Unknown device",
            "platform": getattr(session, "platform", "") or "",
            "system_version": getattr(session, "system_version", "") or "",
            "app_name": getattr(session, "app_name", "") or "Telegram",
            "app_version": getattr(session, "app_version", "") or "",
            "ip": getattr(session, "ip", "") or "",
            "country": getattr(session, "country", "") or "",
            "region": getattr(session, "region", "") or "",
            "date_active": str(getattr(session, "date_active", "") or ""),
            "date_created": str(getattr(session, "date_created", "") or ""),
        }

    async def get_sessions(self) -> list[dict]:
        result = await self.client(functions.account.GetAuthorizationsRequest())
        sessions = [self.session_to_dict(x) for x in getattr(result, "authorizations", [])]
        sessions.sort(key=lambda x: (not x.get("current"), x.get("name", "")))
        self._last_sessions = sessions
        return sessions

    def allowed_hashes(self) -> set[int]:
        out = set()
        for x in self.state().get("allowed_hashes", []):
            try:
                out.add(int(x))
            except Exception:
                pass
        return out

    def is_allowed(self, item: dict) -> bool:
        if item.get("current"):
            return True
        state = self.state()
        allowed_names = {self.norm(x) for x in state.get("allowed_names", []) if str(x).strip()}
        return int(item.get("hash") or 0) in self.allowed_hashes() or self.norm(item.get("name")) in allowed_names

    async def ensure_current_allowed(self) -> None:
        try:
            sessions = await self.get_sessions()
        except Exception:
            return
        state = self.state()
        changed = False
        for item in sessions:
            if item.get("current"):
                h = int(item.get("hash") or 0)
                name = item.get("name") or ""
                if h and h not in state["allowed_hashes"]:
                    state["allowed_hashes"].append(h)
                    changed = True
                if name and name not in state["allowed_names"]:
                    state["allowed_names"].append(name)
                    changed = True
        if changed:
            self.save_state(state)

    def find_session(self, selector: str) -> dict | None:
        selector = selector.strip()
        if not selector:
            return None
        if selector.isdigit():
            index = int(selector) - 1
            if 0 <= index < len(self._last_sessions):
                return self._last_sessions[index]
            target = int(selector)
            for item in self._last_sessions:
                if int(item.get("hash") or 0) == target:
                    return item
        needle = self.norm(selector)
        for item in self._last_sessions:
            if needle in self.norm(item.get("name")):
                return item
        return None

    async def notify(self, text: str) -> None:
        state = self.state()
        chat_id = state.get("log_chat_id") or "me"
        try:
            await self.client.send_message(chat_id, text, parse_mode="html")
        except Exception:
            pass

    # -------------------- ui --------------------

    def status_text(self) -> str:
        state = self.state()
        return (
            "<b>SessionGuardDtg</b>\n\n"
            f"Auto-kick: <code>{'ON' if state.get('enabled') else 'OFF'}</code>\n"
            f"Interval: <code>{int(state.get('interval', 90))} sec</code>\n"
            f"Allowed hashes: <code>{len(state.get('allowed_hashes', []))}</code>\n"
            f"Allowed names: <code>{len(state.get('allowed_names', []))}</code>\n"
            f"Logs: <code>{self.esc(state.get('log_chat_id') or 'Saved Messages')}</code>\n\n"
            "<code>.sglist</code> - inline devices\n"
            "<code>.sgmode on/off</code> - auto-kick\n"
            "<code>.sgscan</code> - check only\n"
            "<code>.sgkick</code> - kick unknown now"
        )

    def sessions_text(self, sessions: list[dict]) -> str:
        state = self.state()
        unknown = len([x for x in sessions if not self.is_allowed(x)])
        lines = [
            "<b>SessionGuardDtg devices</b>",
            "",
            f"Auto-kick: <code>{'ON' if state.get('enabled') else 'OFF'}</code>",
            f"Sessions: <code>{len(sessions)}</code> | Unknown: <code>{unknown}</code>",
            "",
            "Tap device to allow/remove from whitelist.",
            "✅ = allowed, ⚠️ = unknown, 🟦 = current",
        ]
        return "\n".join(lines)

    def detail_text(self, item: dict) -> str:
        status = "CURRENT" if item.get("current") else ("ALLOWED" if self.is_allowed(item) else "UNKNOWN")
        geo = " ".join(x for x in [item.get("country"), item.get("region")] if x) or "unknown"
        return (
            "<b>Device details</b>\n\n"
            f"Status: <code>{status}</code>\n"
            f"Device: <code>{self.esc(item.get('name'))}</code>\n"
            f"Hash: <code>{item.get('hash')}</code>\n"
            f"IP: <code>{self.esc(item.get('ip'))}</code>\n"
            f"Geo: <code>{self.esc(geo)}</code>\n"
            f"Created: <code>{self.esc(item.get('date_created'))}</code>\n"
            f"Active: <code>{self.esc(item.get('date_active'))}</code>"
        )

    def session_buttons(self, sessions: list[dict]):
        rows = []
        for index, item in enumerate(sessions):
            if item.get("current"):
                icon = "🟦"
            elif self.is_allowed(item):
                icon = "✅"
            else:
                icon = "⚠️"
            name = str(item.get("device_model") or item.get("name") or "device")[:30]
            rows.append([
                {"text": f"{icon} {name}", "callback": self.toggle_callback, "args": (index,)},
                {"text": "Info", "callback": self.info_callback, "args": (index,)},
            ])
        mode_text = "Auto-kick: ON" if self.state().get("enabled") else "Auto-kick: OFF"
        rows.append([
            {"text": mode_text, "callback": self.mode_callback, "args": ()},
            {"text": "Refresh", "callback": self.refresh_callback, "args": ()},
        ])
        rows.append([
            {"text": "Kick unknown", "callback": self.kick_callback, "args": ()},
            {"text": "Close", "callback": self.close_callback, "args": ()},
        ])
        return self.inline_buttons(*rows)

    def detail_buttons(self, index: int):
        return self.inline_buttons(
            [{"text": "Toggle allowed", "callback": self.toggle_callback, "args": (index,)}],
            [{"text": "Back", "callback": self.refresh_callback, "args": ()}, {"text": "Close", "callback": self.close_callback, "args": ()}],
        )

    def allowed_text(self) -> str:
        state = self.state()
        lines = ["<b>Allowed devices</b>", "", "<b>Hashes:</b>"]
        lines.extend([f"- <code>{h}</code>" for h in state.get("allowed_hashes", [])] or ["- empty"])
        lines += ["", "<b>Names:</b>"]
        lines.extend([f"- <code>{self.esc(name)}</code>" for name in state.get("allowed_names", [])] or ["- empty"])
        return "\n".join(lines)

    # -------------------- allow/remove/kick --------------------

    def toggle_allowed(self, item: dict) -> bool:
        state = self.state()
        h = int(item.get("hash") or 0)
        name = item.get("name") or ""
        allowed = self.is_allowed(item)
        if allowed and not item.get("current"):
            state["allowed_hashes"] = [x for x in state.get("allowed_hashes", []) if int(x) != h]
            state["allowed_names"] = [x for x in state.get("allowed_names", []) if self.norm(x) != self.norm(name)]
            self.save_state(state)
            return False
        if h and h not in state["allowed_hashes"]:
            state["allowed_hashes"].append(h)
        if name and name not in state["allowed_names"]:
            state["allowed_names"].append(name)
        self.save_state(state)
        return True

    async def scan_and_kick(self, notify: bool = False) -> Tuple[list[dict], list[dict]]:
        async with self._lock:
            sessions = await self.get_sessions()
            unknown = [x for x in sessions if not self.is_allowed(x)]
            kicked = []
            state = self.state()
            last_seen = state.get("last_seen", {}) if isinstance(state.get("last_seen"), dict) else {}

            for item in unknown:
                h = int(item.get("hash") or 0)
                key = str(h)
                if notify and key not in last_seen:
                    await self.notify(
                        "<b>SessionGuard alert</b>\nUnknown Telegram session detected.\n\n"
                        f"Device: <code>{self.esc(item.get('name'))}</code>\n"
                        f"Hash: <code>{h}</code>\n"
                        f"IP: <code>{self.esc(item.get('ip'))}</code>"
                    )
                last_seen[key] = int(time.time())

                if state.get("enabled") and h:
                    try:
                        await self.client(functions.account.ResetAuthorizationRequest(hash=h))
                        kicked.append(item)
                        await self.notify(
                            "<b>SessionGuard kicked unknown session</b>\n"
                            f"Device: <code>{self.esc(item.get('name'))}</code>\nHash: <code>{h}</code>"
                        )
                    except (RPCError, Exception) as exc:
                        await self.notify(f"<b>SessionGuard kick failed:</b> <code>{self.esc(exc)}</code>")

            state["last_seen"] = last_seen
            self.save_state(state)
            return unknown, kicked

    # -------------------- commands --------------------

    @command("sg", description="Show SessionGuard status", usage=".sg")
    async def sg_cmd(self, event, args):
        self.start_task()
        await event.edit(self.status_text(), parse_mode="html")

    @command("sglist", description="Inline active Telegram sessions", usage=".sglist")
    async def sglist_cmd(self, event, args):
        try:
            sessions = await self.get_sessions()
            await self.inline_send(
                event,
                self.sessions_text(sessions),
                reply_markup=self.session_buttons(sessions),
                parse_mode="html",
                link_preview=False,
                ttl=3600,
            )
        except Exception as exc:
            await event.edit(f"<b>Failed to get sessions:</b> <code>{self.esc(exc)}</code>", parse_mode="html")

    @command("sgallow", description="Allow session by number/hash/name", usage=".sgallow 1 | .sgallow current | .sgallow name")
    async def sgallow_cmd(self, event, args):
        selector = self.args_text(args)
        if not self._last_sessions:
            await self.get_sessions()
        if not selector or selector.lower() == "current":
            await self.ensure_current_allowed()
            await event.edit("<b>Current session allowed.</b>", parse_mode="html")
            return
        item = self.find_session(selector)
        if item:
            allowed = self.toggle_allowed(item)
            await event.edit(f"<b>Device allowed:</b> <code>{'YES' if allowed else 'NO'}</code>", parse_mode="html")
            return
        state = self.state()
        if selector not in state["allowed_names"]:
            state["allowed_names"].append(selector)
            self.save_state(state)
        await event.edit(f"<b>Allowed name added:</b> <code>{self.esc(selector)}</code>", parse_mode="html")

    @command("sgdel", description="Remove allowed device by hash/name", usage=".sgdel hash_or_name")
    async def sgdel_cmd(self, event, args):
        selector = self.args_text(args)
        if not selector:
            await event.edit("<b>Usage:</b> <code>.sgdel hash_or_name</code>", parse_mode="html")
            return
        state = self.state()
        before = len(state["allowed_hashes"]) + len(state["allowed_names"])
        if selector.isdigit():
            target = int(selector)
            state["allowed_hashes"] = [h for h in state["allowed_hashes"] if int(h) != target]
        needle = self.norm(selector)
        state["allowed_names"] = [name for name in state["allowed_names"] if needle not in self.norm(name)]
        self.save_state(state)
        after = len(state["allowed_hashes"]) + len(state["allowed_names"])
        await event.edit(f"<b>Removed:</b> <code>{before - after}</code>", parse_mode="html")

    @command("sgmode", description="Enable or disable auto-kick", usage=".sgmode on/off")
    async def sgmode_cmd(self, event, args):
        value = self.args_text(args).lower()
        if value not in {"on", "off"}:
            await event.edit("<b>Usage:</b> <code>.sgmode on</code> or <code>.sgmode off</code>", parse_mode="html")
            return
        await self.ensure_current_allowed()
        state = self.state()
        state["enabled"] = value == "on"
        self.save_state(state)
        self.start_task()
        await event.edit(f"<b>SessionGuard auto-kick:</b> <code>{value.upper()}</code>", parse_mode="html")

    @command("sgscan", description="Scan Telegram sessions without kicking", usage=".sgscan")
    async def sgscan_cmd(self, event, args):
        sessions = await self.get_sessions()
        unknown = [x for x in sessions if not self.is_allowed(x)]
        await event.edit(f"<b>Scan complete.</b> Unknown sessions: <code>{len(unknown)}</code>", parse_mode="html")

    @command("sgkick", description="Kick unauthorized Telegram sessions now", usage=".sgkick")
    async def sgkick_cmd(self, event, args):
        unknown, kicked = await self.scan_and_kick(notify=True)
        await event.edit(
            f"<b>Manual kick complete.</b>\nUnknown: <code>{len(unknown)}</code>\nKicked: <code>{len(kicked)}</code>",
            parse_mode="html",
        )

    @command("sglog", description="Set alert chat", usage=".sglog here/off")
    async def sglog_cmd(self, event, args):
        value = self.args_text(args).lower()
        state = self.state()
        if value == "off":
            state["log_chat_id"] = None
            self.save_state(state)
            await event.edit("<b>Logs:</b> <code>Saved Messages</code>", parse_mode="html")
            return
        if value == "here" or not value:
            state["log_chat_id"] = int(event.chat_id)
            self.save_state(state)
            await event.edit("<b>Logs:</b> <code>current chat</code>", parse_mode="html")
            return
        await event.edit("<b>Usage:</b> <code>.sglog here</code> or <code>.sglog off</code>", parse_mode="html")

    @command("sgallowed", description="Show allowed devices", usage=".sgallowed")
    async def sgallowed_cmd(self, event, args):
        await event.edit(self.allowed_text(), parse_mode="html")

    # -------------------- callbacks --------------------

    async def refresh_callback(self, call):
        sessions = await self.get_sessions()
        await call.edit(
            self.sessions_text(sessions),
            reply_markup=self.session_buttons(sessions),
            parse_mode="html",
            link_preview=False,
        )

    async def info_callback(self, call, index: int):
        if not self._last_sessions:
            await self.get_sessions()
        if index < 0 or index >= len(self._last_sessions):
            await call.edit("Session not found.", reply_markup=None)
            return
        await call.edit(
            self.detail_text(self._last_sessions[index]),
            reply_markup=self.detail_buttons(index),
            parse_mode="html",
            link_preview=False,
        )

    async def toggle_callback(self, call, index: int):
        if not self._last_sessions:
            await self.get_sessions()
        if index < 0 or index >= len(self._last_sessions):
            await call.edit("Session not found.", reply_markup=None)
            return
        item = self._last_sessions[index]
        if item.get("current"):
            self.toggle_allowed(item)
        else:
            self.toggle_allowed(item)
        sessions = await self.get_sessions()
        await call.edit(
            self.sessions_text(sessions),
            reply_markup=self.session_buttons(sessions),
            parse_mode="html",
            link_preview=False,
        )

    async def mode_callback(self, call):
        state = self.state()
        state["enabled"] = not bool(state.get("enabled"))
        self.save_state(state)
        self.start_task()
        sessions = await self.get_sessions()
        await call.edit(
            self.sessions_text(sessions),
            reply_markup=self.session_buttons(sessions),
            parse_mode="html",
            link_preview=False,
        )

    async def kick_callback(self, call):
        unknown, kicked = await self.scan_and_kick(notify=True)
        sessions = await self.get_sessions()
        await call.edit(
            f"<b>Kick complete.</b> Unknown: <code>{len(unknown)}</code> | Kicked: <code>{len(kicked)}</code>\n\n" + self.sessions_text(sessions),
            reply_markup=self.session_buttons(sessions),
            parse_mode="html",
            link_preview=False,
        )

    async def close_callback(self, call):
        await call.edit("Closed.", reply_markup=None)
