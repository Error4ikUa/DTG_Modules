# meta developer: @DeathTerror
# meta name: MusicSearchDtg
# meta description: SoundCloud-only music search with DeathTG inline buttons.
# meta category: media
# meta version: 2.0.0
# meta author: DeathTerror
# requires: aiohttp

from __future__ import annotations

import html
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import aiohttp

from deathtg.loader import Module
from deathtg.command import command


class MusicSearchDtgMod(Module):
    strings = {
        "name": "MusicSearchDtg",
        "title": "MusicSearchDtg",
        "description": "SoundCloud-only track search and downloadable audio sender.",
        "category": "media",
        "version": "2.0.0",
        "author": "DeathTerror",
        "commands": ".tr",
        "usage": ".tr query | .tr soundcloud token | .tr status",
        "permissions": "owner",
    }

    DEFAULT_CFG = {
        "soundcloud_token": "",
        "max_download_mb": 80,
    }

    def __init__(self) -> None:
        super().__init__()
        self.cache: dict[str, dict] = {}
        self.config_path = Path(__file__).with_suffix(".json")

    # -------------------- config --------------------

    def load_cfg(self) -> dict:
        cfg = self.DEFAULT_CFG.copy()
        try:
            if self.config_path.exists():
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cfg.update(data)
        except Exception:
            pass
        cfg["soundcloud_token"] = cfg.get("soundcloud_token") or os.getenv("SOUNDCLOUD_TOKEN", "")
        return cfg

    def save_cfg(self, cfg: dict) -> None:
        try:
            self.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # -------------------- helpers --------------------

    @staticmethod
    def args_text(args) -> str:
        if isinstance(args, (list, tuple)):
            return " ".join(str(item) for item in args).strip()
        return str(args or "").strip()

    @staticmethod
    def esc(text) -> str:
        return html.escape(str(text or ""), quote=False)

    @staticmethod
    def is_url(value: str) -> bool:
        return bool(re.match(r"https?://", value or "", re.I))

    @staticmethod
    def mask(value: str) -> str:
        if not value:
            return "not set"
        if len(value) <= 10:
            return "hidden"
        return f"{value[:5]}...{value[-4:]}"

    @staticmethod
    def safe_filename(text: str) -> str:
        text = re.sub(r"[^a-zA-Zа-яА-ЯёЁіїєґІЇЄҐ0-9 ._-]+", "", text or "track").strip()
        text = re.sub(r"\s+", " ", text)
        return (text or "track")[:90]

    def headers(self) -> dict | None:
        token = self.load_cfg().get("soundcloud_token", "").strip()
        return {"Authorization": f"OAuth {token}"} if token else None

    # -------------------- UI --------------------

    def result_text(self, query: str, results: list[dict]) -> str:
        lines = [f"<b>SoundCloud search:</b> <code>{self.esc(query)}</code>", ""]
        for index, item in enumerate(results, 1):
            title = self.esc(item.get("title") or "Unknown")
            artist = self.esc(item.get("artist") or "Unknown")
            mark = "FULL" if item.get("full_url") else "NO DOWNLOAD"
            lines.append(f"<b>{index}.</b> <b>{title}</b> - <code>{artist}</code> <i>({mark})</i>")
        lines += ["", "Choose a track below."]
        return "\n".join(lines)

    def result_buttons(self, key: str, results: list[dict]):
        rows = []
        for index, item in enumerate(results, 1):
            title = (item.get("title") or "Unknown")[:30]
            artist = (item.get("artist") or "Unknown")[:18]
            mark = "FULL" if item.get("full_url") else "INFO"
            rows.append([
                {
                    "text": f"{index}. {title} - {artist} [{mark}]",
                    "callback": self.select_callback,
                    "args": (key, index - 1),
                }
            ])
        rows.append([{"text": "Close", "callback": self.close_callback, "args": ()}])
        return self.inline_buttons(*rows)

    def card_text(self, item: dict) -> str:
        title = self.esc(item.get("title") or "Unknown")
        artist = self.esc(item.get("artist") or "Unknown")
        url = self.esc(item.get("url") or "")
        lines = [f"<b>{title}</b>", f"Artist: <code>{artist}</code>", "Source: <code>SoundCloud</code>"]
        if item.get("full_url"):
            lines.append("Audio: <code>official download available</code>")
        else:
            lines += ["", "This SoundCloud track is not marked as downloadable, so full audio is not available through the official API."]
        if url:
            lines += ["", f"<code>{url}</code>"]
        return "\n".join(lines)

    def card_buttons(self, item: dict, key: str | None = None):
        rows = []
        if item.get("url"):
            rows.append([{"text": "Open", "url": item["url"]}])
        if key:
            rows.append([{"text": "Back", "callback": self.back_callback, "args": (key,)}])
        rows.append([{"text": "Close", "callback": self.close_callback, "args": ()}])
        return self.inline_buttons(*rows)

    # -------------------- HTTP / SoundCloud --------------------

    async def get_json(self, url: str, headers: dict | None = None):
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers or {}) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = {}
                if response.status >= 400:
                    return None
                return data

    def make_item(self, raw: dict) -> dict:
        user = raw.get("user", {}) or {}
        downloadable = bool(raw.get("downloadable"))
        download_url = raw.get("download_url") or ""
        return {
            "provider": "soundcloud",
            "title": raw.get("title") or "Unknown",
            "artist": user.get("username") or raw.get("publisher_metadata", {}).get("artist") or "Unknown",
            "url": raw.get("permalink_url") or raw.get("uri") or "",
            "full_url": download_url if downloadable and download_url else "",
            "download_headers": self.headers() or {},
        }

    async def search_soundcloud(self, query: str, limit: int = 3) -> list[dict]:
        headers = self.headers()
        if not headers:
            return []
        data = await self.get_json(f"https://api.soundcloud.com/tracks?q={quote(query)}&limit={int(limit)}", headers=headers)
        if not data:
            return []
        if isinstance(data, dict):
            data = data.get("collection", [])
        return [self.make_item(item) for item in data if isinstance(item, dict)][:limit]

    async def resolve_soundcloud(self, url: str) -> dict | None:
        headers = self.headers()
        if not headers:
            return None
        data = await self.get_json(f"https://api.soundcloud.com/resolve?url={quote(url)}", headers=headers)
        if isinstance(data, dict) and data.get("kind") == "track":
            return self.make_item(data)
        return None

    # -------------------- download --------------------

    async def download_full(self, item: dict) -> str | None:
        url = item.get("full_url") or ""
        if not url:
            return None
        suffix = Path(urlparse(url).path).suffix or ".mp3"
        if len(suffix) > 8:
            suffix = ".mp3"
        filename = self.safe_filename(f"{item.get('artist', 'Unknown')} - {item.get('title', 'Track')}") + suffix
        path = str(Path(tempfile.gettempdir()) / filename)
        max_bytes = int(self.load_cfg().get("max_download_mb", 80)) * 1024 * 1024
        try:
            timeout = aiohttp.ClientTimeout(total=180)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=item.get("download_headers") or {}, allow_redirects=True) as response:
                    if response.status >= 400:
                        return None
                    total = 0
                    with open(path, "wb") as file:
                        async for chunk in response.content.iter_chunked(65536):
                            total += len(chunk)
                            if total > max_bytes:
                                return None
                            file.write(chunk)
            return path if os.path.getsize(path) > 0 else None
        except Exception:
            return None

    async def send_track(self, client, chat_id, item: dict) -> bool:
        path = await self.download_full(item)
        if not path:
            return False
        try:
            await client.send_file(chat_id, path, caption="", force_document=False)
            return True
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    # -------------------- command --------------------

    @command("tr", description="Search SoundCloud music", usage=".tr query | .tr soundcloud token | .tr status")
    async def tr_cmd(self, event, args):
        text = self.args_text(args)
        if not text:
            await event.edit(
                "<b>Usage:</b> <code>.tr track name</code>\n"
                "<b>Link:</b> <code>.tr https://soundcloud.com/...</code>\n"
                "<b>Token:</b> <code>.tr soundcloud token</code>",
                parse_mode="html",
            )
            return

        low = text.lower().strip()
        if low == "status":
            await self.status(event)
            return
        if low.startswith("soundcloud "):
            await self.set_soundcloud(event, text[len("soundcloud "):].strip())
            return

        if not self.headers():
            await event.edit("<b>SoundCloud token is not set.</b>\nUse: <code>.tr soundcloud token</code>", parse_mode="html")
            return

        await event.edit("<b>Searching SoundCloud...</b>", parse_mode="html")

        if self.is_url(text):
            item = await self.resolve_soundcloud(text)
            if not item:
                await event.edit("<b>SoundCloud track was not found.</b>", parse_mode="html")
                return
            if await self.send_track(event.client, event.chat_id, item):
                try:
                    await event.delete()
                except Exception:
                    pass
                return
            await self.inline_send(event, self.card_text(item), reply_markup=self.card_buttons(item), parse_mode="html", link_preview=False, ttl=3600)
            return

        results = await self.search_soundcloud(text, 3)
        if not results:
            await event.edit("<b>Nothing found on SoundCloud.</b>", parse_mode="html")
            return

        key = f"{int(time.time())}_{id(event)}"
        self.cache[key] = {"query": text, "results": results, "time": time.time()}
        await self.inline_send(
            event,
            self.result_text(text, results),
            reply_markup=self.result_buttons(key, results),
            parse_mode="html",
            link_preview=False,
            ttl=3600,
        )

    async def status(self, event) -> None:
        cfg = self.load_cfg()
        text = (
            "<b>MusicSearchDtg</b>\n"
            "Mode: <code>SoundCloud only</code>\n"
            "Command: <code>.tr</code>\n"
            "Full audio: <code>official downloadable tracks only</code>\n"
            f"Max download: <code>{int(cfg.get('max_download_mb', 80))} MB</code>\n"
            f"SoundCloud token: <code>{self.esc(self.mask(cfg.get('soundcloud_token', '')))}</code>"
        )
        await event.edit(text, parse_mode="html")

    async def set_soundcloud(self, event, token: str) -> None:
        if not token:
            await event.edit("<b>Usage:</b> <code>.tr soundcloud token</code>", parse_mode="html")
            return
        cfg = self.load_cfg()
        cfg["soundcloud_token"] = token.strip()
        self.save_cfg(cfg)
        await event.edit("<b>SoundCloud token saved.</b>", parse_mode="html")

    # -------------------- callbacks --------------------

    async def select_callback(self, call, key: str, index: int):
        pack = self.cache.get(key)
        if not pack or time.time() - pack.get("time", 0) > 3600:
            await call.edit("Search expired.", reply_markup=None)
            return

        results = pack.get("results") or []
        if index < 0 or index >= len(results):
            await call.edit("Track was not found.", reply_markup=None)
            return

        item = results[index]
        client = getattr(call, "original_client", None)
        chat_id = getattr(call, "original_chat_id", None)

        if client and chat_id:
            ok = await self.send_track(client, chat_id, item)
            if ok:
                await call.edit("Done.", reply_markup=None)
                return

        await call.edit(self.card_text(item), reply_markup=self.card_buttons(item, key), parse_mode="html", link_preview=False)

    async def back_callback(self, call, key: str):
        pack = self.cache.get(key)
        if not pack:
            await call.edit("Search expired.", reply_markup=None)
            return
        await call.edit(
            self.result_text(pack.get("query", ""), pack.get("results", [])),
            reply_markup=self.result_buttons(key, pack.get("results", [])),
            parse_mode="html",
            link_preview=False,
        )

    async def close_callback(self, call):
        await call.edit("Closed.", reply_markup=None)
