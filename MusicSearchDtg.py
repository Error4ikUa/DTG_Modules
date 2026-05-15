# meta developer: @DeathTerror
# meta name: MusicSearchDtg
# requires: aiohttp

from __future__ import annotations

import asyncio
import base64
import html
import json
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote, urlparse, parse_qs

import aiohttp
from telethon import Button

from deathtg.command import command

BLUE = "🔵"
OK = "🔷"
INFO = "💎"
MUSIC = "🎧"
SPOTIFY = "🟢"
APPLE = "🍎"
SC = "🟠"
WARN = "🌀"

CONFIG_FILE = Path(__file__).with_suffix(".json")
SPOTIFY_CACHE = {"token": "", "expires": 0}
PROVIDER_EMOJI = {"spotify": SPOTIFY, "apple": APPLE, "soundcloud": SC}

DEFAULT_CFG = {
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "soundcloud_token": "",
    "default_limit": 3,
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
    if isinstance(args, (list, tuple)):
        return " ".join(str(x) for x in args).strip()
    return str(args or "").strip()


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
    target = norm(f"{title} {artist}")
    title_n = norm(title)
    if not q or not target:
        return 0
    ratio = SequenceMatcher(None, q, target).ratio()
    title_ratio = SequenceMatcher(None, q, title_n).ratio()
    bonus = 0.35 if q in title_n else 0
    return ratio + title_ratio + bonus


def dedupe_sort(query: str, results: list[dict], limit: int = 3) -> list[dict]:
    seen = set()
    clean = []
    for item in results:
        key = norm(f"{item.get('title')} {item.get('artist')}")
        if not key or key in seen:
            continue
        seen.add(key)
        item["score"] = score(query, item.get("title"), item.get("artist"))
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


def card(item: dict) -> str:
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


def result_buttons(results: list[dict]):
    rows = []
    for i, item in enumerate(results, 1):
        url = item.get("url")
        if not url:
            continue
        emoji = PROVIDER_EMOJI.get(item.get("provider"), MUSIC)
        title = (item.get("title") or "Unknown")[:25]
        artist = (item.get("artist") or "Unknown")[:18]
        rows.append([Button.url(f"{emoji} {i}. {title} — {artist}", url)])
    return rows or None


def single_buttons(item: dict):
    rows = []
    if item.get("url"):
        rows.append([Button.url("🔵 Открыть трек", item["url"])])
    if item.get("preview_url") and item.get("preview_url") != item.get("url"):
        rows.append([Button.url("🎧 Preview", item["preview_url"])])
    return rows or None


def list_text(query: str, results: list[dict]) -> str:
    lines = [f"{MUSIC} <b>Нашёл 3 подходящие песни по запросу:</b> <code>{html.escape(query)}</code>\n"]
    for i, item in enumerate(results, 1):
        emoji = PROVIDER_EMOJI.get(item.get("provider"), MUSIC)
        title = html.escape(item.get("title") or "Unknown")
        artist = html.escape(item.get("artist") or "Unknown")
        provider = html.escape(item.get("provider") or "music")
        lines.append(f"<b>{i}.</b> {emoji} <b>{title}</b> — <code>{artist}</code> <i>({provider})</i>")
    lines.append("\n🔷 <i>Кнопки ниже открывают найденные треки.</i>")
    return "\n".join(lines)


async def send_inline(event, text: str, *, buttons=None, link_preview=False):
    """Send via DeathTG inline manager if available, otherwise safe fallback.

    Local DeathTG builds may expose different inline helper method names. This
    function probes them instead of assuming Hikka-style send_or_edit().
    """
    app = getattr(getattr(event, "client", None), "deathtg_app", None)
    inline = getattr(app, "inline", None)

    if inline:
        for method_name in ("send_or_edit", "form", "send", "answer"):
            method = getattr(inline, method_name, None)
            if not method:
                continue
            try:
                if method_name == "form":
                    return await method(text, message=event, reply_markup=buttons, ttl=3600)
                return await method(event, text, buttons=buttons, parse_mode="html", link_preview=link_preview)
            except TypeError:
                try:
                    return await method(event.chat_id, text, buttons=buttons)
                except Exception:
                    pass
            except Exception:
                pass

    try:
        return await event.edit(text, buttons=buttons, parse_mode="html", link_preview=link_preview)
    except Exception:
        return await event.respond(text, buttons=buttons, parse_mode="html", link_preview=link_preview)


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


async def apple_search(query: str, limit: int = 5) -> list[dict]:
    url = f"https://itunes.apple.com/search?term={quote(query)}&media=music&entity=song&limit={int(limit)}"
    data, err = await http_get_json(url)
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
        })
    return out


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
    url = f"https://api.spotify.com/v1/search?q={quote(query)}&type=track&limit={int(limit)}"
    data, err = await http_get_json(url, headers={"Authorization": f"Bearer {token}"})
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
    if err or not data:
        return None
    return spotify_item(data)


def soundcloud_headers():
    token = load_cfg().get("soundcloud_token", "").strip()
    return {"Authorization": f"OAuth {token}"} if token else None


def soundcloud_item(x: dict) -> dict:
    user = x.get("user", {}) or {}
    permalink = x.get("permalink_url") or x.get("uri") or ""
    return {
        "provider": "soundcloud",
        "title": x.get("title") or "Unknown",
        "artist": user.get("username") or x.get("publisher_metadata", {}).get("artist") or "Unknown",
        "album": x.get("label_name") or "",
        "url": permalink,
        "preview_url": permalink,
    }


async def soundcloud_search(query: str, limit: int = 5) -> list[dict]:
    headers = soundcloud_headers()
    if not headers:
        return []
    url = f"https://api.soundcloud.com/tracks?q={quote(query)}&limit={int(limit)}"
    data, err = await http_get_json(url, headers=headers)
    if err or not data:
        return []
    if isinstance(data, dict):
        data = data.get("collection", [])
    return [soundcloud_item(x) for x in data if isinstance(x, dict)]


async def soundcloud_oembed(url: str):
    data, err = await http_get_json(f"https://soundcloud.com/oembed?format=json&url={quote(url)}")
    if err or not data:
        return None
    title = data.get("title") or "SoundCloud"
    artist = "SoundCloud"
    if " by " in title:
        left, right = title.rsplit(" by ", 1)
        title, artist = left.strip(), right.strip()
    return {"provider": "soundcloud", "title": title, "artist": artist, "album": "", "url": url, "preview_url": url}


async def soundcloud_lookup(url: str):
    headers = soundcloud_headers()
    if headers:
        data, err = await http_get_json(f"https://api.soundcloud.com/resolve?url={quote(url)}", headers=headers)
        if not err and isinstance(data, dict) and data.get("kind") == "track":
            return soundcloud_item(data)
    return await soundcloud_oembed(url)


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
            await event.edit(
                f"{WARN} <b>Не смог распознать ссылку.</b>\n{INFO} Для Spotify нужны <code>.trspotify client_id client_secret</code>, для SoundCloud желательно <code>.trsoundcloud token</code>.",
                parse_mode="html",
            )
            return
        await send_inline(event, card(item), buttons=single_buttons(item), link_preview=True)
        return

    results = await search_all(query)
    if not results:
        await event.edit(
            f"{WARN} <b>Ничего не нашёл.</b>\n{INFO} Apple работает без токена. Для Spotify/SoundCloud задай ключи через <code>.trspotify</code> и <code>.trsoundcloud</code>.",
            parse_mode="html",
        )
        return

    await send_inline(event, list_text(query, results), buttons=result_buttons(results), link_preview=False)


@command("trspotify", description="Сохранить Spotify API ключи", usage=".trspotify client_id client_secret")
async def trspotify_cmd(event, args: list[str]) -> None:
    raw = argline(args)
    parts = raw.split(maxsplit=1)
    if len(parts) < 2:
        await event.edit(
            f"{SPOTIFY} <b>Формат:</b> <code>.trspotify client_id client_secret</code>\n{INFO} Создай app в Spotify Developer Dashboard.",
            parse_mode="html",
        )
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
        await event.edit(
            f"{SC} <b>Формат:</b> <code>.trsoundcloud token</code>\n{INFO} Нужен SoundCloud API/OAuth token. Без него ссылки SoundCloud частично работают через oEmbed, но поиск может не работать.",
            parse_mode="html",
        )
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
