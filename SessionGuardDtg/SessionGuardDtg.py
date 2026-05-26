# meta developer: @DeathTerror
# meta name: SessionGuardDtg
# meta description: Inline Telegram session guard with allow/kick policy per device.
# meta category: security
# meta version: 2.0.0
# meta author: DeathTerror
# requires: none

from __future__ import annotations

import asyncio
import html
import time
from typing import Any

from telethon import functions
from telethon.errors import RPCError

from deathtg.command import command
from deathtg.loader import Module


class SessionGuardDtgMod(Module):
    """Inline Telegram sessions guard for DeathTG."""

    strings = {
        "name": "SessionGuardDtg",
        "title": "SessionGuardDtg",
        "description": "Inline session security with per-device allow/kick policy.",
        "category": "security",
        "version": "2.0.0",
        "author": "DeathTerror",
        "commands": ".secconfig, .secstart, .secoff",
        "usage": ".secconfig | .secstart | .secoff",
        "permissions": "owner",
    }

    DEFAULTS = {
        "enabled": False,
        "interval": 90,
        "device_policy": {},
        "log_chat_id": None,
        "last_seen": {},
    }

    def __init__(self) -> None:
        super().__init__()
        self._task: asyncio.Task | None = None
        self._last_sessions: list[dict] = []
        self._lock = asyncio.Lock()

    async def client_ready(self, client, db=None):
        if not self.get("state"):
            self.set("state", dict(self.DEFAULTS))
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
                await asyncio.sleep(max(20, int(state.get("interval", 90) or 90)))
                if state.get("enabled"):
                    await self.scan_and_kick(notify=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(30)

    def state(self) -> dict:
        state = self.get("state", None)
        if not isinstance(state, dict):
            state = dict(self.DEFAULTS)
        for key, value in self.DEFAULTS.items():
            state.setdefault(key, value.copy() if isinstance(value, (list, dict)) else value)

        policy = state.get("device_policy")
        if not isinstance(policy, dict):
            state["device_policy"] = {}
        else:
            cleaned = {}
            for key, value in policy.items():
                mode = str(value).lower().strip()
                if mode in {"allow", "kick"}:
                    cleaned[str(key)] = mode
            state["device_policy"] = cleaned

        last_seen = state.get("last_seen")
        if not isinstance(last_seen, dict):
            state["last_seen"] = {}

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
    def short(value: Any, limit: int = 34) -> str:
        text = " ".join(str(value or "").split())
        return text if len(text) <= limit else text[: limit - 3] + "..."

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
        sessions.sort(key=lambda x: (not x.get("current"), self.norm(x.get("name"))))
        self._last_sessions = sessions
        return sessions

    def policy_map(self) -> dict[str, str]:
        state = self.state()
        out = {}
        for key, value in (state.get("device_policy") or {}).items():
            mode = str(value).lower().strip()
            if mode in {"allow", "kick"}:
                out[str(key)] = mode
        return out

    def policy_for(self, item: dict) -> str | None:
        if item.get("current"):
            return "allow"
        h = int(item.get("hash") or 0)
        if not h:
            return None
        return self.policy_map().get(str(h))

    def is_allowed(self, item: dict) -> bool:
        return self.policy_for(item) == "allow"

    def should_kick(self, item: dict) -> bool:
        if item.get("current"):
            return False
        return self.policy_for(item) != "allow"

    def set_policy(self, session_hash: int, mode: str | None) -> None:
        state = self.state()
        policy = dict(state.get("device_policy") or {})
        key = str(int(session_hash))
        if mode in {"allow", "kick"}:
            policy[key] = mode
        else:
            policy.pop(key, None)
        state["device_policy"] = policy
        self.save_state(state)

    def toggle_policy(self, item: dict) -> str:
        if item.get("current"):
            return "allow"
        h = int(item.get("hash") or 0)
        if not h:
            return "allow"
        current = self.policy_for(item)
        if current is None:
            new_mode = "allow"
        elif current == "allow":
            new_mode = "kick"
        else:
            new_mode = "allow"
        self.set_policy(h, new_mode)
        return new_mode

    async def notify(self, text: str) -> None:
        state = self.state()
        chat_id = state.get("log_chat_id") or "me"
        try:
            await self.client.send_message(chat_id, text, parse_mode="html")
        except Exception:
            pass

    def policy_icon(self, item: dict) -> str:
        if item.get("current"):
            return "🛡️"
        mode = self.policy_for(item)
        if mode == "allow":
            return "✅"
        if mode == "kick":
            return "❌"
        return "❔"

    def sessions_text(self, sessions: list[dict]) -> str:
        state = self.state()
        unknown = len([x for x in sessions if not x.get("current") and self.policy_for(x) is None])
        lines = [
            "<b>SessionGuardDtg • SEC CONFIG</b>",
            "",
            f"Mode: <code>{'SEC START' if state.get('enabled') else 'SEC OFF'}</code>",
            f"Sessions: <code>{len(sessions)}</code> | Unknown: <code>{unknown}</code>",
            "",
            "<blockquote>❔ not configured\n✅ allow (do not kick)\n❌ always kick\n🛡️ current session</blockquote>",
            "Tap a device button to change status.",
        ]
        return "\n".join(lines)

    def session_button_text(self, item: dict) -> str:
        icon = self.policy_icon(item)
        name = item.get("device_model") or item.get("name") or "device"
        if item.get("current"):
            name = f"{name} (current)"
        return f"{icon} {self.short(name, 38)}"

    def session_buttons(self, sessions: list[dict]):
        rows = []
        for index, item in enumerate(sessions):
            rows.append([
                {"text": self.session_button_text(item), "callback": self.toggle_callback, "args": (index,)},
            ])

        mode_btn = "⏹ SEC OFF" if self.state().get("enabled") else "▶️ SEC START"
        rows.append([
            {"text": mode_btn, "callback": self.mode_callback, "args": ()},
            {"text": "🔄 Refresh", "callback": self.refresh_callback, "args": ()},
        ])
        rows.append([
            {"text": "🧹 Kick now", "callback": self.kick_callback, "args": ()},
            {"text": "Close", "callback": self.close_callback, "args": ()},
        ])
        return self.inline_buttons(*rows)

    async def scan_and_kick(self, notify: bool = False, force_kick: bool = False) -> tuple[list[dict], list[dict]]:
        async with self._lock:
            sessions = await self.get_sessions()
            targets = [x for x in sessions if self.should_kick(x)]
            kicked = []

            state = self.state()
            last_seen = state.get("last_seen", {}) if isinstance(state.get("last_seen"), dict) else {}

            for item in targets:
                h = int(item.get("hash") or 0)
                if not h:
                    continue

                key = str(h)
                if notify and key not in last_seen:
                    await self.notify(
                        "<b>SessionGuard alert</b>\n"
                        "Device is not allowed by config.\n\n"
                        f"Device: <code>{self.esc(item.get('name'))}</code>\n"
                        f"Hash: <code>{h}</code>\n"
                        f"IP: <code>{self.esc(item.get('ip'))}</code>"
                    )
                last_seen[key] = int(time.time())

                if state.get("enabled") or force_kick:
                    try:
                        await self.client(functions.account.ResetAuthorizationRequest(hash=h))
                        kicked.append(item)
                        await self.notify(
                            "<b>SessionGuard kicked session</b>\n"
                            f"Device: <code>{self.esc(item.get('name'))}</code>\n"
                            f"Hash: <code>{h}</code>"
                        )
                    except (RPCError, Exception) as exc:
                        await self.notify(f"<b>SessionGuard kick failed:</b> <code>{self.esc(exc)}</code>")

            state["last_seen"] = last_seen
            self.save_state(state)
            return targets, kicked

    @command("secconfig", description="Open inline session config", usage=".secconfig")
    async def secconfig_cmd(self, event, args):
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
            await event.edit(f"<b>Failed to load sessions:</b> <code>{self.esc(exc)}</code>", parse_mode="html")

    @command("secstart", description="Enable session guard and kick not-allowed sessions", usage=".secstart")
    async def secstart_cmd(self, event, args):
        state = self.state()
        state["enabled"] = True
        self.save_state(state)
        self.start_task()
        targets, kicked = await self.scan_and_kick(notify=True)
        await event.edit(
            "<b>SEC START enabled.</b>\n"
            f"Targets: <code>{len(targets)}</code>\n"
            f"Kicked now: <code>{len(kicked)}</code>",
            parse_mode="html",
        )

    @command("secoff", description="Disable session guard", usage=".secoff")
    async def secoff_cmd(self, event, args):
        state = self.state()
        state["enabled"] = False
        self.save_state(state)
        await event.edit("<b>SEC OFF set.</b> Auto-kick stopped.", parse_mode="html")

    async def refresh_callback(self, call):
        sessions = await self.get_sessions()
        await call.edit(
            self.sessions_text(sessions),
            reply_markup=self.session_buttons(sessions),
            parse_mode="html",
            link_preview=False,
        )

    async def toggle_callback(self, call, index: int):
        if not self._last_sessions:
            await self.get_sessions()
        if index < 0 or index >= len(self._last_sessions):
            await call.edit("Session not found.", reply_markup=None)
            return

        self.toggle_policy(self._last_sessions[index])
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

        if state["enabled"]:
            await self.scan_and_kick(notify=True)

        sessions = await self.get_sessions()
        await call.edit(
            self.sessions_text(sessions),
            reply_markup=self.session_buttons(sessions),
            parse_mode="html",
            link_preview=False,
        )

    async def kick_callback(self, call):
        targets, kicked = await self.scan_and_kick(notify=True, force_kick=True)
        sessions = await self.get_sessions()
        await call.edit(
            f"<b>Manual kick done.</b> Targets: <code>{len(targets)}</code> | Kicked: <code>{len(kicked)}</code>\n\n"
            + self.sessions_text(sessions),
            reply_markup=self.session_buttons(sessions),
            parse_mode="html",
            link_preview=False,
        )

    async def close_callback(self, call):
        await call.edit("Closed.", reply_markup=None)
