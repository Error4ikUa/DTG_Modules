# meta name: NoteDtg
# meta description: Personal notes for DeathTG.
# meta category: productivity
# meta version: 1.0.0
# meta author: DeathTerror

from __future__ import annotations

import html
import json
import logging
import os
import time
from pathlib import Path

from deathtg.loader import Module
from deathtg.command import command

logger = logging.getLogger("deathtg.modules.NoteDtg")
MAX_NOTES = 500


class NoteDtgMod(Module):
    strings = {
        "name": "NoteDtg",
        "title": "NoteDtg",
        "description": "Personal notes with inline menu.",
        "category": "productivity",
        "version": "1.0.0",
        "author": "DeathTerror",
        "commands": ".notecr, .note",
        "usage": ".notecr name text | .note",
        "permissions": "owner",
    }

    def __init__(self) -> None:
        super().__init__()
        self.config_path = Path(__file__).with_suffix(".json")

    def load_notes(self) -> dict:
        try:
            if self.config_path.exists():
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Unable to load notes from %s: %s", self.config_path, exc)
            return {}
        return {}

    def save_notes(self, notes: dict) -> bool:
        temporary = self.config_path.with_suffix(f"{self.config_path.suffix}.tmp")
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.config_path)
            return True
        except OSError as exc:
            logger.error("Unable to save notes to %s: %s", self.config_path, exc)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                logger.debug("Unable to remove temporary notes file", exc_info=True)
            return False

    @staticmethod
    def args_text(args) -> str:
        return " ".join(str(x) for x in args).strip() if isinstance(args, (list, tuple)) else str(args or "").strip()

    @staticmethod
    def esc(text) -> str:
        return html.escape(str(text or ""), quote=False)

    @staticmethod
    def clean_name(name: str) -> str:
        return " ".join(str(name or "").strip().split())[:64]

    @staticmethod
    def short(text: str, limit: int = 42) -> str:
        text = " ".join(str(text or "").split())
        return text if len(text) <= limit else text[: limit - 3] + "..."

    def note_key(self, name: str) -> str:
        return self.clean_name(name).lower()

    def menu_text(self, notes: dict) -> str:
        if not notes:
            return "<b>NoteDtg</b>\n\nNo notes yet.\nUse: <code>.notecr name text</code>"
        lines = ["<b>NoteDtg</b>", "", f"Notes: <code>{len(notes)}</code>", ""]
        for i, item in enumerate(notes.values(), 1):
            lines.append(f"<b>{i}.</b> <code>{self.esc(item.get('name', 'note'))}</code> - {self.esc(self.short(item.get('text', '')))}")
        lines += ["", "Choose a note below."]
        return "\n".join(lines)

    def menu_buttons(self, notes: dict):
        rows = []
        for key, item in notes.items():
            rows.append([{"text": self.short(item.get("name", key), 32), "callback": self.open_callback, "args": (key,)}])
        rows.append([{"text": "Close", "callback": self.close_callback, "args": ()}])
        return self.inline_buttons(*rows)

    def note_text(self, item: dict) -> str:
        updated = int(item.get("updated") or item.get("created") or 0)
        lines = [f"<b>Note:</b> <code>{self.esc(item.get('name', 'note'))}</code>", "", self.esc(item.get("text", ""))]
        if updated:
            lines += ["", f"<i>Updated:</i> <code>{time.strftime('%Y-%m-%d %H:%M', time.localtime(updated))}</code>"]
        return "\n".join(lines)

    def note_buttons(self, key: str):
        return self.inline_buttons(
            [{"text": "Back", "callback": self.back_callback, "args": ()}],
            [{"text": "Remove", "callback": self.remove_confirm_callback, "args": (key,)}],
            [{"text": "Close", "callback": self.close_callback, "args": ()}],
        )

    def remove_buttons(self, key: str):
        return self.inline_buttons(
            [{"text": "Yes", "callback": self.remove_callback, "args": (key,)}],
            [{"text": "Back", "callback": self.open_callback, "args": (key,)}],
            [{"text": "Close", "callback": self.close_callback, "args": ()}],
        )

    @command("notecr", description="Create or update a note", usage=".notecr name text")
    async def notecr_cmd(self, event, args):
        raw = self.args_text(args)
        if not raw or " " not in raw:
            await event.edit("<b>Usage:</b> <code>.notecr name text</code>", parse_mode="html")
            return
        name, text = raw.split(maxsplit=1)
        name = self.clean_name(name)
        text = text.strip()
        if not name or not text:
            await event.edit("<b>Usage:</b> <code>.notecr name text</code>", parse_mode="html")
            return
        notes = self.load_notes()
        key = self.note_key(name)
        if key not in notes and len(notes) >= MAX_NOTES:
            await event.edit(f"<b>Note limit reached:</b> <code>{MAX_NOTES}</code>", parse_mode="html")
            return
        old = notes.get(key, {})
        notes[key] = {"name": name, "text": text[:6000], "created": int(old.get("created") or time.time()), "updated": int(time.time())}
        if not self.save_notes(notes):
            await event.edit("<b>Could not save the note.</b> Check DeathTG logs.", parse_mode="html")
            return
        await event.edit(f"<b>Saved note:</b> <code>{self.esc(name)}</code>", parse_mode="html")

    @command("note", description="Open notes menu", usage=".note")
    async def note_cmd(self, event, args):
        notes = self.load_notes()
        await self.inline_send(event, self.menu_text(notes), reply_markup=self.menu_buttons(notes), parse_mode="html", link_preview=False, ttl=3600)

    async def open_callback(self, call, key: str):
        notes = self.load_notes()
        item = notes.get(key)
        if not item:
            await call.edit("Note not found.", reply_markup=None)
            return
        await call.edit(self.note_text(item), reply_markup=self.note_buttons(key), parse_mode="html", link_preview=False)

    async def back_callback(self, call):
        notes = self.load_notes()
        await call.edit(self.menu_text(notes), reply_markup=self.menu_buttons(notes), parse_mode="html", link_preview=False)

    async def remove_confirm_callback(self, call, key: str):
        notes = self.load_notes()
        item = notes.get(key)
        if not item:
            await call.edit("Note not found.", reply_markup=None)
            return
        await call.edit(f"<b>Remove note?</b> <code>{self.esc(item.get('name', key))}</code>", reply_markup=self.remove_buttons(key), parse_mode="html", link_preview=False)

    async def remove_callback(self, call, key: str):
        notes = self.load_notes()
        notes.pop(key, None)
        if not self.save_notes(notes):
            await call.edit("<b>Could not update notes.</b> Check DeathTG logs.", reply_markup=None, parse_mode="html")
            return
        await call.edit("<b>Removed.</b>\n\n" + self.menu_text(notes), reply_markup=self.menu_buttons(notes), parse_mode="html", link_preview=False)

    async def close_callback(self, call):
        await call.edit("Closed.", reply_markup=None)
