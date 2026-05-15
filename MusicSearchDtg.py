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
import secrets
import tempfile
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs

import aiohttp
from telethon import Button, events

from deathtg.command import command

BLUE = "🔵"
MUSIC = "🎧"
APPLE = "🍎"
SPOTIFY = "🟢"
SC = "🟠"
OK = "🔷"
INFO = "💎"
WARN = "🌀"

CONFIG_FILE = Path(__file__).with_suffix(".json")
SPOTIFY_CACHE = {"token": "", "expires": 0}
SEARCH_CACHE: dict[str, dict] = {}
CALLBACK_REGISTERED: set[int] = set()
PROVIDER_EMOJI = {"apple": APPLE, "spotify": SPOTIFY, "soundcloud": SC}

DEFAULT_CFG = {
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "soundcloud_token": "",
}


def load_cfg() -> dict:
    try:
        if CONFIG_FILE.exists():
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg = DEFAULT_CFG.copy()
            cfg.update(data if isinstance(data, dict) else {})
            return cfg
    except Exception:
        pass
    return DEFAULT_CFG.copy()


def save_cfg(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def argline(args) -> str:
    return " ".join(str(x) for x in args).strip() if isinstance(args, (list, tuple)) else str(args or "").strip()


def mask(value: str) -> str:
    if not value:
        return "не задан"
    if len(value) <= 10:
        return "скрыт"
    return f"{value[:5]}...{value[-4:]}"


def norm(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-zа-яёіїєґ0-9\s]+", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()


def score(query: str, title: str, artist: str) -> float:
    q = norm(query)
    title_n = norm(title)
    target = norm(f"{title} {artist}")
    if not q or not target:
        return 0
    return SequenceMatcher(None, q, target).ratio() + SequenceMatcher(None, q, title_n).ratio() + (0.35 if q in title_n else 0)


def dedupe_sort(query: str, results: list[dict], limit: int = 3) -> list[dict]:
    seen = set()
    clean = []
    for item in results:
        key = norm(f"{item.get('title')} {item.get('artist')}")
        if not key or key in seen:
            continue
        seen.add(key)
        item["score"] = score(query, item.get("title", ""), item.get("artist", ""))
        clean.append(item)
    clean.sort(key=lambda x: x.get("score", 0), reverse=True)
    return clean[:limit]


def is_url(value: str) -> bool:
    return bool(re.match(r"https?://", value or "", re.I))


def provider_from_url(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "spotify.com" in host:
        return "spotify"
    if "music.apple.com" in host or "itunes.apple.com" in host:
        return "apple"
    if "soundcloud.com" in host:
        return "soundcloud"
    return "unknown"


def safe_name(text: str) -> str:
    text = re.sub(r"[^a-zA-Zа-яА-ЯёЁіїєґІЇЄҐ0-9 ._-]+", "", text or "track").strip()
    text = re.sub(r"\s+", " ", text)
    return (text or "track")[:80]


def item_name(item: dict) -> str:
    return f"{item.get('artist') or 'Unknown'} - {item.get('title') or 'Track'}"


def list_text(query: str, results: list[dict]) -> str:
    lines = [f"{MUSIC} <b>Нашёл 3 подходящие песни по запросу:</b> <code>{html.escape(query)}</code>\n"]
    for i, item in enumerate(results, 1):
        emoji = PROVIDER_EMOJI.get(item.get("provider"), MUSIC)
        title = html.escape(item.get("title") or "Unknown")
        artist = html.escape(item.get("artist") or "Unknown")
        provider = html.escape(item.get("provider") or "music")
        lines.append(f"<b>{i}.</b> {emoji} <b>{title}</b> — <code>{artist}</code> <i>({provider})</i>")
    lines.append("\n🔷 <i>Нажми кнопку — трек отправится в чат.</i>")
    return "\n".join(lines)


def picker_buttons(key: str, results: list[dict]):
    rows = []
    for i, item in enumerate(results, 1):
        emoji = PROVIDER_EMOJI.get(item.get("provider"), MUSIC)
        title = (item.get("title") or "Unknown")[:25]
        artist = (item.get("artist") or "Unknown")[:18]
        rows.append([Button.inline(f"{emoji} {i}. {title} — {artist}", data=f"ms:{key}:{i - 1}".encode())])
    return rows or None


def open_button(item: dict):
    return [[Button.url("🔵 Открыть трек", item["url"])] ] if item.get("url") else None


def get_app(event):
    return getattr(getattr(event, "client", None), "deathtg_app", None)


def get_bot_client(event):
    app = get_app(event)
    if not app:
        return None
    for name in ("bot_client", "bot", "inline_bot", "assistant_bot"):
        client = getattr(app, name, None)
        if client:
            return client
    inline = getattr(app, "inline", None)
    if inline:
        for name in ("bot_client", "bot", "client"):
            client = getattr(inline, name, None)
            if client:
                return client
    return None


def ensure_callback_handler(client) -> None:
    if not client:
        return
    key = id(client)
    if key in CALLBACK_REGISTERED:
        return
    try:
        client.add_event_handler(track_callback_handler, events.CallbackQuery(pattern=b"^ms:"))
        CALLBACK_REGISTERED.add(key)
    except Exception:
        pass


async def send_picker(event, text: str, results: list[dict]):
    key = secrets.token_urlsafe(6)
    SEARCH_CACHE[key] = {"results": results, "time": time.time(), "chat_id": event.chat_id}
    buttons = picker_buttons(key, results)

    bot_client = get_bot_client(event)
    ensure_callback_handler(bot_client)
    ensure_callback_handler(event.client)

    if bot_client:
        try:
            sent = await bot_client.send_message(event.chat_id, text, buttons=buttons, parse_mode="html", link_preview=False)
            try:
                await event.delete()
            except Exception:
                pass
            return sent
        except Exception:
            pass

    app = get_app(event)
    inline = getattr(app, "inline", None) if app else None
    if inline:
        for method_name in ("form", "send", "answer"):
            method = getattr(inline, method_name, None)
            if not method:
                continue
            try:
                if method_name == "form":
                    return await method(text, message=event, reply_markup=buttons, ttl=3600)
                return await method(event, text, buttons=buttons, parse_mode="html", link_preview=False)
            except Exception:
                pass

    return await event.edit(text, buttons=buttons, parse_mode="html", link_preview=False)


async def http_get_json(url: str, headers: dict | None = None):
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


async def download_preview(url: str, item: dict | None = None) -> str | None:
    if not url or not url.startswith("http"):
        return None
    suffix = Path(urlparse(url).path).suffix or ".m4a"
    if len(suffix) > 8:
        suffix = ".m4a"
    filename = safe_name(item_name(item or {})) + suffix
    path = str(Path(tempfile.gettempdir()) / filename)
    try:
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    return None
                total = 0
                with open(path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > 12 * 1024 * 1024:
                            return None
                        f.write(chunk)
        return path if os.path.getsize(path) > 0 else None
    except Exception:
        return None


async def send_audio_only(client, chat_id, item: dict) -> bool:
    preview = item.get("preview_url") or ""
    if not preview or preview == item.get("url"):
        return False
    path = await download_preview(preview, item)
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


async def track_callback_handler(event):
    try:
        data = (event.data or b"").decode(errors="ignore")
        _, key, idx_raw = data.split(":", 2)
        pack = SEARCH_CACHE.get(key)
        if not pack or time.time() - pack.get("time", 0) > 3600:
            await event.answer("Поиск устарел, запусти .tr ещё раз", alert=True)
            return
        item = pack["results"][int(idx_raw)]
    except Exception:
        await event.answer("Не нашёл этот трек в кеше", alert=True)
        return

    await event.answer("Кидаю трек...", alert=False)
    ok = await send_audio_only(event.client, event.chat_id, item)
    if ok:
        try:
            await event.delete()
        except Exception:
            pass
        return

    await event.answer("У этого трека нет аудио-превью", alert=True)


async def apple_search(query: str, limit: int = 5) -> list[dict]:
    url = f"https://itunes.apple.com/search?term={quote(query)}&media=music&entity=song&limit={int(limit)}"
    data, err = await http_get_json(url)
    if err or not data:
        return []
    return [{
        "provider": "apple",
        "title": x.get("trackName") or "Unknown",
        "artist": x.get("artistName") or "Unknown",
        "album": x.get("collectionName") or "",
        "url": x.get("trackViewUrl") or x.get("collectionViewUrl") or "",
        "preview_url": x.get("previewUrl") or "",
    } for x in data.get("results", [])]


async def apple_lookup(url: str):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    track_id = qs.get("i", [None])[0]
    if not track_id:
        m = re.search(r"/(\d+)(?:\?|$)", parsed.path)
        if m:
            track_id = m.group(1)
    if not track_id:
        return None
    data, err = await http_get_json(f"https://itunes.apple.com/lookup?id={quote(track_id)}&entity=song")
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
    }


async def spotify_token():
    cfg = load_cfg()
    cid = cfg.get("spotify_client_id", "").strip()
    secret = cfg.get("spotify_client_secret", "").strip()
    if not cid or not secret:
        return None
    if SPOTIFY_CACHE.get("token") and SPOTIFY_CACHE.get("expires", 0) > time.time() + 30:
        return SPOTIFY_CACHE["token"]
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
        SPOTIFY_CACHE.update({"token": token, "expires": time.time() + int(data.get("expires_in", 3600))})
    return token


def spotify_item(x: dict) -> dict:
    artists = ", ".join(a.get("name", "") for a in x.get("artists", []) if a.get("name")) or "Unknown"
    album = x.get("album", {}) or {}
    return {
        "provider": "spotify",
        "title": x.get("name") or "Unknown",
        "artist": artists,
        "album": album.get("name") or "",
        "url": (x.get("external_urls") or {}).get("spotify") or "",
        "preview_url": x.get("preview_url") or "",
    }


async def spotify_search(query: str, limit: int = 5) -> list[dict]:
    token = await spotify_token()
    if not token:
        return []
    data, err = await http_get_json(f"https://api.spotify.com/v1/search?q={quote(query)}&type=track&limit={int(limit)}", headers={"Authorization": f"Bearer {token}"})
    if err or not data:
        return []
    return [spotify_item(x) for x in data.get("tracks", {}).get("items", [])]


async def spotify_lookup(url: str):
    token = await spotify_token()
    if not token:
        return None
    m = re.search(r"/track/([A-Za-z0-9]+)", url)
    if not m:
        return None
    data, err = await http_get_json(f"https://api.spotify.com/v1/tracks/{m.group(1)}", headers={"Authorization": f"Bearer {token}"})
    return None if err or not data else spotify_item(data)


def soundcloud_headers():
    token = load_cfg().get("soundcloud_token", "").strip()
    return {"Authorization": f"OAuth {token}"} if token else None


def soundcloud_item(x: dict) -> dict:
    user = x.get("user", {}) or {}
    return {
        "provider": "soundcloud",
        "title": x.get("title") or "Unknown",
        "artist": user.get("username") or x.get("publisher_metadata", {}).get("artist") or "Unknown",
        "album": x.get("label_name") or "",
        "url": x.get("permalink_url") or x.get("uri") or "",
        "preview_url": "",
    }


async def soundcloud_search(query: str, limit: int = 5) -> list[dict]:
    headers = soundcloud_headers()
    if not headers:
        return []
    data, err = await http_get_json(f"https://api.soundcloud.com/tracks?q={quote(query)}&limit={int(limit)}", headers=headers)
    if err or not data:
        return []
    if isinstance(data, dict):
        data = data.get("collection", [])
    return [soundcloud_item(x) for x in data if isinstance(x, dict)]


async def soundcloud_lookup(url: str):
    headers = soundcloud_headers()
    if headers:
        data, err = await http_get_json(f"https://api.soundcloud.com/resolve?url={quote(url)}", headers=headers)
        if not err and isinstance(data, dict) and data.get("kind") == "track":
            return soundcloud_item(data)
    return {"provider": "soundcloud", "title": "SoundCloud", "artist": "Unknown", "album": "", "url": url, "preview_url": ""}


async def search_all(query: str) -> list[dict]:
    tasks = [apple_search(query, 5), spotify_search(query, 5), soundcloud_search(query, 5)]
    results = []
    for part in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(part, list):
            results.extend(part)
    return dedupe_sort(query, results, 3)


async def lookup_link(url: str):
    provider = provider_from_url(url)
    if provider == "spotify":
        return await spotify_lookup(url)
    if provider == "apple":
        return await apple_lookup(url)
    if provider == "soundcloud":
        return await soundcloud_lookup(url)
    return None


@command("tr", description="Поиск трека", usage=".tr миражи")
async def tr_cmd(event, args: list[str]) -> None:
    query = argline(args)
    if not query:
        await event.edit(f"{BLUE} <b>Формат:</b> <code>.tr миражи</code> или <code>.tr ссылка_на_трек</code>", parse_mode="html")
        return

    await event.edit(f"{MUSIC} <b>Ищу трек...</b>", parse_mode="html")

    if is_url(query):
        item = await lookup_link(query)
        if not item:
            await event.edit(f"{WARN} <b>Не смог распознать ссылку.</b>", parse_mode="html")
            return
        if await send_audio_only(event.client, event.chat_id, item):
            try:
                await event.delete()
            except Exception:
                pass
            return
        await event.edit(f"{WARN} <b>У этого трека нет аудио-превью.</b>", parse_mode="html", buttons=open_button(item))
        return

    results = await search_all(query)
    if not results:
        await event.edit(f"{WARN} <b>Ничего не нашёл.</b>", parse_mode="html")
        return

    await send_picker(event, list_text(query, results), results)


@command("trspotify", description="Сохранить Spotify API ключи", usage=".trspotify client_id client_secret")
async def trspotify_cmd(event, args: list[str]) -> None:
    parts = argline(args).split(maxsplit=1)
    if len(parts) < 2:
        await event.edit(f"{SPOTIFY} <b>Формат:</b> <code>.trspotify client_id client_secret</code>", parse_mode="html")
        return
    cfg = load_cfg()
    cfg["spotify_client_id"] = parts[0].strip()
    cfg["spotify_client_secret"] = parts[1].strip()
    SPOTIFY_CACHE.update({"token": "", "expires": 0})
    save_cfg(cfg)
    await event.edit(f"{OK} <b>Spotify ключи сохранены.</b>", parse_mode="html")


@command("trsoundcloud", description="Сохранить SoundCloud API token", usage=".trsoundcloud token")
async def trsoundcloud_cmd(event, args: list[str]) -> None:
    token = argline(args)
    if not token:
        await event.edit(f"{SC} <b>Формат:</b> <code>.trsoundcloud token</code>", parse_mode="html")
        return
    cfg = load_cfg()
    cfg["soundcloud_token"] = token
    save_cfg(cfg)
    await event.edit(f"{OK} <b>SoundCloud token сохранён.</b>", parse_mode="html")


@command("trstatus", description="Статус MusicSearchDtg", usage=".trstatus")
async def trstatus_cmd(event, args: list[str]) -> None:
    cfg = load_cfg()
    await event.edit(
        f"{MUSIC} <b>MusicSearchDtg status</b>\n"
        f"{APPLE} <b>Apple/iTunes:</b> <code>без токена</code>\n"
        f"{SPOTIFY} <b>Spotify client_id:</b> <code>{html.escape(mask(cfg.get('spotify_client_id', '')))}</code>\n"
        f"{SPOTIFY} <b>Spotify secret:</b> <code>{html.escape(mask(cfg.get('spotify_client_secret', '')))}</code>\n"
        f"{SC} <b>SoundCloud token:</b> <code>{html.escape(mask(cfg.get('soundcloud_token', '')))}</code>",
        parse_mode="html",
    )
