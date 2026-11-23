import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from video_pls_bot.analytics import Analytics
from video_pls_bot.app import Application
from video_pls_bot.config import Settings
from video_pls_bot.links import parse_link
from video_pls_bot.media import Video, make_mp3
from video_pls_bot.messages import build_video_caption
from video_pls_bot.process import ProcessError, run_process


class LinkTests(unittest.TestCase):
    def test_platform_links_and_tracking_removal(self):
        examples = {
            "https://www.youtube.com/shorts/BaW_jenozKc?si=tracking": "youtube",
            "https://youtu.be/BaW_jenozKc?t=2": "youtube",
            "https://youtube.com/watch?v=BaW_jenozKc&list=abc": "youtube",
            "https://www.tiktok.com/@example/video/123456789": "tiktok",
            "https://vm.tiktok.com/Zexample/": "tiktok",
            "https://www.instagram.com/reel/Example123/?igsh=abc": "instagram",
        }
        for link, platform in examples.items():
            with self.subTest(link=link):
                parsed = parse_link(link)
                self.assertEqual(parsed.platform, platform)
                self.assertNotIn("tracking", parsed.url)
                self.assertNotIn("igsh", parsed.url)
                self.assertNotIn("list=", parsed.url)
        self.assertEqual(parse_link("https://m.tiktok.com/@user/video/12345").url,
                         "https://www.tiktok.com/@user/video/12345")
        self.assertEqual(parse_link("https://m.instagram.com/reel/example/").url,
                         "https://www.instagram.com/reel/example/")

    def test_rejects_nonvideo_urls_and_host_spoofing(self):
        links = ["http://127.0.0.1/a", "https://youtube.com.evil.test/watch?v=BaW_jenozKc",
                 "https://youtube.com@evil.test/watch?v=BaW_jenozKc",
                 "https://youtube.com:8080/watch?v=BaW_jenozKc",
                 "https://www.instagram.com/example/", "https://www.tiktok.com/@example",
                 "https://youtube.com/playlist?list=a", "file:///etc/passwd",
                 "https://youtu.be/BaW_jenozKc https://youtu.be/BaW_jenozKc"]
        for link in links:
            with self.subTest(link=link), self.assertRaises(ValueError):
                parse_link(link)


class AnalyticsTests(unittest.TestCase):
    def test_deduplicates_users_across_bots_and_counts_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            store = Analytics(Path(root) / "stats.sqlite3")
            store.seen(100, 1)
            store.seen(100, 2)
            store.seen(200, 2)
            store.accepted("one", 100, 1, "youtube")
            store.update("one", status="completed", video_sent=1, audio_sent=1, video_bytes=1234)
            store.accepted("two", 200, 2, "tiktok")
            store.update("two", status="partial", video_sent=1)
            result = store.summary()
            self.assertEqual(result["total"]["active_users"], 2)
            self.assertEqual(result["total"]["new_users"], 2)
            self.assertEqual(result["total"]["requests"], 2)
            self.assertEqual(result["total"]["completed"], 1)
            self.assertEqual(result["total"]["partial"], 1)
            self.assertEqual(result["bots"]["2"]["active_users"], 2)
            store.set_offset(1, 123)
            store.close()
            store = Analytics(Path(root) / "stats.sqlite3")
            self.assertEqual(store.offset(1), 123)
            store.close()


class FakeBot:
    id = 1
    username = "example_bot"

    def __init__(self, fail_audio=False):
        self.sent = []
        self.deleted = []
        self.fail_audio = fail_audio

    async def text(self, chat_id, text):
        self.sent.append(("text", text))
        return {"message_id": len(self.sent)}

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def update_progress(self, *args, **kwargs):
        pass

    async def upload(self, method, field, path, chat_id, **fields):
        if field == "audio" and self.fail_audio:
            from video_pls_bot.telegram import TelegramError
            raise TelegramError(403)
        self.sent.append((field, fields))
        assert path.is_file()


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_then_mp3_caption_and_cleanup_success_or_partial(self):
        for fail_audio in (False, True):
            with self.subTest(fail_audio=fail_audio), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                temporary = root / "media"
                temporary.mkdir()
                settings = Settings((), root, temporary)
                store = Analytics(root / "stats.sqlite3")
                app = Application(settings, store)
                bot = FakeBot(fail_audio)

                async def download(link, directory, settings):
                    path = directory / "video.mp4"
                    path.write_bytes(b"test")
                    cover, thumbnail = directory / "cover.jpg", directory / "thumbnail.jpg"
                    cover.write_bytes(b"cover")
                    thumbnail.write_bytes(b"thumbnail")
                    return Video(path, 10, 1080, 1920, True, "Test", cover=cover, thumbnail=thumbnail)

                async def audio(video, settings):
                    path = video.path.with_name("audio.mp3")
                    path.write_bytes(b"audio")
                    return path

                message = {"message": {"from": {"id": 100}, "chat": {"id": 100, "type": "private"},
                                       "text": "https://youtu.be/BaW_jenozKc"}}
                with patch("video_pls_bot.app.download_video", download), patch("video_pls_bot.app.make_mp3", audio):
                    await app.handle(bot, message)
                    await app.queue.close()
                media = [item for item in bot.sent if item[0] != "text"]
                self.assertEqual(bot.sent[0], ("text", "Downloading ⏳"))
                self.assertEqual(bot.deleted, [(100, 1)])
                self.assertEqual([item[0] for item in media], ["video"] if fail_audio else ["video", "audio"])
                for _, fields in media:
                    self.assertEqual(fields["caption"], build_video_caption(bot.username))
                    self.assertEqual(fields["parse_mode"], "HTML")
                self.assertEqual(media[0][1]["cover"].name, "cover.jpg")
                self.assertEqual(media[0][1]["thumbnail"].name, "thumbnail.jpg")
                if not fail_audio:
                    self.assertNotIn("cover", media[1][1])
                self.assertEqual(list(temporary.iterdir()), [])
                result = store.summary()["total"]
                self.assertEqual(result["partial" if fail_audio else "completed"], 1)
                store.close()

    async def test_process_timeout_stops_process_before_returning(self):
        with tempfile.TemporaryDirectory() as root:
            pid_file = Path(root) / "pid"
            code = "import os,time,pathlib; pathlib.Path(__import__('sys').argv[1]).write_text(str(os.getpid())); time.sleep(30)"
            with self.assertRaises(TimeoutError):
                await run_process(sys.executable, "-c", code, str(pid_file), timeout=0.3)
            pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    async def test_process_stops_when_workspace_exceeds_limit(self):
        with tempfile.TemporaryDirectory() as root:
            code = "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_bytes(b'x'*2000); time.sleep(30)"
            with self.assertRaisesRegex(ProcessError, "size_limit"):
                await run_process(sys.executable, "-c", code, str(Path(root) / "large"),
                                  workspace=Path(root), max_disk_bytes=1000)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    async def test_real_mp3_conversion(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            video = root / "video.mp4"
            await run_process(shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                              "color=c=black:s=64x64:r=10:d=1", "-f", "lavfi", "-i",
                              "sine=frequency=440:duration=1", "-c:v", "libx264", "-c:a", "aac", str(video))
            settings = Settings((), root, root, ffmpeg=shutil.which("ffmpeg"))
            audio = await make_mp3(Video(video, 1, 64, 64, True, "Test"), settings)
            details = json.loads(await run_process(shutil.which("ffprobe"), "-v", "error", "-show_entries",
                                                  "stream=codec_name,codec_type", "-of", "json", str(audio)))
            self.assertEqual(details["streams"], [{"codec_name": "mp3", "codec_type": "audio"}])


if __name__ == "__main__":
    unittest.main()
