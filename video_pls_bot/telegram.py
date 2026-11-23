import asyncio
from contextlib import ExitStack, contextmanager, nullcontext
import json
import logging
import os
from pathlib import Path

import aiohttp

from .outbound import Outbound

log = logging.getLogger(__name__)


class TelegramError(Exception):
    def __init__(self, code: int, retry_after: int = 0):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"telegram_{code}")


class Bot:
    def __init__(self, token: str, base: str, session: aiohttp.ClientSession, *, media_gid: int | None = None):
        self.id = int(token.split(":")[0])
        self._endpoint = f"{base}/bot{token}/"
        self.session = session
        self.username = ""
        self.media_gid = media_gid
        self.outbound = Outbound()
        self._progress = {}
        self._progress_slot = asyncio.Semaphore(1)

    async def call(self, method: str, payload=None, *, timeout=45, data=None,
                   _data_factory=None, _chat_id=None):
        paced = method not in {"getUpdates", "getMe", "getWebhookInfo"}
        chat = (payload or {}).get("chat_id", _chat_id)
        if method == "deleteMessage":
            chat = None  # Deletion consumes global budget but sends no chat message.
        for attempt in range(3):
            if paced:
                await self.outbound.acquire(chat)
            try:
                with _data_factory() if _data_factory else nullcontext(data) as body:
                    result = await self._request(method, payload, timeout=timeout, data=body)
                if paced:
                    self.outbound.succeeded()
                return result
            except TelegramError as exc:
                if exc.code != 429:
                    raise
                self.outbound.limited(exc.retry_after)
                # Only a known rejection is safe to retry. Raw multipart bodies
                # cannot be replayed; upload() supplies a fresh body factory.
                if not paced or attempt == 2 or (data is not None and _data_factory is None):
                    raise

    async def _request(self, method, payload=None, *, timeout=45, data=None):
        try:
            async with self.session.post(
                self._endpoint + method, json=payload if data is None else None,
                data=data, timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                result = await response.json()
                if not result.get("ok"):
                    raise TelegramError(result.get("error_code", response.status),
                                        result.get("parameters", {}).get("retry_after", 0))
                return result["result"]
        except (aiohttp.ClientError, ValueError) as exc:
            # Never propagate request URLs: they contain the bot token.
            raise TelegramError(503) from None

    async def text(self, chat_id: int, text: str, **fields):
        return await self.call("sendMessage", {"chat_id": chat_id, "text": text, **fields})

    async def delete_message(self, chat_id: int, message_id: int):
        await self._clear_progress(chat_id, message_id)
        return await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=5)

    async def preview(self, chat_id: int, photo: str, caption: str, **fields):
        return await self.call("sendPhoto", {"chat_id": chat_id, "photo": photo, "caption": caption, **fields}, timeout=10)

    async def edit_progress(self, chat_id: int, message_id: int, text: str, *, photo=False, **fields):
        await self._clear_progress(chat_id, message_id)
        return await self.call("editMessageCaption" if photo else "editMessageText",
                               {"chat_id": chat_id, "message_id": message_id,
                                "caption" if photo else "text": text, **fields}, timeout=5)

    async def update_progress(self, chat_id, message_id, text, *, photo=False, **fields):
        """Publish latest state locally; never wait for Telegram in a download hook."""
        key = (chat_id, message_id)
        payload = {"chat_id": chat_id, "message_id": message_id,
                   "caption" if photo else "text": text, **fields}
        state = self._progress.get(key)
        if state is None:
            state = {"latest": payload, "sent": None, "sending": False, "stopped": False}
            self._progress[key] = state
            state["task"] = asyncio.create_task(self._send_progress(state, photo))
        else:
            state["latest"] = payload
            if state["task"].done() and state["sent"] != payload:
                state["task"] = asyncio.create_task(self._send_progress(state, photo))

    async def _send_progress(self, state, photo):
        while not state["stopped"] and state["latest"] != state["sent"]:
            # Bound background HTTP work so slow edits cannot fill the shared
            # connection pool. Release after each edit for FIFO fairness.
            async with self._progress_slot:
                await self._send_progress_once(state, photo)

    async def _send_progress_once(self, state, photo):
        await self.outbound.acquire(state["latest"]["chat_id"], background=True)
        if state["stopped"]:
            return
        payload = state["latest"]  # Read AFTER admission, dropping obsolete values.
        state["sending"] = True
        try:
            await self._request("editMessageCaption" if photo else "editMessageText", payload, timeout=5)
            state["sent"] = payload
            self.outbound.succeeded()
        except TelegramError as exc:
            if exc.code == 429:
                self.outbound.limited(exc.retry_after)
                return
            log.info("progress_edit_failed bot_id=%s code=%s", self.id, exc.code)
            state["sent"] = payload
            return
        except TimeoutError:
            log.info("progress_edit_failed bot_id=%s code=timeout", self.id)
            state["sent"] = payload
            return
        finally:
            state["sending"] = False

    async def _clear_progress(self, chat_id, message_id):
        state = self._progress.pop((chat_id, message_id), None)
        if state:
            state["stopped"] = True
            # A queued edit can be cancelled. An issued edit must finish before
            # deleting/changing this message to avoid a late status regression.
            if not state["sending"]:
                state["task"].cancel()
            await asyncio.gather(state["task"], return_exceptions=True)

    async def close(self):
        for key in list(self._progress):
            await self._clear_progress(*key)
        await self.outbound.close()

    async def answer_callback(self, callback_id: str, text: str = ""):
        return await self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text}, timeout=5)

    async def upload(self, method: str, field: str, path: Path, chat_id: int, *,
                     cover: Path | None = None, thumbnail: Path | None = None, **fields):
        if path.stat().st_size > 2_000_000_000:
            raise ValueError("Telegram upload size exceeded")
        if self.media_gid is not None:
            # Only finished video/audio become readable by the API user.
            # Cookie jars stay mode 0600; the directory is traversable, not listable.
            os.chown(path.parent, -1, self.media_gid)
            os.chmod(path.parent, 0o710)
            os.chown(path, -1, self.media_gid)
            os.chmod(path, 0o640)
            if cover is None and thumbnail is None:
                return await self.call(method, {"chat_id": chat_id, field: path.resolve().as_uri(), **fields}, timeout=1800)
        # Multipart keeps the local Bot API independent of our filesystem permissions.
        # It forwards the file to Telegram; file IDs are deliberately discarded.
        @contextmanager
        def body():
            with ExitStack() as opened:
                yield self._multipart(opened, path, chat_id, field, cover, thumbnail, fields)
        return await self.call(method, timeout=1800, _data_factory=body, _chat_id=chat_id)

    def _multipart(self, opened, path, chat_id, field, cover, thumbnail, fields):
        fields = dict(fields)
        data = aiohttp.FormData()
        data.add_field("chat_id", str(chat_id))
        for key, image in (("cover", cover), ("thumbnail", thumbnail)):
            if image is not None:
                fields[key] = f"attach://video_{key}"
                data.add_field(f"video_{key}", opened.enter_context(image.open("rb")),
                               filename=image.name, content_type="image/jpeg")
        for key, value in fields.items():
            data.add_field(key, json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value))
        if self.media_gid is not None:
            # Keep a multi-GB video on the file:// path; only the tiny JPEGs
            # use multipart so thumbnails are uploaded as new attachments.
            data.add_field(field, path.resolve().as_uri())
        else:
            data.add_field(field, opened.enter_context(path.open("rb")), filename=path.name,
                           content_type="video/mp4" if field == "video" else "audio/mpeg")
        return data
