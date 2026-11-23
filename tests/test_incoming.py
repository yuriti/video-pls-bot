import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from video_pls_bot.analytics import Analytics
from video_pls_bot.app import Application
from video_pls_bot.config import Settings
from video_pls_bot.incoming import message_link
from video_pls_bot.media import MediaError, Video
from video_pls_bot.queue import DownloadQueue, QueueLimits
from video_pls_bot.telegram import TelegramError


URL = "https://youtu.be/BaW_jenozKc"


class MessageLinkTests(unittest.TestCase):
    def test_supported_link_among_unrelated_links_and_normalized_duplicates(self):
        result = message_link({"text": f"Look {URL}?si=one, https://example.com {URL}?si=two"})
        self.assertEqual(result.url, URL)

    def test_captions_and_hidden_links_with_emoji_utf16_offsets(self):
        message = {"caption": "🎬 watch this video", "caption_entities": [
            {"type": "text_link", "offset": 3, "length": 11, "url": URL}]}
        self.assertEqual(message_link(message).url, URL)
        self.assertEqual(message_link({"caption": f"Clip {URL}"}).url, URL)

    def test_hidden_destination_takes_precedence_over_visible_url_label(self):
        label = "https://www.instagram.com/reel/Example123/"
        message = {"text": label, "entities": [
            {"type": "text_link", "offset": 0, "length": len(label), "url": URL}]}
        self.assertEqual(message_link(message).url, URL)

    def test_schemeless_telegram_url_entity(self):
        label = "youtu.be/BaW_jenozKc"
        self.assertEqual(message_link({"text": "🎬 " + label, "entities": [
            {"type": "url", "offset": 3, "length": len(label)}]}).url, URL)

    def test_ignores_nonvideo_messages_nested_messages_and_ambiguous_links(self):
        examples = [
            {"text": "Hello!"},
            {"text": "https://youtube.com.evil.test/watch?v=BaW_jenozKc"},
            {"text": "https://youtube.com/playlist?list=abc"},
            {"text": "https://youtube.com:bad/watch?v=BaW_jenozKc"},
            {"text": "Agreed", "reply_to_message": {"text": URL}},
            {"text": "Agreed", "link_preview_options": {"url": URL}},
            {"text": URL + " https://www.instagram.com/reel/Example123/"},
        ]
        for message in examples:
            with self.subTest(message=message), self.assertRaises(ValueError):
                message_link(message)


class IncomingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.store = Analytics(root / "stats.sqlite3")
        self.app = Application(Settings((), root, root), self.store)
        self.bot = AsyncMock()
        self.bot.id = 1
        self.bot.username = "example_bot"
        self.bot.text.return_value = {"message_id": 99}

    async def asyncTearDown(self):
        await self.app.queue.close()
        self.store.close()
        self.directory.cleanup()

    def message(self, kind="supergroup", **fields):
        return {"message_id": 42, "chat": {"id": -1001, "type": kind},
                "from": {"id": 100, "is_bot": False}, "text": URL, **fields}

    async def test_ordinary_group_activity_is_silent_and_not_recorded(self):
        for message in (self.message(text="Hello"), self.message(text="/help"),
                        self.message(text="https://example.com"),
                        self.message(**{"from": {"id": 2, "is_bot": True}})):
            await self.app.handle(self.bot, {"message": message})
        self.bot.text.assert_not_awaited()
        self.assertEqual(self.store.summary()["total"]["active_users"], 0)
        self.assertEqual(self.store.summary()["counters"], [])
        self.assertEqual(self.app.queue.running, 0)

    async def test_group_channel_and_anonymous_admin_are_admitted(self):
        process = AsyncMock()
        for kind in ("group", "supergroup", "channel", "anonymous"):
            message = self.message("supergroup" if kind == "anonymous" else kind)
            key = "channel_post" if kind == "channel" else "message"
            if kind == "channel":
                message.pop("from")
            if kind == "anonymous":
                message["from"] = {"id": 1087968824, "is_bot": True}
                message["sender_chat"] = message["chat"]
            with self.subTest(kind=kind), patch.object(self.app, "process", process):
                await self.app.handle(self.bot, {key: message})
                # Let this job finish without closing admission for the next one.
                while self.app.queue.running:
                    await asyncio.sleep(0)
                self.assertEqual(process.call_args.args[1], message)
        self.assertEqual(process.await_count, 4)
        users = self.store.db.execute("SELECT DISTINCT user_id FROM users").fetchall()
        self.assertEqual(users, [(100,)])
        anonymous = self.store.db.execute("SELECT COUNT(*) FROM requests WHERE user_id IS NULL").fetchone()[0]
        self.assertEqual(anonymous, 2)

    async def test_channel_only_bot_appears_in_summary_without_inventing_user(self):
        message = self.message("channel")
        message.pop("from")
        with patch.object(self.app, "process", AsyncMock()):
            await self.app.handle(self.bot, {"channel_post": message})
            await self.app.queue.close()
        summary = self.store.summary()
        self.assertEqual(summary["bots"]["1"]["requests"], 1)
        self.assertEqual(summary["total"]["active_users"], 0)
        self.assertEqual(summary["total"]["download_users"], 0)

    async def test_edited_messages_do_not_start_downloads(self):
        for key in ("edited_message", "edited_channel_post"):
            await self.app.handle(self.bot, {key: self.message()})
        self.assertEqual(self.app.queue.running, 0)

    async def test_user_limit_spans_private_and_group_and_busy_reply_stays_in_group(self):
        release = asyncio.Event()
        async def process(*args, **kwargs):
            await release.wait()
        with patch.object(self.app, "process", side_effect=process) as processing:
            await self.app.handle(self.bot, {"message": self.message("private", chat={"id": 100, "type": "private"})})
            await asyncio.sleep(0)
            await self.app.handle(self.bot, {"message": self.message(message_thread_id=7)})
            self.assertEqual(self.app.queue.running, 1)
            self.assertEqual(processing.await_count, 1)
            self.assertEqual(self.store.summary()["total"]["requests"], 1)
            args, fields = self.bot.text.call_args
            self.assertEqual(args[0], -1001)
            self.assertEqual(fields["message_thread_id"], 7)
            self.assertEqual(fields["reply_parameters"]["message_id"], 42)
            release.set()

    async def test_media_and_failure_notifications_return_to_original_topic(self):
        async def download(link, directory, settings):
            path = directory / "video.mp4"
            path.write_bytes(b"video")
            return Video(path, 10, 1080, 1920, True, "Test")

        async def audio(video, settings):
            path = video.path.with_name("audio.mp3")
            path.write_bytes(b"audio")
            return path

        with patch("video_pls_bot.app.download_video", download), patch("video_pls_bot.app.make_mp3", audio):
            for kind in ("supergroup", "channel", "anonymous"):
                message = self.message("channel" if kind == "channel" else "supergroup")
                if kind == "channel":
                    message.pop("from")
                else:
                    message["message_thread_id"] = 7
                if kind == "anonymous":
                    message["sender_chat"] = message["chat"]
                    message["from"] = {"id": 1087968824, "is_bot": True}
                with self.subTest(kind=kind):
                    self.bot.upload.reset_mock()
                    await self.app.handle(self.bot, {"channel_post" if kind == "channel" else "message": message})
                    while self.app.queue.running:
                        await asyncio.sleep(0)
                    self.assertEqual(self.bot.upload.await_count, 2)
                    for call in self.bot.upload.call_args_list:
                        self.assertEqual(call.args[3], -1001)
                        self.assertEqual(call.kwargs.get("message_thread_id"), None if kind == "channel" else 7)
                        self.assertEqual(call.kwargs["reply_parameters"]["message_id"], 42)
            self.assertEqual(self.store.summary()["total"]["completed"], 3)
        message = self.message(message_thread_id=7)
        with patch("video_pls_bot.app.download_video", side_effect=MediaError("source_unavailable")):
            await self.app.handle(self.bot, {"message": message})
            await self.app.queue.close()
        self.assertEqual(self.bot.text.call_args.args[0], -1001)
        self.assertEqual(self.bot.text.call_args.kwargs["message_thread_id"], 7)

    async def test_poll_subscribes_to_messages_and_channel_posts(self):
        async def call(method, payload, **kwargs):
            self.assertEqual(method, "getUpdates")
            self.assertEqual(payload["allowed_updates"], ["message", "channel_post", "callback_query"])
            self.app.stop.set()
            return [{"update_id": 8, "channel_post": self.message("channel")}]
        self.bot.call.side_effect = call
        with patch.object(self.app, "process", AsyncMock()):
            await self.app.poll(self.bot)
            await self.app.queue.close()
        self.assertEqual(self.store.offset(1), 9)

    async def test_notification_failure_with_no_from_is_reported_safely(self):
        self.bot.text.side_effect = TelegramError(403)
        message = self.message("channel")
        message.pop("from")
        with patch.object(self.app, "report") as report:
            await self.app.notify(self.bot, -1001, "Error", message=message)
            self.assertIsNone(report.call_args.kwargs["user_id"])

    async def test_progress_is_sent_before_download_and_deleted_before_upload(self):
        message = self.message(message_thread_id=7)

        async def download(link, directory, settings):
            self.bot.text.assert_awaited_once_with(
                -1001, "Downloading ⏳", message_thread_id=7,
                reply_parameters={"message_id": 42, "allow_sending_without_reply": True})
            self.bot.delete_message.assert_not_awaited()
            path = directory / "video.mp4"
            path.write_bytes(b"video")
            return Video(path, 10, 1080, 1920, False, "Test")

        async def upload(*args, **kwargs):
            self.bot.delete_message.assert_awaited_once_with(-1001, 99)

        self.bot.upload.side_effect = upload
        with patch("video_pls_bot.app.download_video", download):
            await self.app.handle(self.bot, {"message": message})
            await self.app.queue.close()
        self.bot.delete_message.assert_awaited_once_with(-1001, 99)
        self.bot.upload.assert_awaited_once()

    async def test_progress_is_deleted_on_download_failure_or_timeout(self):
        for timed_out in (False, True):
            with self.subTest(timed_out=timed_out):
                self.bot.reset_mock()
                if timed_out:
                    self.app.queue = DownloadQueue(QueueLimits(timeout_seconds=0.02))
                else:
                    self.app.queue = DownloadQueue()

                async def download(*args):
                    if timed_out:
                        await asyncio.Event().wait()
                    raise MediaError("source_unavailable")

                with patch("video_pls_bot.app.download_video", download):
                    await self.app.handle(self.bot, {"message": self.message()})
                    await self.app.queue.close()
                self.bot.delete_message.assert_awaited_once_with(-1001, 99)
                self.bot.upload.assert_not_awaited()

    async def test_progress_failure_does_not_block_video_delivery(self):
        for operation in ("text", "delete_message"):
            with self.subTest(operation=operation):
                self.app.queue = DownloadQueue()
                self.bot.reset_mock(side_effect=True)
                getattr(self.bot, operation).side_effect = TelegramError(403)

                async def download(link, directory, settings):
                    path = directory / "video.mp4"
                    path.write_bytes(b"video")
                    return Video(path, 10, 1080, 1920, False, "Test")

                with patch("video_pls_bot.app.download_video", download):
                    await self.app.handle(self.bot, {"message": self.message()})
                    await self.app.queue.close()
                self.bot.upload.assert_awaited_once()

    async def test_waiting_request_gets_progress_immediately(self):
        self.app.queue = DownloadQueue(QueueLimits(concurrency=1))
        release = asyncio.Event()
        self.app.queue.submit(bot_id=1, user_id=200, run=release.wait)
        with patch.object(self.app, "process", AsyncMock()) as process:
            try:
                await self.app.handle(self.bot, {"message": self.message()})
                self.bot.text.assert_awaited_once()
                self.assertEqual(self.bot.text.call_args.args[1], "Downloading ⏳")
                process.assert_not_awaited()
            finally:
                release.set()
                await self.app.queue.close()
            self.assertEqual((await process.call_args.kwargs["progress_future"])["message_id"], 99)

    async def test_slow_initial_status_does_not_delay_downloading(self):
        release, downloading = asyncio.Event(), asyncio.Event()
        async def slow_text(*args, **kwargs):
            await release.wait()
            return {'message_id': 99}
        self.bot.text.side_effect = slow_text
        async def download(link, directory, settings):
            downloading.set()
            path = directory / 'video.mp4'
            path.write_bytes(b'video')
            return Video(path, 10, 1080, 1920, False, 'Test')
        with patch('video_pls_bot.app.download_video', download):
            handler = asyncio.create_task(self.app.handle(self.bot, {'message': self.message()}))
            try:
                await asyncio.wait_for(downloading.wait(), 1)
                self.bot.upload.assert_not_awaited()
            finally:
                release.set()
                await handler
                await self.app.queue.close()
            self.bot.upload.assert_awaited_once()
            self.bot.delete_message.assert_awaited_once()
