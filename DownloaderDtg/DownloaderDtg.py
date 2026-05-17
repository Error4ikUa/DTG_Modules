# meta developer: @DeathTerror
# meta name: DownloaderDtg
# meta description: SoundCloud, TikTok and YouTube downloader for DeathTG with native inline buttons.
# meta category: media
# meta version: 2.3.1
# meta author: DeathTerror
# requires: yt-dlp

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from yt_dlp import YoutubeDL

from deathtg.loader import Module
from deathtg.command import command

logger = logging.getLogger("deathtg.modules.DownloaderDtg")

YTDL_BASE = {
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
    "ignoreerrors": True,
    "restrictfilenames": False,
    "noplaylist": True,
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    },
}


class DownloaderDtgMod(Module):
    """Downloader for DeathTG: SoundCloud (.tr), TikTok (.tt), YouTube (.yt)."""

    strings = {
        "name": "DownloaderDtg",
        "title": "DownloaderDtg",
        "description": "Download media from SoundCloud, TikTok and YouTube with native inline buttons.",
        "category": "media",
        "version": "2.3.1",
        "author": "DeathTerror",
        "commands": ".tr, .tt, .yt",
        "usage": ".tr query/link | .tt tiktok_link | .yt youtube_link",
        "permissions": "owner",
    }

    def __init__(self) -> None:
        super().__init__()
        self.sc_cache: Dict[str, List[dict]] = {}

    @staticmethod
    def args_text(args) -> str:
        if isinstance(args, (list, tuple)):
            return " ".join(str(item) for item in args).strip()
        return str(args or "").strip()

    @staticmethod
    def esc(text: object) -> str:
        return html.escape(str(text or ""), quote=False)

    @staticmethod
    def is_url(text: str) -> bool:
        return bool(re.match(r"https?://", str(text or ""), re.I))

    @staticmethod
    def cleanup_path(path: str | Path | None) -> None:
        if not path:
            return
        try:
            target = Path(path)
            if target.exists() and target.is_file():
                target.unlink()
            parent = target.parent
            if parent.exists() and parent.is_dir() and parent.name.startswith("dtg_downloader_"):
                try:
                    parent.rmdir()
                except OSError:
                    pass
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

    @staticmethod
    def prepared_path(ydl: YoutubeDL, info: dict) -> str:
        requested = info.get("requested_downloads") or []
        if requested:
            filepath = requested[0].get("filepath") or requested[0].get("filename")
            if filepath:
                return filepath
        filepath = info.get("filepath") or info.get("_filename")
        if filepath:
            return filepath
        return ydl.prepare_filename(info)

    async def ytdlp_extract(self, target: str, opts: dict, download: bool = False) -> dict | None:
        def run() -> dict | None:
            with YoutubeDL({**YTDL_BASE, **opts}) as ydl:
                return ydl.extract_info(target, download=download)
        try:
            return await asyncio.to_thread(run)
        except Exception as exc:
            logger.exception("yt-dlp failed for %s: %s", target, exc)
            return None

    async def ytdlp_download(self, url: str, media_type: str) -> Tuple[Optional[str], Optional[dict]]:
        workdir = tempfile.mkdtemp(prefix="dtg_downloader_")
        if media_type == "audio":
            opts = {
                "format": "bestaudio/best",
                "outtmpl": str(Path(workdir) / "%(uploader|Unknown)s - %(title|Track)s.%(ext)s"),
            }
        else:
            opts = {
                "format": "best[ext=mp4][height<=720]/best[height<=720]/best[ext=mp4]/best",
                "outtmpl": str(Path(workdir) / "%(title|video)s.%(ext)s"),
            }
        info = await self.ytdlp_extract(url, opts, download=True)
        if not info:
            return None, None
        file_path = self.prepared_path(YoutubeDL({**YTDL_BASE, **opts}), info)
        if file_path and Path(file_path).exists():
            return file_path, info
        files = [p for p in Path(workdir).glob("*") if p.is_file()]
        if files:
            newest = max(files, key=lambda p: p.stat().st_mtime)
            return str(newest), info
        return None, info

    async def send_downloaded(self, client, chat_id, url: str, media_type: str, with_caption: bool = True) -> bool:
        file_path = None
        try:
            file_path, info = await self.ytdlp_download(url, media_type)
            if not file_path:
                return False

            caption = ""
            if with_caption:
                title = self.esc((info or {}).get("title") or "Media")
                uploader = self.esc((info or {}).get("uploader") or (info or {}).get("channel") or "")
                caption = f"<b>{title}</b>" if not uploader else f"<b>{uploader} - {title}</b>"

            await client.send_file(
                chat_id,
                file=file_path,
                caption=caption,
                parse_mode="html" if caption else None,
                force_document=False,
                supports_streaming=(media_type == "video"),
            )
            return True
        except Exception as exc:
            logger.exception("send_downloaded failed: %s", exc)
            return False
        finally:
            self.cleanup_path(file_path)

    async def search_sc(self, query: str, limit: int = 3) -> List[dict]:
        target = query if self.is_url(query) else f"scsearch{limit}:{query}"
        info = await self.ytdlp_extract(target, {"extract_flat": True}, download=False)
        if not info:
            return []
        if info.get("_type") == "playlist":
            entries = [entry for entry in (info.get("entries") or []) if entry]
        else:
            entries = [info]
        result = []
        for entry in entries[:limit]:
            url = entry.get("webpage_url") or entry.get("url") or entry.get("original_url")
            if url and not str(url).startswith("http") and entry.get("ie_key"):
                url = f"https://soundcloud.com/{url}"
            result.append(
                {
                    "title": entry.get("title") or "Unknown",
                    "artist": entry.get("uploader") or entry.get("channel") or "SoundCloud",
                    "url": url or query,
                }
            )
        return result

    def soundcloud_text(self, query: str, tracks: List[dict]) -> str:
        lines = [f"<b>SoundCloud:</b> <code>{self.esc(query)}</code>", ""]
        for index, track in enumerate(tracks, 1):
            lines.append(
                f"<b>{index}.</b> <b>{self.esc(track.get('title'))}</b> - <code>{self.esc(track.get('artist'))}</code>"
            )
        lines += ["", "Choose track below."]
        return "\n".join(lines)

    def soundcloud_buttons(self, key: str, tracks: List[dict]):
        rows = []
        for index, track in enumerate(tracks, 1):
            title = str(track.get("title") or "Unknown")[:34]
            artist = str(track.get("artist") or "SoundCloud")[:18]
            rows.append([
                {"text": f"{index}. {artist} - {title}", "callback": self.sc_callback, "args": (key, index - 1)}
            ])
        rows.append([{"text": "Close", "callback": self.close_callback, "args": ()}])
        return self.inline_buttons(*rows)

    @command("tr", description="Download SoundCloud track", usage=".tr link_or_query")
    async def tr_cmd(self, event, args):
        query = self.args_text(args) or await self.reply_text(event)
        if not query:
            await self.set_status(event, "<b>Usage:</b> <code>.tr soundcloud link or track name</code>")
            return

        if self.is_url(query):
            await self.set_status(event, "<b>Downloading SoundCloud track...</b>")
            ok = await self.send_downloaded(event.client, event.chat_id, query, "audio")
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
        await self.inline_send(
            event,
            self.soundcloud_text(query, tracks),
            reply_markup=self.soundcloud_buttons(key, tracks),
            parse_mode="html",
            link_preview=False,
            ttl=3600,
        )

    async def sc_callback(self, call, key: str, index: int):
        tracks = self.sc_cache.get(key)
        if not tracks or index >= len(tracks):
            await call.edit("Session expired.", reply_markup=None)
            return
        url = tracks[index].get("url")
        if not url:
            await call.edit("Track URL not found.", reply_markup=None)
            return
        await call.edit("<b>Downloading...</b>", reply_markup=None, parse_mode="html")
        client = getattr(call, "original_client", None)
        chat_id = getattr(call, "original_chat_id", None)
        if client and chat_id and await self.send_downloaded(client, chat_id, url, "audio"):
            await call.edit("Done.", reply_markup=None)
        else:
            await call.edit("Download failed.", reply_markup=None)

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
        ok = await self.send_downloaded(event.client, event.chat_id, url, "video", with_caption=False)
        if ok:
            try:
                await event.delete()
            except Exception:
                pass
        else:
            await self.set_status(event, "<b>TikTok download failed.</b>")

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
        ok = await self.send_downloaded(event.client, event.chat_id, url, "video")
        if ok:
            try:
                await event.delete()
            except Exception:
                pass
        else:
            await self.set_status(event, "<b>YouTube download failed.</b>")

    async def close_callback(self, call):
        await call.edit("Closed.", reply_markup=None)
