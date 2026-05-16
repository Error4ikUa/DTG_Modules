# meta developer: @DeathTerror
# meta name: MusicSearchDtg
# meta description: SoundCloud-only music search with DeathTG inline buttons.
# meta category: media
# meta version: 2.0.0
# meta author: DeathTerror
# requires: aiohttp

import asyncio
import io
import logging
import re
from typing import Dict, List, Optional, Tuple, Union
from urllib.parse import parse_qs
from urllib.parse import urlsplit as E

import requests
from PIL import Image
from telethon.tl.types import Message
from yt_dlp import YoutubeDL

from .. import loader, utils

logger = logging.getLogger(__name__)

_SC_API = "https://api-v2.soundcloud.com"
_CLIENT_ID = "iZ0gA7dgGx7v1N077p276V046g7uN67p"  

YTDL_OPTS_BASE = {
    "quiet": True,
    "no_warnings": True,
    "nocheckcertificate": True,
}


class MediaDownloaderMod(loader.Module):
    """Модуль для скачивания медиа: SoundCloud (.tr), TikTok (.tt) и YouTube (.yt)"""

    strings = {
        "name": "MediaDownloader",
        "description": "Скачивание музыки и видео из SoundCloud, TikTok и YouTube",
        "no_args": "<emoji document_id=5778527486270770928>❌</emoji> <b>Введите название или ссылку!</b>",
        "loading": "<emoji document_id=5841359499146825803>⏳</emoji> <b>Загрузка...</b>",
        "searching": "<emoji document_id=5841359499146825803>🔍</emoji> <b>Поиск треков в SoundCloud...</b>",
        "sc_select": "<emoji document_id=6007938409857815902>🎧</emoji> <b>Выберите нужный трек из SoundCloud:</b>",
        "downloading": "<emoji document_id=5841359499146825803>📥</emoji> <b>Скачивание файла...</b>",
        "error": "<emoji document_id=5778527486270770928>❌</emoji> <b>Ошибка:</b> <code>{}</code>",
        "bad_url": "<emoji document_id=5778527486270770928>❌</emoji> <b>Неподдерживаемая ссылка.</b>",
    }

    strings_ru = {
        "no_args": "<emoji document_id=5778527486270770928>❌</emoji> <b>Введите название или ссылку!</b>",
        "loading": "<emoji document_id=5841359499146825803>⏳</emoji> <b>Загрузка...</b>",
        "searching": "<emoji document_id=5841359499146825803>🔍</emoji> <b>Поиск треков в SoundCloud...</b>",
        "sc_select": "<emoji document_id=6007938409857815902>🎧</emoji> <b>Выберите нужный трек из SoundCloud:</b>",
        "downloading": "<emoji document_id=5841359499146825803>📥</emoji> <b>Скачивание файла...</b>",
        "error": "<emoji document_id=5778527486270770928>❌</emoji> <b>Ошибка:</b> <code>{}</code>",
        "bad_url": "<emoji document_id=5778527486270770928>❌</emoji> <b>Неподдерживаемая ссылка.</b>",
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "sc_client_id",
                _CLIENT_ID,
                "SoundCloud Public Client ID (если перестанет искать, обновите)",
                validator=loader.validators.String(),
            )
        )
        self._sc_cache: Dict[str, List[dict]] = {}

    async def client_ready(self, client, db):
        self.client = client

    # ==========================================
    #             SOUNDCLOUD (.tr)
    # ==========================================

    @loader.command(
        ru_doc="[ссылка/название] — Скачать трек из SoundCloud. Если указано название, выдаст 3 варианта.",
        en_doc="[link/query] — Download track from SoundCloud. Gives 3 options if queried by name.",
    )
    async def trcmd(self, message: Message):
        args = utils.get_args_raw(message) or await self._get_reply_text(message)
        if not args:
            return await utils.answer(message, self.strings("no_args"))

        if "soundcloud.com" in args:
            await utils.answer(message, self.strings("downloading"))
            await self._download_and_send_sc(message, args)
        else:
            await utils.answer(message, self.strings("searching"))
            tracks = await self._search_sc(args)
            if not tracks:
                return await utils.answer(message, self.strings("error").format("Ничего не найдено"))

            msg_id = f"{message.chat_id}_{message.id}"
            self._sc_cache[msg_id] = tracks

            buttons = []
            for idx, track in enumerate(tracks):
                title = track.get("title", "Unknown Track")[:30]
                artist = track.get("user", {}).get("username", "Unknown Artist")[:15]
                buttons.append(
                    [
                        {
                            "text": f"🎵 {artist} - {title}",
                            "callback": self._sc_callback,
                            "args": (msg_id, idx),
                        }
                    ]
                )

            await self.inline.form(
                message=message,
                text=self.strings("sc_select"),
                reply_markup=buttons,
            )

    async def _search_sc(self, query: str) -> List[dict]:
        def sync_search():
            try:
                r = requests.get(
                    f"{_SC_API}/tracks",
                    params={
                        "q": query,
                        "client_id": self.config["sc_client_id"],
                        "limit": 3,
                    },
                    timeout=10,
                )
                if r.status_code == 200:
                    return r.json().get("collection", [])
            except Exception as e:
                logger.error(f"SC Search error: {e}")
            return []

        return await utils.run_sync(sync_search)

    async def _sc_callback(self, call):
        msg_id, idx = call.args
        tracks = self._sc_cache.get(msg_id)

        if not tracks or idx >= len(tracks):
            return await call.answer("Сессия истекла или трек не найден", alert=True)

        track_url = tracks[idx].get("permalink_url")
        await call.edit(self.strings("downloading"))

        await self._download_and_send_sc(call.message, track_url, is_callback=True)

    async def _download_and_send_sc(self, message: Message, url: str, is_callback: bool = False):
        try:
            opts = {
                **YTDL_OPTS_BASE,
                "format": "bestaudio/best",
                "outtmpl": "%(title)s.%(ext)s",
            }

            def download():
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    filename = ydl.prepare_filename(info)
                    return filename, info

            filename, info = await utils.run_sync(download)

            with open(filename, "rb") as f:
                audio_data = io.BytesIO(f.read())
            audio_data.name = filename

            title = info.get("title", "Track")
            artist = info.get("uploader", "SoundCloud")

            await self.client.send_file(
                message.chat_id,
                file=audio_data,
                voice=False,
                attributes=[],
                caption=f"⚡ <b>{artist} — {title}</b>",
            )

            if is_callback:
                await message.delete()
            else:
                await message.delete()

            import os
            if os.path.exists(filename):
                os.remove(filename)

        except Exception as e:
            logger.exception("SC Download failed")
            await utils.answer(message, self.strings("error").format(str(e)))

    # ==========================================
    #               TIKTOK (.tt)
    # ==========================================

    @loader.command(
        ru_doc="[ссылка] — Скачать видео из TikTok без водяного знака.",
        en_doc="[link] — Download TikTok video without watermark.",
    )
    async def ttcmd(self, message: Message):
        url = utils.get_args_raw(message) or await self._get_reply_text(message)
        if not url:
            return await utils.answer(message, self.strings("no_args"))

        if "tiktok.com" not in url:
            return await utils.answer(message, self.strings("bad_url"))

        await utils.answer(message, self.strings("loading"))
        video_url, api_res = await self._parse_tt(url)

        if not video_url:
            try:
                await utils.answer(message, self.strings("downloading"))
                bytes_data = await utils.run_sync(lambda: requests.get(api_res).content)
                video_io = io.BytesIO(bytes_data)
                video_io.name = "tiktok.mp4"
                await self.client.send_file(message.chat_id, file=video_io)
                return await message.delete()
            except Exception:
                return await utils.answer(message, self.strings("error").format("Не удалось извлечь видео"))

        try:
            await utils.answer(message, self.strings("downloading"))
            bytes_data = await utils.run_sync(lambda: requests.get(video_url).content)
            video_io = io.BytesIO(bytes_data)
            video_io.name = "tiktok.mp4"

            await self.client.send_file(message.chat_id, file=video_io)
            await message.delete()
        except Exception as e:
            await utils.answer(message, self.strings("error").format(str(e)))

    async def _parse_tt(self, url: str) -> Tuple[Union[str, bool], str]:
        def sync_tt():
            try:
                actual_url = requests.head(url, allow_redirects=True).url
                try:
                    query = parse_qs(E(actual_url).query)
                    item_id = query.get("share_item_id")[0]
                except Exception:
                    item_id = "".join(re.findall("[0-9]", E(actual_url).path.split("/")[-1]))

                api_url = f"https://api-va.tiktokv.com/aweme/v1/multi/aweme/detail/?aweme_ids=%5B{item_id}%5D"
                res = requests.get(api_url).json()
                details = res.get("aweme_details")
                if not details:
                    return False, api_url
                return details[0]["video"]["bit_rate"][0]["play_addr"]["url_list"][-1], api_url
            except Exception:
                return False, url

        return await utils.run_sync(sync_tt)

    # ==========================================
    #               YOUTUBE (.yt)
    # ==========================================

    @loader.command(
        ru_doc="[ссылка] — Скачать видео с YouTube в качестве до 1080p MP4.",
        en_doc="[link] — Download YouTube video in quality up to 1080p MP4.",
    )
    async def ytcmd(self, message: Message):
        url = utils.get_args_raw(message) or await self._get_reply_text(message)
        if not url:
            return await utils.answer(message, self.strings("no_args"))

        if "youtube.com" not in url and "youtu.be" not in url:
            return await utils.answer(message, self.strings("bad_url"))

        await utils.answer(message, self.strings("downloading"))

        try:
            opts = {
                **YTDL_OPTS_BASE,
                "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best",
                "outtmpl": "yt_video_%(id)s.%(ext)s",
                "merge_output_format": "mp4",
            }

            def download_yt():
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return ydl.prepare_filename(info), info

            filename, info = await utils.run_sync(download_yt)

            await utils.answer(message, f"📤 <b>Отправка видео:</b> {info.get('title', 'YouTube Video')}")
            
            await self.client.send_file(
                message.chat_id,
                file=filename,
                caption=f"🎬 <b>{info.get('title')}</b>",
                supports_streaming=True,
            )
            
            await message.delete()
            import os
            if os.path.exists(filename):
                os.remove(filename)

        except Exception as e:
            logger.exception("YouTube Download failed")
            await utils.answer(message, self.strings("error").format(str(e)))

    # ==========================================
    #             УТИЛИТЫ МОДУЛЯ
    # ==========================================

    async def _get_reply_text(self, message: Message) -> Optional[str]:
        reply = await message.get_reply_message()
        if reply and reply.raw_text:
            return reply.raw_text.strip()
        return None
