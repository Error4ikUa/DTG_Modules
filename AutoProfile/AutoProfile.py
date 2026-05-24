# meta developer: @DeathTerror
# meta name: AutoProfile
# meta description: Telegram profile automation: avatar rotate, photo set/delete, bio text and premium emoji status.
# meta category: profile
# meta version: 1.0.3
# meta author: DeathTerror
# requires: pillow

from __future__ import annotations

import asyncio
import html
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageOps
from telethon import functions, types, utils
from telethon.errors import RPCError

from deathtg.loader import Module
from deathtg.command import command


class AutoProfileMod(Module):
    """Profile tools for DeathTG."""

    strings = {
        "name": "AutoProfile",
        "title": "AutoProfile",
        "description": "Rotate avatar, set/delete profile photos, update bio and premium emoji status.",
        "category": "profile",
        "version": "1.0.3",
        "author": "DeathTerror",
        "commands": ".rotate, .rotateoff, .onprof, .dellprof, .desc, .prem",
        "usage": ".rotate +15 60 | .rotateoff | .onprof reply_photo | .dellprof | .desc text | .prem emoji/off",
        "permissions": "owner",
    }

    DEFAULTS = {
        "rotate_enabled": False,
        "rotate_angle": 0,
        "rotate_step": 15,
        "rotate_interval": 300,
        "rotate_source": "",
        "last_rotated": None,
        "rotate_photo_ids": [],
        "keep_rotated": 1,
        "rotate_size": 1024,
    }

    def __init__(self) -> None:
        super().__init__()
        self._rotate_task: asyncio.Task | None = None
        self._rotate_lock = asyncio.Lock()

    async def client_ready(self, client, db=None):
        if not self.get("state"):
            self.set("state", dict(self.DEFAULTS))
        self.start_rotate_task()

    async def on_unload(self):
        if self._rotate_task:
            self._rotate_task.cancel()
            self._rotate_task = None

    def state(self) -> dict:
        state = self.get("state", None)
        if not isinstance(state, dict):
            state = dict(self.DEFAULTS)
        for key, value in self.DEFAULTS.items():
            state.setdefault(key, value.copy() if isinstance(value, list) else value)
        self.set("state", state)
        return state

    def save_state(self, state: dict) -> None:
        self.set("state", state)

    def module_data_dir(self) -> Path:
        path = Path(__file__).resolve().parent / ".autoprofile"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def rotate_source_path(self) -> Path:
        return self.module_data_dir() / "rotate_source.jpg"

    def start_rotate_task(self) -> None:
        if self._rotate_task and not self._rotate_task.done():
            return
        self._rotate_task = asyncio.create_task(self.rotate_loop())

    async def rotate_loop(self) -> None:
        while True:
            try:
                state = self.state()
                await asyncio.sleep(max(15, int(state.get("rotate_interval", 300))))
                if state.get("rotate_enabled"):
                    await self.rotate_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(30)

    @staticmethod
    def args_text(args) -> str:
        if isinstance(args, (list, tuple)):
            return " ".join(str(x) for x in args).strip()
        return str(args or "").strip()

    @staticmethod
    def esc(value: Any) -> str:
        return html.escape(str(value or ""), quote=False)

    @staticmethod
    def tmp_path(suffix: str = ".jpg") -> Path:
        return Path(tempfile.gettempdir()) / f"dtg_autoprofile_{int(time.time() * 1000)}{suffix}"

    @staticmethod
    def cleanup(*paths: Any) -> None:
        for path in paths:
            try:
                if path and Path(path).exists():
                    Path(path).unlink()
            except Exception:
                pass

    async def get_reply_photo_path(self, event) -> Optional[Path]:
        reply = await event.get_reply_message()
        if not reply or not getattr(reply, "photo", None):
            return None
        path = self.tmp_path(".jpg")
        downloaded = await self.client.download_media(reply, file=str(path))
        return Path(downloaded) if downloaded else None

    async def get_current_avatar_path(self) -> Optional[Path]:
        path = self.tmp_path(".jpg")
        downloaded = await self.client.download_profile_photo("me", file=str(path), download_big=True)
        return Path(downloaded) if downloaded else None

    async def save_rotate_source(self, source_path: Path) -> Path:
        target = self.rotate_source_path()
        size = int(self.state().get("rotate_size", 1024) or 1024)
        with Image.open(source_path) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img = ImageOps.fit(img, (size, size), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            img.save(target, "JPEG", quality=96)
        return target

    async def upload_profile_photo(self, image_path: Path):
        uploaded = await self.client.upload_file(str(image_path))
        return await self.client(functions.photos.UploadProfilePhotoRequest(file=uploaded))

    async def delete_photo_obj(self, photo_obj) -> bool:
        try:
            input_photo = utils.get_input_photo(photo_obj)
            await self.client(functions.photos.DeletePhotosRequest(id=[input_photo]))
            return True
        except Exception:
            return False

    async def delete_photo_by_id(self, photo_id: int) -> bool:
        if not photo_id:
            return False
        try:
            photos = await self.client(functions.photos.GetUserPhotosRequest(user_id="me", offset=0, max_id=0, limit=100))
            for photo in list(getattr(photos, "photos", []) or []):
                if int(getattr(photo, "id", 0) or 0) == int(photo_id):
                    return await self.delete_photo_obj(photo)
        except Exception:
            return False
        return False

    async def cleanup_rotated_history(self) -> None:
        state = self.state()
        ids = [int(x) for x in state.get("rotate_photo_ids", []) if str(x).isdigit()]
        keep = max(1, int(state.get("keep_rotated", 1) or 1))
        if len(ids) <= keep:
            return
        to_delete = ids[:-keep]
        state["rotate_photo_ids"] = ids[-keep:]
        self.save_state(state)
        for photo_id in to_delete:
            await self.delete_photo_by_id(photo_id)

    async def cleanup_all_rotated_history(self) -> int:
        state = self.state()
        ids = [int(x) for x in state.get("rotate_photo_ids", []) if str(x).isdigit()]
        deleted = 0
        for photo_id in ids:
            if await self.delete_photo_by_id(photo_id):
                deleted += 1
        state["rotate_photo_ids"] = []
        state["last_rotated"] = None
        self.save_state(state)
        return deleted

    async def delete_latest_profile_photo(self) -> bool:
        photos = await self.client(functions.photos.GetUserPhotosRequest(user_id="me", offset=0, max_id=0, limit=1))
        items = list(getattr(photos, "photos", []) or [])
        if not items:
            return False
        return await self.delete_photo_obj(items[0])

    def parse_rotate_args(self, text: str) -> tuple[int, int]:
        parts = text.split()
        if len(parts) < 2:
            raise ValueError("usage")
        step = int(parts[0].replace("+", ""))
        interval = int(parts[1])
        if step == 0:
            raise ValueError("angle_zero")
        return step, max(15, interval)

    def rotate_image(self, source: Path, angle: int) -> Path:
        out = self.tmp_path(".jpg")
        size = int(self.state().get("rotate_size", 1024) or 1024)
        with Image.open(source) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img = ImageOps.fit(img, (size, size), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))
            # expand=False keeps fixed 1:1 canvas. No endless zoom-out and no huge black frame.
            rotated = img.rotate(-angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=(0, 0, 0))
            rotated.save(out, "JPEG", quality=95)
        return out

    async def rotate_once(self) -> bool:
        async with self._rotate_lock:
            state = self.state()
            source = Path(str(state.get("rotate_source") or ""))
            if not source.exists():
                current = await self.get_current_avatar_path()
                if not current:
                    return False
                source = await self.save_rotate_source(current)
                self.cleanup(current)
                state["rotate_source"] = str(source)

            angle = (int(state.get("rotate_angle", 0)) + int(state.get("rotate_step", 15))) % 360
            state["rotate_angle"] = angle
            self.save_state(state)

            rotated_path = None
            try:
                rotated_path = self.rotate_image(source, angle)
                result = await self.upload_profile_photo(rotated_path)
                photo = getattr(result, "photo", None)
                photo_id = int(getattr(photo, "id", 0) or 0)
                if photo_id:
                    ids = [int(x) for x in state.get("rotate_photo_ids", []) if str(x).isdigit()]
                    ids.append(photo_id)
                    state["rotate_photo_ids"] = ids
                    state["last_rotated"] = str(photo_id)
                    self.save_state(state)
                    await self.cleanup_rotated_history()
                return True
            finally:
                self.cleanup(rotated_path)

    def extract_custom_emoji_id_from_message(self, message) -> Optional[int]:
        if not message:
            return None
        for entity in list(getattr(message, "entities", []) or []):
            if isinstance(entity, types.MessageEntityCustomEmoji):
                document_id = int(getattr(entity, "document_id", 0) or 0)
                if document_id:
                    return document_id
        return None

    async def extract_premium_emoji_id(self, event, text: str) -> Optional[int]:
        if text and text.strip().isdigit():
            return int(text.strip())
        document_id = self.extract_custom_emoji_id_from_message(getattr(event, "message", None))
        if document_id:
            return document_id
        try:
            reply = await event.get_reply_message()
        except Exception:
            reply = None
        return self.extract_custom_emoji_id_from_message(reply)

    @command("rotate", description="Rotate current avatar by angle every timer seconds", usage=".rotate +15 300")
    async def rotate_cmd(self, event, args):
        text = self.args_text(args)
        try:
            step, interval = self.parse_rotate_args(text)
        except Exception:
            await event.edit(
                "<b>Usage:</b> <code>.rotate +15 300</code>\n"
                "Angle can be positive or negative. Timer is seconds, minimum 15.",
                parse_mode="html",
            )
            return

        await event.edit("<b>Preparing stable avatar rotation...</b>", parse_mode="html")
        await self.cleanup_all_rotated_history()
        source_raw = await self.get_current_avatar_path()
        if not source_raw:
            await event.edit("<b>No current profile photo found.</b>", parse_mode="html")
            return

        source = await self.save_rotate_source(source_raw)
        self.cleanup(source_raw)

        state = self.state()
        state["rotate_enabled"] = True
        state["rotate_step"] = step
        state["rotate_interval"] = interval
        state["rotate_angle"] = 0
        state["rotate_source"] = str(source)
        state["rotate_photo_ids"] = []
        state["last_rotated"] = None
        self.save_state(state)
        self.start_rotate_task()
        await self.rotate_once()
        await event.edit(
            "<b>Avatar rotation enabled.</b>\n"
            f"Step: <code>{step}</code> deg\n"
            f"Timer: <code>{interval}</code> sec\n"
            "Scale: <code>fixed 1:1, no zoom-out</code>\n"
            "Cleanup: <code>keeps only 1 rotated avatar</code>",
            parse_mode="html",
        )

    @command("rotateoff", description="Disable avatar rotation", usage=".rotateoff")
    async def rotateoff_cmd(self, event, args):
        state = self.state()
        state["rotate_enabled"] = False
        self.save_state(state)
        deleted = await self.cleanup_all_rotated_history()
        await event.edit(f"<b>Avatar rotation disabled.</b>\nDeleted rotated avatars: <code>{deleted}</code>", parse_mode="html")

    @command("onprof", description="Set replied photo as profile avatar", usage=".onprof reply_to_photo")
    async def onprof_cmd(self, event, args):
        path = await self.get_reply_photo_path(event)
        if not path:
            await event.edit("<b>Reply to a photo.</b>", parse_mode="html")
            return
        try:
            await self.upload_profile_photo(path)
            await event.edit("<b>Profile photo uploaded.</b>\nOld photos were not deleted.", parse_mode="html")
        except Exception as exc:
            await event.edit(f"<b>Failed:</b> <code>{self.esc(exc)}</code>", parse_mode="html")
        finally:
            self.cleanup(path)

    @command("dellprof", description="Delete latest profile avatar", usage=".dellprof")
    async def dellprof_cmd(self, event, args):
        try:
            ok = await self.delete_latest_profile_photo()
            if ok:
                state = self.state()
                latest = state.get("last_rotated")
                if latest and str(latest).isdigit():
                    state["rotate_photo_ids"] = [x for x in state.get("rotate_photo_ids", []) if int(x) != int(latest)]
                    state["last_rotated"] = None
                    self.save_state(state)
                await event.edit("<b>Latest profile photo deleted.</b>", parse_mode="html")
            else:
                await event.edit("<b>No profile photo to delete.</b>", parse_mode="html")
        except Exception as exc:
            await event.edit(f"<b>Failed:</b> <code>{self.esc(exc)}</code>", parse_mode="html")

    @command("desc", description="Set Telegram profile bio/about", usage=".desc text")
    async def desc_cmd(self, event, args):
        text = self.args_text(args)
        if not text:
            await event.edit("<b>Usage:</b> <code>.desc profile description</code>", parse_mode="html")
            return
        if len(text) > 70:
            text = text[:70]
        try:
            await self.client(functions.account.UpdateProfileRequest(about=text))
            await event.edit(f"<b>Profile description updated:</b>\n<code>{self.esc(text)}</code>", parse_mode="html")
        except Exception as exc:
            await event.edit(f"<b>Failed:</b> <code>{self.esc(exc)}</code>", parse_mode="html")

    @command("prem", description="Set premium emoji status by emoji or document id", usage=".prem emoji | .prem off")
    async def prem_cmd(self, event, args):
        text = self.args_text(args)
        try:
            if text.lower() in {"off", "clear", "none", "0"}:
                status = None
                await self.client(functions.account.UpdateEmojiStatusRequest(emoji_status=status))
                await event.edit("<b>Premium emoji status cleared.</b>", parse_mode="html")
                return

            document_id = await self.extract_premium_emoji_id(event, text)
            if not document_id:
                await event.edit(
                    "<b>Send a premium emoji with command:</b> <code>.prem 😎</code>\n"
                    "Or reply <code>.prem</code> to a message with premium emoji.",
                    parse_mode="html",
                )
                return

            status = types.EmojiStatus(document_id=document_id)
            await self.client(functions.account.UpdateEmojiStatusRequest(emoji_status=status))
            await event.edit(f"<b>Premium emoji status updated.</b>\nID: <code>{document_id}</code>", parse_mode="html")
        except RPCError as exc:
            await event.edit(f"<b>Telegram error:</b> <code>{self.esc(exc)}</code>", parse_mode="html")
        except Exception as exc:
            await event.edit(f"<b>Failed:</b> <code>{self.esc(exc)}</code>", parse_mode="html")

    @command("autoprof", description="Show AutoProfile status", usage=".autoprof")
    async def autoprof_cmd(self, event, args):
        state = self.state()
        await event.edit(
            "<b>AutoProfile</b>\n\n"
            f"Rotate: <code>{'ON' if state.get('rotate_enabled') else 'OFF'}</code>\n"
            f"Step: <code>{state.get('rotate_step')}</code> deg\n"
            f"Timer: <code>{state.get('rotate_interval')}</code> sec\n"
            f"Canvas: <code>{state.get('rotate_size', 1024)}x{state.get('rotate_size', 1024)}</code>\n"
            f"Rotated kept: <code>{state.get('keep_rotated', 1)}</code>\n\n"
            "Commands:\n"
            "<code>.rotate +15 300</code>\n"
            "<code>.rotateoff</code>\n"
            "<code>.onprof</code> reply to photo\n"
            "<code>.dellprof</code>\n"
            "<code>.desc text</code>\n"
            "<code>.prem 😎</code> or reply <code>.prem</code>",
            parse_mode="html",
        )
