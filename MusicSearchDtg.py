# meta developer: @DeathTerror
# meta name: MusicSearchDtg
# requires: aiohttp

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import re
import tempfile
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import aiohttp

from deathtg.loader import Module
from deathtg.command import command


class MusicSearchDtgMod(Module):
    """Music search for DeathTG with native inline buttons."""

    strings = {
        "name": "MusicSearchDtg",
        "description": "Search tracks in Apple/iTunes, Spotify and SoundCloud. Use .tr only.",
    }

    DEFAULT_CFG = {
        "spotify_client_id": "",
        "spotify_client_secret": "",
        "soundcloud_token": "",
    }

    def __init__(self) -> None:
        super().__init__()
        self.cache: dict[str, dict] = {}
        self.spotify_cache = {"token": "", "expires": 0}
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

        cfg["spotify_client_id"] = cfg.get("spotify_client_id") or os.getenv("SPOTIFY_CLIENT_ID", "")
        cfg["spotify_client_secret"] = cfg.get("spotify_client_secret") or os.getenv("SPOTIFY_CLIENT_SECRET", "")
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
    def mask(value: str) -> str:
        if not value:
            return "not set"
        if len(value) <= 10:
            return "hidden"
        return f"{value[:5]}...{value[-4:]}"

    @staticmethod
    def is_url(value: str) -> bool:
        return bool(re.match(r"https?://", value or "", re.I))

    @staticmethod
    def provider_from_url(url: str) -> str:
        host = urlparse(url).netloc.lower()
        if "spotify.com" in host:
            return "spotify"
        if "music.apple.com" in host or "itunes.apple.com" in host:
            return "apple"
        if "soundcloud.com" in host:
            return "soundcloud"
        return "unknown"

    @staticmethod
    def normalize(text: str) -> str:
        text = (text or "").lower().strip()
        text = re.sub(r"[^a-zа-яёіїєґ0-9\s]+", " ", text, flags=re.I)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def safe_filename(text: str) -> str:
        text = re.sub(r"[^a-zA-Zа-яА-ЯёЁіїєґІЇЄҐ0-9 ._-]+", "", text or "track").strip()
        text = re.sub(r"\s+", " ", text)
        return (text or "track")[:90]

    def score(self, query: str, item: dict) -> float:
        query_norm = self.normalize(query)
        title_norm = self.normalize(item.get("title", ""))
        full_norm = self.normalize(f"{item.get('title', '')} {item.get('artist', '')}")
        if not query_norm or not full_norm:
            return 0.0
        return (
            SequenceMatcher(None, query_norm, full_norm).ratio()
            + SequenceMatcher(None, query_norm, title_norm).ratio()
            + (0.35 if query_norm in title_norm else 0)
        )

    def sort_results(self, query: str, results: list[dict], limit: int = 3) -> list[dict]:
        seen = set()
        clean = []
        for item in results:
            key = self.normalize(f"{item.get('title')} {item.get('artist')}")
            if not key or key in seen:
                continue
            seen.add(key)
            item["score"] = self.score(query, item)
            clean.append(item)
        clean.sort(key=lambda item: item.get("score", 0), reverse=True)
        return clean[:limit]

    # -------------------- ui --------------------

    def result_text(self, query: str, results: list[dict]) -> str:
        lines = [f"<b>Found 3 matching tracks:</b> <code>{self.esc(query)}</code>", ""]
        for index, item in enumerate(results, 1):
            title = self.esc(item.get("title") or "Unknown")
            artist = self.esc(item.get("artist") or "Unknown")
            provider = self.esc(item.get("provider") or "music")
            lines.append(f"<b>{index}.</b> <b>{title}</b> - <code>{artist}</code> <i>({provider})</i>")
        lines.append("")
        lines.append("Choose a track below.")
        return "\n".join(lines)

    def result_buttons(self, key: str, results: list[dict]):
        rows = []
        for index, item in enumerate(results, 1):
            title = (item.get("title") or "Unknown")[:30]
            artist = (item.get("artist") or "Unknown")[:20]
            rows.append([
                {
                    "text": f"{index}. {title} - {artist}",
                    "callback": self.select_callback,
                    "args": (key, index - 1),
                }
            ])
        rows.append([
            {
                "text": "Close",
                "callback": self.close_callback,
                "args": (),
            }
        ])
        return self.inline_buttons(*rows)

    def card_text(self, item: dict) -> str:
        title = self.esc(item.get("title") or "Unknown")
        artist = self.esc(item.get("artist") or "Unknown")
        album = self.esc(item.get("album") or "")
        provider = self.esc(item.get("provider") or "music")
        lines = [f"<b>{title}</b>", f"Artist: <code>{artist}</code>"]
        if album:
            lines.append(f"Album: <code>{album}</code>")
        lines.append(f"Source: <code>{provider}</code>")
        if not item.get("preview_url"):
            lines.append("")
            lines.append("Audio preview is not available for this track.")
        return "\n".join(lines)

    def card_buttons(self, item: dict, key: str | None = None):
        rows = []
        if item.get("url"):
            rows.append([{"text": "Open", "url": item["url"]}])
        if key:
            rows.append([{"text": "Back", "callback": self.back_callback, "args": (key,)}])
        rows.append([{"text": "Close", "callback": self.close_callback, "args": ()}])
        return self.inline_buttons(*rows)

    # -------------------- http --------------------

    async def get_json(self, url: str, headers: dict | None = None):
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers or {}) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = {}
                if response.status >= 400:
                    return None
                return data

    # -------------------- Apple/iTunes --------------------

    async def apple_search(self, query: str, limit: int = 5) -> list[dict]:
        url = f"https://itunes.apple.com/search?term={quote(query)}&media=music&entity=song&limit={int(limit)}"
        data = await self.get_json(url)
        if not data:
            return []
        return [
            {
                "provider": "apple",
                "title": item.get("trackName") or "Unknown",
                "artist": item.get("artistName") or "Unknown",
                "album": item.get("collectionName") or "",
                "url": item.get("trackViewUrl") or item.get("collectionViewUrl") or "",
                "preview_url": item.get("previewUrl") or "",
            }
            for item in data.get("results", [])
        ]

    async def apple_lookup(self, url: str):
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        track_id = params.get("i", [None])[0]
        if not track_id:
            match = re.search(r"/(\d+)(?:\?|$)", parsed.path)
            if match:
                track_id = match.group(1)
        if not track_id:
            return None

        data = await self.get_json(f"https://itunes.apple.com/lookup?id={quote(track_id)}&entity=song")
        if not data:
            return None
        results = data.get("results", [])
        song = next((item for item in results if item.get("wrapperType") == "track"), results[0] if results else None)
        if not song:
            return None
        return {
            "provider": "apple",
            "title": song.get("trackName") or song.get("collectionName") or "Apple Music",
            "artist": song.get("artistName") or "Unknown",
            "album": song.get("collectionName") or "",
            "url": song.get("trackViewUrl") or url,
            "preview_url": song.get("previewUrl") or "",
        }

    # -------------------- Spotify --------------------

    async def spotify_token(self):
        cfg = self.load_cfg()
        client_id = cfg.get("spotify_client_id", "").strip()
        client_secret = cfg.get("spotify_client_secret", "").strip()
        if not client_id or not client_secret:
            return None
        if self.spotify_cache.get("token") and self.spotify_cache.get("expires", 0) > time.time() + 30:
            return self.spotify_cache["token"]

        auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        timeout = aiohttp.ClientTimeout(total=25)
        headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("https://accounts.spotify.com/api/token", data="grant_type=client_credentials", headers=headers) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    return None
                if response.status >= 400:
                    return None
        token = data.get("access_token")
        if token:
            self.spotify_cache = {"token": token, "expires": time.time() + int(data.get("expires_in", 3600))}
        return token

    @staticmethod
    def spotify_item(item: dict) -> dict:
        artists = ", ".join(artist.get("name", "") for artist in item.get("artists", []) if artist.get("name")) or "Unknown"
        album = item.get("album", {}) or {}
        return {
            "provider": "spotify",
            "title": item.get("name") or "Unknown",
            "artist": artists,
            "album": album.get("name") or "",
            "url": (item.get("external_urls") or {}).get("spotify") or "",
            "preview_url": item.get("preview_url") or "",
        }

    async def spotify_search(self, query: str, limit: int = 5) -> list[dict]:
        token = await self.spotify_token()
        if not token:
            return []
        url = f"https://api.spotify.com/v1/search?q={quote(query)}&type=track&limit={int(limit)}"
        data = await self.get_json(url, headers={"Authorization": f"Bearer {token}"})
        if not data:
            return []
        return [self.spotify_item(item) for item in data.get("tracks", {}).get("items", [])]

    async def spotify_lookup(self, url: str):
        token = await self.spotify_token()
        if not token:
            return None
        match = re.search(r"/track/([A-Za-z0-9]+)", url)
        if not match:
            return None
        data = await self.get_json(
            f"https://api.spotify.com/v1/tracks/{match.group(1)}",
            headers={"Authorization": f"Bearer {token}"},
        )
        return self.spotify_item(data) if data else None

    # -------------------- SoundCloud --------------------

    def soundcloud_headers(self):
        token = self.load_cfg().get("soundcloud_token", "").strip()
        return {"Authorization": f"OAuth {token}"} if token else None

    @staticmethod
    def soundcloud_item(item: dict) -> dict:
        user = item.get("user", {}) or {}
        return {
            "provider": "soundcloud",
            "title": item.get("title") or "Unknown",
            "artist": user.get("username") or item.get("publisher_metadata", {}).get("artist") or "Unknown",
            "album": item.get("label_name") or "",
            "url": item.get("permalink_url") or item.get("uri") or "",
            "preview_url": "",
        }

    async def soundcloud_search(self, query: str, limit: int = 5) -> list[dict]:
        headers = self.soundcloud_headers()
        if not headers:
            return []
        data = await self.get_json(f"https://api.soundcloud.com/tracks?q={quote(query)}&limit={int(limit)}", headers=headers)
        if not data:
            return []
        if isinstance(data, dict):
            data = data.get("collection", [])
        return [self.soundcloud_item(item) for item in data if isinstance(item, dict)]

    async def soundcloud_lookup(self, url: str):
        headers = self.soundcloud_headers()
        if headers:
            data = await self.get_json(f"https://api.soundcloud.com/resolve?url={quote(url)}", headers=headers)
            if isinstance(data, dict) and data.get("kind") == "track":
                return self.soundcloud_item(data)
        return {
            "provider": "soundcloud",
            "title": "SoundCloud",
            "artist": "Unknown",
            "album": "",
            "url": url,
            "preview_url": "",
        }

    # -------------------- search core --------------------

    async def search_all(self, query: str) -> list[dict]:
        tasks = [self.apple_search(query, 5), self.spotify_search(query, 5), self.soundcloud_search(query, 5)]
        results = []
        for result in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(result, list):
                results.extend(result)
        return self.sort_results(query, results, 3)

    async def lookup_link(self, url: str):
        provider = self.provider_from_url(url)
        if provider == "spotify":
            return await self.spotify_lookup(url)
        if provider == "apple":
            return await self.apple_lookup(url)
        if provider == "soundcloud":
            return await self.soundcloud_lookup(url)
        return None

    # -------------------- audio --------------------

    async def download_preview(self, url: str, item: dict) -> str | None:
        if not url or not url.startswith("http"):
            return None
        suffix = Path(urlparse(url).path).suffix or ".m4a"
        if len(suffix) > 8:
            suffix = ".m4a"
        filename = self.safe_filename(f"{item.get('artist', 'Unknown')} - {item.get('title', 'Track')}") + suffix
        path = str(Path(tempfile.gettempdir()) / filename)
        try:
            timeout = aiohttp.ClientTimeout(total=45)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status >= 400:
                        return None
                    total = 0
                    with open(path, "wb") as file:
                        async for chunk in response.content.iter_chunked(65536):
                            total += len(chunk)
                            if total > 12 * 1024 * 1024:
                                return None
                            file.write(chunk)
            return path if os.path.getsize(path) > 0 else None
        except Exception:
            return None

    async def send_preview_audio(self, client, chat_id, item: dict) -> bool:
        preview_url = item.get("preview_url") or ""
        if not preview_url or preview_url == item.get("url"):
            return False
        path = await self.download_preview(preview_url, item)
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

    @command("tr", description="Search music", usage=".tr query_or_link | .tr status | .tr spotify id secret | .tr soundcloud token")
    async def tr_cmd(self, event, args):
        text = self.args_text(args)
        if not text:
            await event.edit(
                "<b>Usage:</b> <code>.tr track name</code>\n"
                "<b>Link:</b> <code>.tr https://music.apple.com/...</code>\n"
                "<b>Status:</b> <code>.tr status</code>",
                parse_mode="html",
            )
            return

        low = text.lower().strip()
        if low == "status":
            await self.status(event)
            return
        if low.startswith("spotify "):
            await self.set_spotify(event, text[len("spotify "):].strip())
            return
        if low.startswith("soundcloud "):
            await self.set_soundcloud(event, text[len("soundcloud "):].strip())
            return

        await event.edit("<b>Searching...</b>", parse_mode="html")

        if self.is_url(text):
            item = await self.lookup_link(text)
            if not item:
                await event.edit("<b>Track was not found.</b>", parse_mode="html")
                return
            ok = await self.send_preview_audio(event.client, event.chat_id, item)
            if ok:
                try:
                    await event.delete()
                except Exception:
                    pass
                return
            await self.inline_send(
                event,
                self.card_text(item),
                reply_markup=self.card_buttons(item),
                parse_mode="html",
                link_preview=False,
                ttl=3600,
            )
            return

        results = await self.search_all(text)
        if not results:
            await event.edit("<b>Nothing found.</b>", parse_mode="html")
            return

        key = f"{int(time.time())}_{id(event)}"
        self.cache[key] = {
            "query": text,
            "results": results,
            "time": time.time(),
        }

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
            "Command: <code>.tr</code>\n"
            "Apple/iTunes: <code>enabled, no token</code>\n"
            f"Spotify client_id: <code>{self.esc(self.mask(cfg.get('spotify_client_id', '')))}</code>\n"
            f"Spotify secret: <code>{self.esc(self.mask(cfg.get('spotify_client_secret', '')))}</code>\n"
            f"SoundCloud token: <code>{self.esc(self.mask(cfg.get('soundcloud_token', '')))}</code>"
        )
        await event.edit(text, parse_mode="html")

    async def set_spotify(self, event, raw: str) -> None:
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            await event.edit("<b>Usage:</b> <code>.tr spotify client_id client_secret</code>", parse_mode="html")
            return
        cfg = self.load_cfg()
        cfg["spotify_client_id"] = parts[0].strip()
        cfg["spotify_client_secret"] = parts[1].strip()
        self.spotify_cache = {"token": "", "expires": 0}
        self.save_cfg(cfg)
        await event.edit("<b>Spotify keys saved.</b>", parse_mode="html")

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
            ok = await self.send_preview_audio(client, chat_id, item)
            if ok:
                await call.edit("Done.", reply_markup=None)
                return

        await call.edit(
            self.card_text(item),
            reply_markup=self.card_buttons(item, key),
            parse_mode="html",
            link_preview=False,
        )

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
