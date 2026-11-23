import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from aiohttp import web

from video_pls_bot.telegram import Bot, TelegramError


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_message_targets_only_the_given_status(self):
        bot = Bot("123:fake", "http://127.0.0.1:8081", MagicMock())
        bot.call = AsyncMock(return_value=True)
        await bot.delete_message(-1001, 99)
        bot.call.assert_awaited_once_with("deleteMessage", {"chat_id": -1001, "message_id": 99}, timeout=5)

    async def test_multipart_upload_preserves_caption_and_separate_audio(self):
        received = []

        async def handle(request):
            fields = await request.json() if request.content_type == "application/json" else await request.post()
            received.append((request.match_info["method"], fields))
            return web.json_response({"ok": True, "result": {"message_id": len(received)}})

        app = web.Application()
        app.router.add_post("/bot123:fake/{method}", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as root:
                video, audio = Path(root) / "video.mp4", Path(root) / "audio.mp3"
                video.write_bytes(b"video")
                audio.write_bytes(b"audio")
                async with aiohttp.ClientSession() as session:
                    bot = Bot("123:fake", f"http://127.0.0.1:{port}", session)
                    await bot.upload("sendVideo", "video", video, 100,
                                     caption="❤️ Downloaded with <b>@example_bot</b>", parse_mode="HTML",
                                     message_thread_id=7,
                                     reply_parameters={"message_id": 42, "allow_sending_without_reply": True})
                    await bot.upload("sendAudio", "audio", audio, 100, title="Audio")
                    await bot.text(-1001, "Reply", message_thread_id=7,
                                   reply_parameters={"message_id": 42, "allow_sending_without_reply": True})
            self.assertEqual([method for method, _ in received], ["sendVideo", "sendAudio", "sendMessage"])
            self.assertEqual(received[0][1]["parse_mode"], "HTML")
            self.assertEqual(received[0][1]["caption"], "❤️ Downloaded with <b>@example_bot</b>")
            self.assertEqual(received[0][1]["video"].filename, "video.mp4")
            self.assertEqual(received[0][1]["message_thread_id"], "7")
            self.assertEqual(json.loads(received[0][1]["reply_parameters"]),
                             {"message_id": 42, "allow_sending_without_reply": True})
            self.assertEqual(received[1][1]["audio"].filename, "audio.mp3")
            self.assertEqual(received[2][1]["chat_id"], -1001)
            self.assertEqual(received[2][1]["message_thread_id"], 7)
            self.assertEqual(received[2][1]["reply_parameters"]["message_id"], 42)
        finally:
            await runner.cleanup()

    async def test_local_api_keeps_reply_parameters_as_json_objects(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "video.mp4"
            path.write_bytes(b"video")
            bot = Bot("123:fake", "http://127.0.0.1:8081", MagicMock(), media_gid=os.getgid())
            bot.call = AsyncMock(return_value={"message_id": 1})
            await bot.upload("sendVideo", "video", path, -1001, message_thread_id=7,
                             reply_parameters={"message_id": 42, "allow_sending_without_reply": True})
            payload = bot.call.call_args.args[1]
            self.assertEqual(payload["chat_id"], -1001)
            self.assertEqual(payload["message_thread_id"], 7)
            self.assertEqual(payload["reply_parameters"], {"message_id": 42, "allow_sending_without_reply": True})

    async def test_cover_and_thumbnail_are_real_attachments_in_both_upload_modes(self):
        received = []

        async def handle(request):
            values = {}
            async for part in await request.multipart():
                values[part.name] = (part.filename, bytes(await part.read()))
            received.append(values)
            return web.json_response({"ok": True, "result": {"message_id": 1}})

        app = web.Application()
        app.router.add_post("/bot123:fake/sendVideo", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as root:
                root = Path(root)
                video, cover, thumbnail = (root / name for name in ("video.mp4", "cover.jpg", "thumbnail.jpg"))
                for path, contents in ((video, b"mp4"), (cover, b"cover"), (thumbnail, b"thumb")):
                    path.write_bytes(contents)
                    path.chmod(0o600)
                cookies = root / "cookies.txt"
                cookies.write_text("private")
                cookies.chmod(0o600)
                async with aiohttp.ClientSession() as session:
                    for gid in (None, os.getgid()):
                        bot = Bot("123:fake", f"http://127.0.0.1:{port}", session, media_gid=gid)
                        await bot.upload("sendVideo", "video", video, -1001, cover=cover, thumbnail=thumbnail,
                                         caption="Caption", message_thread_id=7,
                                         reply_parameters={"message_id": 42, "allow_sending_without_reply": True})
                        fields = received[-1]
                        self.assertEqual(fields["cover"], (None, b"attach://video_cover"))
                        self.assertEqual(fields["thumbnail"], (None, b"attach://video_thumbnail"))
                        self.assertEqual(fields["video_cover"], ("cover.jpg", b"cover"))
                        self.assertEqual(fields["video_thumbnail"], ("thumbnail.jpg", b"thumb"))
                        self.assertEqual(fields["message_thread_id"], (None, b"7"))
                        self.assertEqual(json.loads(fields["reply_parameters"][1])["message_id"], 42)
                        self.assertEqual(fields["video"], ("video.mp4", b"mp4") if gid is None else (
                            None, video.resolve().as_uri().encode()))
                        self.assertEqual(cookies.stat().st_mode & 0o777, 0o600)
                        self.assertEqual(cover.stat().st_mode & 0o777, 0o600)
                        self.assertEqual(thumbnail.stat().st_mode & 0o777, 0o600)
        finally:
            await runner.cleanup()

    async def test_transport_errors_do_not_expose_token(self):
        async with aiohttp.ClientSession() as session:
            bot = Bot("123:secret", "http://127.0.0.1:1", session)
            with self.assertRaises(TelegramError) as error:
                await bot.call("getMe", timeout=1)
            self.assertNotIn("secret", str(error.exception))
