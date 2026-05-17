# meta developer: @DeathTerror
# meta name: DownloaderDtg
# meta description: SoundCloud, TikTok and YouTube downloader for DeathTG with native inline buttons.
# meta category: media
# meta version: 2.2.1
# meta author: DeathTerror
# requires: yt-dlp

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from yt_dlp import YoutubeDL

from deathtg.loader import Module
from deathtg.command import command

logger = logging.getLogger("deathtg.modules.DownloaderDtg")

SC_API = "https://api-v2.soundcloud.com"
SC_CLIENT_ID = "iZ0gA7dgGx7v1N077p276V046g7uN67p"

YTDL_OPTS_BASE = {
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "restrictfilenames": False,
}


class DownloaderDtgMod(Module):
    """Media downloader for DeathTG: SoundCloud (.tr), TikTok (.tt), YouTube (.yt)."""

    strings = {
        "name": "DownloaderDtg",
        "title": "DownloaderDtg",
        "description": "Download media from SoundCloud, TikTok and YouTube with native inline buttons.",
        "category": "media",
        "version": "2.2.1",
        "author": "DeathTerror",
        "commands": ".tr, .tt, .yt",
        "usage": ".tr query/link | .tt tiktok_link | .yt youtube_link",
        "permissions": "owner",
    }

    STOP_WORDS = {
        "the", "a", "an", "and", "or", "feat", "ft", "prod", "official",
        "audio", "video", "lyrics", "lyric", "remix", "slowed", "reverb",
    }

    def __init__(self) -> None:
        super().__init__()
        self.sc_cache: Dict[str, List[dict]] = {}
        self.sc_client_id = SC_CLIENT_ID

    # -------------------- stdlib HTTP --------------------

    @staticmethod
    def http_headers() -> dict:
        return {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept": "application/json,text/html,*/*",
        }

    def http_get_bytes(self, url: str, timeout: int = 30) -> bytes:
        request = urllib.request.Request(url, headers=self.http_headers())
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    def http_get_json(self, url: str, params: Optional[dict] = None, timeout: int = 30) -> dict:
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        raw = self.http_get_bytes(url, timeout=timeout)
        return json.loads(raw.decode("utf-8", errors="replace"))

    def http_head_url(self, url: str, timeout: int = 20) -> str:
        request = urllib.request.Request(url, headers=self.http_headers(), method="HEAD")
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        with opener.open(request, timeout=timeout) as response:
            return response.geturl()

    # -------------------- helpers --------------------

    @staticmethod
    def args_text(args) -> str:
        if isinstance(args, (list, tuple)):
            return " ".join(str(item) for item in args).strip()
        return str(args or "").strip()

    @staticmethod
    def esc(text: object) -> str:
        return html.escape(str(text or ""), quote=False)

    @staticmethod
    def normalize(text: str) -> str:
        text = str(text or "").lower().replace("ё", "е")
        text = re.sub(r"[\[\](){}]+", " ", text)
        text = re.sub(r"[^a-zа-яіїєґ0-9\s]+", " ", text, flags=re.I)
        return re.sub(r"\s+", " ", text).strip()

    def tokens(self, text: str) -> List[str]:
        return [item for item in self.normalize(text).split() if len(item) > 1 and item not in self.STOP_WORDS]

    @staticmethod
    def cleanup_file(path: str | Path | None) -> None:
        if not path:
            return
        try:
            path_obj = Path(path)
            if path_obj.exists() and path_obj.is_file():
                path_obj.unlink()
        except Exception:
            pass

    async def reply_text(self, event) -> Optional[str]:
        try:
            reply = await event.get_reply_message()
            if reply and getattr(reply, "raw_text", None):
                return reply.raw_text.strip()
        except Exception:
            return None
        return None

    async def set_status(self, event, text: str):
        try:
            return await event.edit(text, parse_mode="html")
        except Exception:
            return await event.respond(text, parse_mode="html")

    def result_score(self, query: str, item: dict) -> float:
        query_norm = self.normalize(query)
        title = self.normalize(item.get("title", ""))
        artist = self.normalize((item.get("user") or {}).get("username", ""))
        combined = self.normalize(f"{title} {artist}")
        query_tokens = set(self.tokens(query))
        combined_tokens = set(self.tokens(combined))
        if not query_tokens or not combined_tokens:
            return 0.0
        coverage = len(query_tokens & combined_tokens) / max(1, len(query_tokens))
        precision = len(query_tokens & combined_tokens) / max(1, len(combined_tokens))
        seq = SequenceMatcher(None, query_norm, combined).ratio()
        title_seq = SequenceMatcher(None, query_norm, title).ratio()
        bonus = 0.0
        if query_norm and query_norm in combined:
            bonus += 0.35
        if query_tokens <= combined_tokens:
            bonus += 0.22
        return coverage * 0.50 + precision * 0.18 + seq * 0.18 + title_seq * 0.14 + bonus

    def query_variants(self, query: str) -> List[str]:
        clean = self.normalize(query)
        tokens = self.tokens(query)
        variants = [query.strip(), clean]
        if " - " in query:
            left, right = query.split(" - ", 1)
            variants.extend([f"{left} {right}", f"{right} {left}", left, right])
        if len(tokens) >= 2:
            variants.append(" ".join(tokens))
        if len(tokens) >= 3:
            variants.append(" ".join(tokens[:3]))
            variants.append(" ".join(tokens[-3:]))
        result = []
        seen = set()
        for item in variants:
            item = " ".join(str(item or "").split()).strip()
            key = item.lower()
            if item and key not in seen:
                seen.add(key)
                result.append(item)
        return result[:6]

    # -------------------- SoundCloud UI/search --------------------

    def soundcloud_text(self, query: str, tracks: List[dict]) -> str:
        lines = [f"<b>SoundCloud:</b> <code>{self.esc(query)}</code>", ""]
        for index, track in enumerate(tracks, 1):
            title = self.esc(track.get("title", "Unknown"))
            artist = self.esc((track.get("user") or {}).get("username", "Unknown"))
            score = float(track.get("score", 0))
            lines.append(f"<b>{index}.</b> <b>{title}</b> - <code>{artist}</code> <i>({score:.2f})</i>")
        lines += ["", "Choose track below."]
        return "\n".join(lines)

    def soundcloud_buttons(self, key: str, tracks: List[dict]):
        rows = []
        for index, track in enumerate(tracks, 1):
            title = str(track.get("title", "Unknown"))[:30]
            artist = str((track.get("user") or {}).get("username", "Unknown"))[:18]
            rows.append([
                {"text": f"{index}. {artist} - {title}", "callback": self.sc_callback, "args": (key, index - 1)}
            ])
        rows.append([{"text": "Close", "callback": self.close_callback, "args": ()}])
        return self.inline_buttons(*rows)

    async def search_sc_raw(self, query: str, limit: int = 20) -> List[dict]:
        def sync_search() -> List[dict]:
            try:
                data = self.http_get_json(
                    f"{SC_API}/tracks",
                    params={"q": query, "client_id": self.sc_client_id, "limit": limit},
                    timeout=15,
                )
                if isinstance(data, dict):
                    return data.get("collection", []) or []
            except Exception as exc:
                logger.warning("SoundCloud search failed: %s", exc)
            return []

        return await asyncio.to_thread(sync_search)

    async def search_sc(self, query: str, limit: int = 3) -> List[dict]:
        tracks: List[dict] = []
        seen = set()
        for variant in self.query_variants(query):
            for track in await self.search_sc_raw(variant, 20):
                url = track.get("permalink_url") or track.get("uri") or f"{track.get('title')}:{track.get('id')}"
                if url in seen:
                    continue
                seen.add(url)
                track["score"] = self.result_score(query, track)
                tracks.append(track)
        tracks.sort(key=lambda item: item.get("score", 0), reverse=True)
        return tracks[:limit]

    async def download_sc(self, url: str) -> Tuple[str, dict]:
        workdir = tempfile.mkdtemp(prefix="dtg_sc_")
        opts = {**YTDL_OPTS_BASE, "format": "bestaudio/best", "outtmpl": str(Path(workdir) / "%(uploader)s - %(title)s.%(ext)s")}

        def download() -> Tuple[str, dict]:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info), info

        return await asyncio.to_thread(download)

    async def send_sc(self, client, chat_id, url: str) -> bool:
        file_name = None
        try:
            file_name, info = await self.download_sc(url)
            title = info.get("title", "Track")
            artist = info.get("uploader", "SoundCloud")
            await client.send_file(chat_id, file=file_name, caption=f"<b>{self.esc(artist)} - {self.esc(title)}</b>", parse_mode="html", force_document=False)
            return True
        except Exception as exc:
            logger.exception("SoundCloud download failed: %s", exc)
            return False
        finally:
            self.cleanup_file(file_name)
            try:
                parent = Path(file_name).parent if file_name else None
                if parent and parent.exists() and parent.name.startswith("dtg_sc_"):
                    parent.rmdir()
            except Exception:
                pass

    @command("tr", description="Download SoundCloud track", usage=".tr link_or_query")
    async def tr_cmd(self, event, args):
        query = self.args_text(args) or await self.reply_text(event)
        if not query:
            await self.set_status(event, "<b>Usage:</b> <code>.tr soundcloud link or track name</code>")
            return

        if "soundcloud.com" in query:
            await self.set_status(event, "<b>Downloading SoundCloud track...</b>")
            ok = await self.send_sc(event.client, event.chat_id, query)
            if ok:
                try:
                    await event.delete()
                except Exception:
                    pass
            else:
                await self.set_status(event, "<b>SoundCloud download failed.</b>")
            return

        await self.set_status(event, "<b>Searching SoundCloud...</b>")
        tracks = await self.search_sc(query, 3)
        if not tracks:
            await self.set_status(event, "<b>Nothing found.</b>")
            return

        key = f"sc_{int(time.time())}_{id(event)}"
        self.sc_cache[key] = tracks
        await self.inline_send(event, self.soundcloud_text(query, tracks), reply_markup=self.soundcloud_buttons(key, tracks), parse_mode="html", link_preview=False, ttl=3600)

    async def sc_callback(self, call, key: str, index: int):
        tracks = self.sc_cache.get(key)
        if not tracks or index >= len(tracks):
            await call.edit("Session expired.", reply_markup=None)
            return
        url = tracks[index].get("permalink_url")
        if not url:
            await call.edit("Track URL not found.", reply_markup=None)
            return
        await call.edit("<b>Downloading...</b>", reply_markup=None, parse_mode="html")
        client = getattr(call, "original_client", None)
        chat_id = getattr(call, "original_chat_id", None)
        if client and chat_id and await self.send_sc(client, chat_id, url):
            await call.edit("Done.", reply_markup=None)
        else:
            await call.edit("Download failed.", reply_markup=None)

    # -------------------- TikTok --------------------

    @command("tt", description="Download TikTok video", usage=".tt tiktok_link")
    async def tt_cmd(self, event, args):
        url = self.args_text(args) or await self.reply_text(event)
        if not url:
            await self.set_status(event, "<b>Usage:</b> <code>.tt tiktok link</code>")
            return
        if "tiktok.com" not in url:
            await self.set_status(event, "<b>Unsupported TikTok link.</b>")
            return
        await self.set_status(event, "<b>Downloading TikTok...</b>")
        ok = await self.send_tiktok(event.client, event.chat_id, url)
        if ok:
            try:
                await event.delete()
            except Exception:
                pass
        else:
            await self.set_status(event, "<b>TikTok download failed.</b>")

    async def parse_tiktok(self, url: str) -> Tuple[Optional[str], str]:
        def sync_parse() -> Tuple[Optional[str], str]:
            try:
                actual_url = self.http_head_url(url, timeout=15)
                try:
                    query = parse_qs(urlsplit(actual_url).query)
                    item_id = query.get("share_item_id", [""])[0]
                except Exception:
                    item_id = "".join(re.findall("[0-9]", urlsplit(actual_url).path.split("/")[-1]))
                api_url = f"https://api-va.tiktokv.com/aweme/v1/multi/aweme/detail/?aweme_ids=%5B{item_id}%5D"
                result = self.http_get_json(api_url, timeout=15)
                details = result.get("aweme_details") or []
                if not details:
                    return None, api_url
                video = details[0].get("video") or {}
                rates = video.get("bit_rate") or []
                if rates:
                    urls = rates[0].get("play_addr", {}).get("url_list") or []
                    if urls:
                        return urls[-1], api_url
            except Exception as exc:
                logger.warning("TikTok parse failed: %s", exc)
            return None, url

        return await asyncio.to_thread(sync_parse)

    async def send_tiktok(self, client, chat_id, url: str) -> bool:
        file_path = None
        try:
            video_url, fallback_url = await self.parse_tiktok(url)
            target_url = video_url or fallback_url

            def download() -> str:
                data = self.http_get_bytes(target_url, timeout=60)
                path = Path(tempfile.gettempdir()) / f"dtg_tiktok_{int(time.time())}.mp4"
                path.write_bytes(data)
                return str(path)

            file_path = await asyncio.to_thread(download)
            await client.send_file(chat_id, file=file_path, supports_streaming=True)
            return True
        except Exception as exc:
            logger.exception("TikTok download failed: %s", exc)
            return False
        finally:
            self.cleanup_file(file_path)

    # -------------------- YouTube --------------------

    @command("yt", description="Download YouTube video", usage=".yt youtube_link")
    async def yt_cmd(self, event, args):
        url = self.args_text(args) or await self.reply_text(event)
        if not url:
            await self.set_status(event, "<b>Usage:</b> <code>.yt youtube link</code>")
            return
        if "youtube.com" not in url and "youtu.be" not in url:
            await self.set_status(event, "<b>Unsupported YouTube link.</b>")
            return
        await self.set_status(event, "<b>Downloading YouTube...</b>")
        ok = await self.send_youtube(event.client, event.chat_id, url)
        if ok:
            try:
                await event.delete()
            except Exception:
                pass
        else:
            await self.set_status(event, "<b>YouTube download failed.</b>")

    async def send_youtube(self, client, chat_id, url: str) -> bool:
        file_name = None
        try:
            workdir = tempfile.mkdtemp(prefix="dtg_yt_")
            opts = {
                **YTDL_OPTS_BASE,
                "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
                "outtmpl": str(Path(workdir) / "yt_%(id)s.%(ext)s"),
                "merge_output_format": "mp4",
            }

            def download() -> Tuple[str, dict]:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return ydl.prepare_filename(info), info

            file_name, info = await asyncio.to_thread(download)
            await client.send_file(chat_id, file=file_name, caption=f"<b>{self.esc(info.get('title', 'YouTube Video'))}</b>", parse_mode="html", supports_streaming=True)
            return True
        except Exception as exc:
            logger.exception("YouTube download failed: %s", exc)
            return False
        finally:
            self.cleanup_file(file_name)
            try:
                parent = Path(file_name).parent if file_name else None
                if parent and parent.exists() and parent.name.startswith("dtg_yt_"):
                    parent.rmdir()
            except Exception:
                pass

    async def close_callback(self, call):
        await call.edit("Closed.", reply_markup=None)
