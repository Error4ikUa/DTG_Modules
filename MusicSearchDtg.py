# meta developer: @DeathTerror
# meta name: MusicSearchDtg
# requires: aiohttp

import asyncio
import base64
import html
import re
import time
from difflib import SequenceMatcher
from urllib.parse import quote, urlparse, parse_qs

import aiohttp

from .. import loader, utils

BLUE = "🔵"
OK = "🔷"
INFO = "💎"
MUSIC = "🎧"
SPOTIFY = "🟢"
APPLE = "🍎"
SC = "🟠"
WARN = "🌀"

PROVIDER_EMOJI = {"spotify": SPOTIFY, "apple": APPLE, "soundcloud": SC}


class MusicSearchDtgMod(loader.Module):
    """🔵 MusicSearchDtg — поиск треков Spotify / Apple Music / SoundCloud."""

    strings = {
        "name": "MusicSearchDtg",
        "description": "🔵 Поиск песен по словам или ссылке: Apple Music, Spotify, SoundCloud, топ-3 с инлайн-кнопками.",
        "help": (
            "🔵 <b>Модуль MusicSearchDtg</b>\n"
            "Ищет треки по названию/артисту или по ссылке и кидает красивую карточку в чат.\n\n"
            "<b>🎧 Поиск</b>\n"
            "<code>.tr миражи</code> — найти 3 самых похожих трека\n"
            "<code>.tr ссылка</code> — сразу показать трек по ссылке\n\n"
            "<b>🔷 API ключи</b>\n"
            "<code>.trspotify client_id client_secret</code> — Spotify Web API\n"
            "<code>.trsoundcloud token</code> — SoundCloud API token\n"
            "<code>.trstatus</code> — статус ключей\n\n"
            "<b>💎 Логика</b>\n"
            "Apple/iTunes Search работает без токена и используется как fallback. Spotify и SoundCloud работают, если заданы ключи.\n"
            "Полный аудиофайл модуль не ворует: отправляет ссылку/превью/карточку, потому что Spotify/Apple/SoundCloud не дают легально скачивать полный трек через API.\n"
        ),
    }
    strings_ru = strings

    def __init__(self):
        self.search_cache = {}
        self.spotify_cache = {"token": "", "expires": 0}

    async def client_ready(self, client, db):
        self.client = client
        self.db = db

    def cfg(self):
        return self.db.get("MusicSearchDtg", "cfg", {
            "spotify_client_id": "",
            "spotify_client_secret": "",
            "soundcloud_token": "",
            "default_limit": 3,
        })

    def save_cfg(self, cfg):
        self.db.set("MusicSearchDtg", "cfg", cfg)

    def raw(self, message, args=None):
        if args is not None:
            return str(args or "").strip()
        try:
            return utils.get_args_raw(message).strip()
        except Exception:
            return ""

    def mask(self, value):
        if not value:
            return "не задан"
        if len(value) <= 10:
            return "скрыт"
        return f"{value[:5]}...{value[-4:]}"

    def norm(self, text):
        text = (text or "").lower().strip()
        text = re.sub(r"[^a-zа-яёіїєґ0-9\s]+", " ", text, flags=re.I)
        return re.sub(r"\s+", " ", text).strip()

    def score(self, query, title, artist):
        q = self.norm(query)
        target = self.norm(f"{title} {artist}")
        title_n = self.norm(title)
        if not q or not target:
            return 0
        ratio = SequenceMatcher(None, q, target).ratio()
        title_ratio = SequenceMatcher(None, q, title_n).ratio()
        bonus = 0.35 if q in title_n else 0
        return ratio + title_ratio + bonus

    def dedupe_sort(self, query, results, limit=3):
        seen = set()
        clean = []
        for item in results:
            key = self.norm(f"{item.get('title')} {item.get('artist')}")
            if not key or key in seen:
                continue
            seen.add(key)
            item["score"] = self.score(query, item.get("title"), item.get("artist"))
            clean.append(item)
        clean.sort(key=lambda x: x.get("score", 0), reverse=True)
        return clean[:limit]

    def is_url(self, value):
        return bool(re.match(r"https?://", value or "", re.I))

    def provider_from_url(self, url):
        host = urlparse(url).netloc.lower()
        if "spotify.com" in host:
            return "spotify"
        if "music.apple.com" in host or "itunes.apple.com" in host:
            return "apple"
        if "soundcloud.com" in host:
            return "soundcloud"
        return "unknown"

    def card(self, item):
        emoji = PROVIDER_EMOJI.get(item.get("provider"), MUSIC)
        title = html.escape(item.get("title") or "Unknown")
        artist = html.escape(item.get("artist") or "Unknown")
        album = html.escape(item.get("album") or "")
        provider = html.escape(item.get("provider", "music"))
        url = html.escape(item.get("url") or "")
        preview = html.escape(item.get("preview_url") or "")
        text = f"{emoji} <b>{title}</b>\n{BLUE} <b>Артист:</b> <code>{artist}</code>\n"
        if album:
            text += f"{INFO} <b>Альбом:</b> <code>{album}</code>\n"
        text += f"{MUSIC} <b>Источник:</b> <code>{provider}</code>\n"
        if preview and preview != url:
            text += f"{OK} <b>Preview:</b> <code>{preview}</code>\n"
        if url:
            text += f"\n<a href=\"{url}\">🔵 Открыть трек</a>"
        return text

    def list_text(self, query, results):
        lines = [f"{MUSIC} <b>Нашёл 3 подходящие песни по запросу:</b> <code>{html.escape(query)}</code>\n"]
        for i, item in enumerate(results, 1):
            emoji = PROVIDER_EMOJI.get(item.get("provider"), MUSIC)
            title = html.escape(item.get("title") or "Unknown")
            artist = html.escape(item.get("artist") or "Unknown")
            provider = html.escape(item.get("provider") or "music")
            lines.append(f"<b>{i}.</b> {emoji} <b>{title}</b> — <code>{artist}</code> <i>({provider})</i>")
        lines.append("\n🔷 <i>Выбери кнопкой нужный трек.</i>")
        return "\n".join(lines)

    def buttons_for_result(self, item):
        buttons = []
        if item.get("url"):
            buttons.append([{"text": "🔵 Открыть трек", "url": item["url"]}])
        if item.get("preview_url") and item.get("preview_url") != item.get("url"):
            buttons.append([{"text": "🎧 Preview", "url": item["preview_url"]}])
        return buttons or None

    async def http_get_json(self, url, headers=None):
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers or {}) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {}
                if resp.status >= 400:
                    return None, f"HTTP {resp.status}: {str(data)[:300]}"
                return data, None

    async def apple_search(self, query, limit=5):
        url = f"https://itunes.apple.com/search?term={quote(query)}&media=music&entity=song&limit={int(limit)}"
        data, err = await self.http_get_json(url)
        if err or not data:
            return []
        out = []
        for x in data.get("results", []):
            out.append({
                "provider": "apple",
                "title": x.get("trackName") or "Unknown",
                "artist": x.get("artistName") or "Unknown",
                "album": x.get("collectionName") or "",
                "url": x.get("trackViewUrl") or x.get("collectionViewUrl") or "",
                "preview_url": x.get("previewUrl") or "",
                "artwork": x.get("artworkUrl100") or "",
            })
        return out

    async def apple_lookup(self, url):
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        track_id = qs.get("i", [None])[0]
        if not track_id:
            m = re.search(r"/(\d+)(?:\?|$)", parsed.path)
            if m:
                track_id = m.group(1)
        if not track_id:
            return None
        data, err = await self.http_get_json(f"https://itunes.apple.com/lookup?id={quote(track_id)}&entity=song")
        if err or not data:
            return None
        items = data.get("results", [])
        song = next((x for x in items if x.get("wrapperType") == "track"), items[0] if items else None)
        if not song:
            return None
        return {
            "provider": "apple",
            "title": song.get("trackName") or song.get("collectionName") or "Apple Music",
            "artist": song.get("artistName") or "Unknown",
            "album": song.get("collectionName") or "",
            "url": song.get("trackViewUrl") or url,
            "preview_url": song.get("previewUrl") or "",
            "artwork": song.get("artworkUrl100") or "",
        }

    async def spotify_token(self):
        cfg = self.cfg()
        cid = cfg.get("spotify_client_id", "").strip()
        secret = cfg.get("spotify_client_secret", "").strip()
        if not cid or not secret:
            return None
        if self.spotify_cache.get("token") and self.spotify_cache.get("expires", 0) > time.time() + 30:
            return self.spotify_cache["token"]
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post("https://accounts.spotify.com/api/token", data="grant_type=client_credentials", headers=headers) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    return None
        token = data.get("access_token")
        if token:
            self.spotify_cache = {"token": token, "expires": time.time() + int(data.get("expires_in", 3600))}
        return token

    def spotify_item(self, x):
        artists = ", ".join(a.get("name", "") for a in x.get("artists", []) if a.get("name")) or "Unknown"
        album = x.get("album", {}) or {}
        return {
            "provider": "spotify",
            "title": x.get("name") or "Unknown",
            "artist": artists,
            "album": album.get("name") or "",
            "url": (x.get("external_urls") or {}).get("spotify") or "",
            "preview_url": x.get("preview_url") or "",
            "artwork": ((album.get("images") or [{}])[0]).get("url", "") if album.get("images") else "",
        }

    async def spotify_search(self, query, limit=5):
        token = await self.spotify_token()
        if not token:
            return []
        url = f"https://api.spotify.com/v1/search?q={quote(query)}&type=track&limit={int(limit)}"
        data, err = await self.http_get_json(url, headers={"Authorization": f"Bearer {token}"})
        if err or not data:
            return []
        return [self.spotify_item(x) for x in data.get("tracks", {}).get("items", [])]

    async def spotify_lookup(self, url):
        token = await self.spotify_token()
        if not token:
            return None
        m = re.search(r"/track/([A-Za-z0-9]+)", url)
        if not m:
            return None
        data, err = await self.http_get_json(f"https://api.spotify.com/v1/tracks/{m.group(1)}", headers={"Authorization": f"Bearer {token}"})
        if err or not data:
            return None
        return self.spotify_item(data)

    def soundcloud_headers(self):
        token = self.cfg().get("soundcloud_token", "").strip()
        return {"Authorization": f"OAuth {token}"} if token else None

    def soundcloud_item(self, x):
        user = x.get("user", {}) or {}
        permalink = x.get("permalink_url") or x.get("uri") or ""
        return {
            "provider": "soundcloud",
            "title": x.get("title") or "Unknown",
            "artist": user.get("username") or x.get("publisher_metadata", {}).get("artist") or "Unknown",
            "album": x.get("label_name") or "",
            "url": permalink,
            "preview_url": permalink,
            "artwork": x.get("artwork_url") or "",
        }

    async def soundcloud_search(self, query, limit=5):
        headers = self.soundcloud_headers()
        if not headers:
            return []
        url = f"https://api.soundcloud.com/tracks?q={quote(query)}&limit={int(limit)}"
        data, err = await self.http_get_json(url, headers=headers)
        if err or not data:
            return []
        if isinstance(data, dict):
            data = data.get("collection", [])
        return [self.soundcloud_item(x) for x in data if isinstance(x, dict)]

    async def soundcloud_oembed(self, url):
        data, err = await self.http_get_json(f"https://soundcloud.com/oembed?format=json&url={quote(url)}")
        if err or not data:
            return None
        title = data.get("title") or "SoundCloud"
        artist = "SoundCloud"
        if " by " in title:
            left, right = title.rsplit(" by ", 1)
            title, artist = left.strip(), right.strip()
        return {
            "provider": "soundcloud",
            "title": title,
            "artist": artist,
            "album": "",
            "url": url,
            "preview_url": url,
            "artwork": data.get("thumbnail_url") or "",
        }

    async def soundcloud_lookup(self, url):
        headers = self.soundcloud_headers()
        if headers:
            data, err = await self.http_get_json(f"https://api.soundcloud.com/resolve?url={quote(url)}", headers=headers)
            if not err and isinstance(data, dict) and data.get("kind") == "track":
                return self.soundcloud_item(data)
        return await self.soundcloud_oembed(url)

    async def search_all(self, query):
        tasks = [self.apple_search(query, 5), self.spotify_search(query, 5), self.soundcloud_search(query, 5)]
        results = []
        for part in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(part, list):
                results.extend(part)
        return self.dedupe_sort(query, results, 3)

    async def lookup_link(self, url):
        provider = self.provider_from_url(url)
        if provider == "spotify":
            return await self.spotify_lookup(url)
        if provider == "apple":
            return await self.apple_lookup(url)
        if provider == "soundcloud":
            return await self.soundcloud_lookup(url)
        return None

    async def trcmd(self, message, args=None):
        """[название/артист/ссылка] — найти трек."""
        query = self.raw(message, args)
        if not query:
            return await utils.answer(message, f"{BLUE} <b>Формат:</b> <code>.tr миражи</code> или <code>.tr ссылка_на_трек</code>")

        loading = await utils.answer(message, f"{MUSIC} <b>Ищу трек...</b>")

        if self.is_url(query):
            item = await self.lookup_link(query)
            if not item:
                return await utils.answer(loading, f"{WARN} <b>Не смог распознать ссылку.</b>\n{INFO} Для Spotify нужны <code>.trspotify client_id client_secret</code>, для SoundCloud желательно <code>.trsoundcloud token</code>.")
            buttons = self.buttons_for_result(item)
            try:
                return await self.inline.form(self.card(item), message=loading, reply_markup=buttons, ttl=3600)
            except Exception:
                return await utils.answer(loading, self.card(item))

        results = await self.search_all(query)
        if not results:
            return await utils.answer(loading, f"{WARN} <b>Ничего не нашёл.</b>\n{INFO} Apple работает без токена. Для Spotify/SoundCloud задай ключи через <code>.trspotify</code> и <code>.trsoundcloud</code>.")

        key = f"{message.chat_id}:{message.id}:{int(time.time())}"
        self.search_cache[key] = {"query": query, "results": results, "time": time.time()}
        markup = []
        for i, item in enumerate(results):
            emoji = PROVIDER_EMOJI.get(item.get("provider"), MUSIC)
            title = (item.get("title") or "Unknown")[:28]
            artist = (item.get("artist") or "Unknown")[:20]
            markup.append([{"text": f"{emoji} {i + 1}. {title} — {artist}", "callback": self.select_track, "args": (key, i)}])
        try:
            await self.inline.form(self.list_text(query, results), message=loading, reply_markup=markup, ttl=3600)
        except Exception:
            text = self.list_text(query, results)
            text += "\n\n" + "\n".join(f"{i+1}) {html.escape(x.get('url') or '')}" for i, x in enumerate(results))
            await utils.answer(loading, text)

    async def select_track(self, call, key, index):
        pack = self.search_cache.get(key)
        if not pack or time.time() - pack.get("time", 0) > 3600:
            return await call.answer("Поиск устарел, запусти .tr ещё раз", show_alert=True)
        try:
            item = pack["results"][int(index)]
        except Exception:
            return await call.answer("Трек не найден в кеше", show_alert=True)
        await call.edit(self.card(item), reply_markup=self.buttons_for_result(item))

    async def trspotifycmd(self, message, args=None):
        """client_id client_secret — сохранить Spotify API ключи."""
        parts = self.raw(message, args).split(maxsplit=1)
        cfg = self.cfg()
        if len(parts) < 2:
            return await utils.answer(message, f"{SPOTIFY} <b>Формат:</b> <code>.trspotify client_id client_secret</code>\n{INFO} Создай app в Spotify Developer Dashboard.")
        cfg["spotify_client_id"] = parts[0].strip()
        cfg["spotify_client_secret"] = parts[1].strip()
        self.spotify_cache = {"token": "", "expires": 0}
        self.save_cfg(cfg)
        await utils.answer(message, f"{OK} <b>Spotify ключи сохранены.</b>")

    async def trsoundcloudcmd(self, message, args=None):
        """token — сохранить SoundCloud API token."""
        token = self.raw(message, args)
        cfg = self.cfg()
        if not token:
            return await utils.answer(message, f"{SC} <b>Формат:</b> <code>.trsoundcloud token</code>\n{INFO} Нужен SoundCloud API/OAuth token. Без него ссылки SoundCloud частично работают через oEmbed, но поиск может не работать.")
        cfg["soundcloud_token"] = token
        self.save_cfg(cfg)
        await utils.answer(message, f"{OK} <b>SoundCloud token сохранён.</b>")

    async def trstatuscmd(self, message, args=None):
        """— статус ключей MusicSearchDtg."""
        cfg = self.cfg()
        await utils.answer(
            message,
            f"{MUSIC} <b>MusicSearchDtg status</b>\n"
            f"{APPLE} <b>Apple/iTunes:</b> <code>без токена</code>\n"
            f"{SPOTIFY} <b>Spotify client_id:</b> <code>{html.escape(self.mask(cfg.get('spotify_client_id', '')))}</code>\n"
            f"{SPOTIFY} <b>Spotify secret:</b> <code>{html.escape(self.mask(cfg.get('spotify_client_secret', '')))}</code>\n"
            f"{SC} <b>SoundCloud token:</b> <code>{html.escape(self.mask(cfg.get('soundcloud_token', '')))}</code>"
        )
