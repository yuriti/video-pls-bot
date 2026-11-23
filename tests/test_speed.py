import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from video_pls_bot.analytics import Analytics
from video_pls_bot.app import Application
from video_pls_bot.config import Settings
from video_pls_bot.extract import resolve_tiktok_short
from video_pls_bot.extractor_pool import ExtractorPool
from video_pls_bot.links import VideoLink
from video_pls_bot.media import Video
from video_pls_bot.process import ProcessError
from video_pls_bot.telegram import Bot, TelegramError

STUB = '''
import sys,json,os,time,pathlib
print(json.dumps({'ready':True}),flush=True)
count=0
for line in sys.stdin:
 r=json.loads(line); count+=1
 if r['url'].endswith('exit'): os._exit(3)
 if r['url'].endswith('hang'):
  pathlib.Path(r['directory'],'pid').write_text(str(os.getpid()))
  time.sleep(30)
 print(json.dumps({'ok':True,'pid':os.getpid(),'count':count}),flush=True)
'''


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_selection_handoff_prefers_its_idle_worker(self):
        stub = STUB.replace("'count':count", "'count':count,'metadata_handle':str(os.getpid())")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = ExtractorPool(Settings((), root, root, concurrency=2), (sys.executable, '-u', '-c', stub))
            await pool.start()
            try:
                link = VideoLink('https://example.test/video', 'youtube')
                first = await pool.download(link, root)
                second = await pool.download(link, root, metadata_handle=first['metadata_handle'])
                self.assertEqual(first['pid'], second['pid'])
                self.assertEqual(second['count'], 2)
                self.assertEqual(pool.available.qsize(), 2)
            finally:
                await pool.close()
    async def test_workers_are_warm_reused_and_bounded(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Settings((), Path(root), Path(root), concurrency=2)
            pool = ExtractorPool(settings, (sys.executable, "-u", "-c", STUB))
            await pool.start()
            try:
                first, second = await asyncio.gather(*(
                    pool.download(VideoLink("https://example.test/video", "tiktok"), Path(root)) for _ in range(2)))
                self.assertNotEqual(first["pid"], second["pid"])
                third = await pool.download(VideoLink("https://example.test/video", "tiktok"), Path(root))
                self.assertIn(third["pid"], (first["pid"], second["pid"]))
                self.assertEqual(third["count"], 2)
                self.assertEqual(len(pool.workers), 2)
            finally:
                await pool.close()

    async def test_cancel_kills_worker_and_next_request_uses_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            pool = ExtractorPool(Settings((), root, root, concurrency=1), (sys.executable, "-u", "-c", STUB))
            await pool.start()
            try:
                task = asyncio.create_task(pool.download(VideoLink("https://example.test/hang", "tiktok"), root))
                async with asyncio.timeout(2):
                    while not (root / "pid").exists():
                        await asyncio.sleep(0.01)
                old_pid = int((root / "pid").read_text())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                with self.assertRaises(ProcessLookupError):
                    os.kill(old_pid, 0)
                result = await pool.download(VideoLink("https://example.test/video", "tiktok"), root)
                self.assertNotEqual(result["pid"], old_pid)
            finally:
                await pool.close()

    async def test_crashed_worker_is_replaced_without_stalling_queue(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            pool = ExtractorPool(Settings((), root, root, concurrency=1), (sys.executable, "-u", "-c", STUB))
            await pool.start()
            try:
                with self.assertRaises(ProcessError):
                    await pool.download(VideoLink("https://example.test/exit", "tiktok"), root)
                result = await pool.download(VideoLink("https://example.test/video", "tiktok"), root)
                self.assertTrue(result["ok"])
            finally:
                await pool.close()


class RedirectTests(unittest.TestCase):
    def test_short_link_stops_at_canonical_redirect_and_strips_tracking(self):
        with patch("video_pls_bot.extract.curl_requests.Session") as cls:
            response = cls.return_value.__enter__.return_value.head.return_value
            response.status_code = 301
            response.headers = {"location": "https://www.tiktok.com/@user/video/123456?tracking=secret"}
            self.assertEqual(resolve_tiktok_short("https://vm.tiktok.com/example", "http://127.0.0.1:1"),
                             "https://www.tiktok.com/@user/video/123456")
            cls.return_value.__enter__.return_value.head.assert_called_once_with(
                "https://vm.tiktok.com/example", allow_redirects=False)

    def test_external_redirect_and_timeout_fall_back_without_following(self):
        original = "https://vm.tiktok.com/example"
        with patch("video_pls_bot.extract.curl_requests.Session") as cls:
            session = cls.return_value.__enter__.return_value
            session.head.return_value.status_code = 302
            session.head.return_value.headers = {"location": "http://127.0.0.1/private"}
            self.assertEqual(resolve_tiktok_short(original, "http://127.0.0.1:1"), original)
            session.head.side_effect = TimeoutError()
            self.assertEqual(resolve_tiktok_short(original, "http://127.0.0.1:1"), original)


class ParallelTests(unittest.IsolatedAsyncioTestCase):
    async def test_mp3_finishes_while_video_upload_is_still_running(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            store = Analytics(root / "stats.sqlite3")
            app = Application(Settings((), root, root), store)
            audio_done = asyncio.Event()
            sent = []

            async def download(link, directory, settings):
                path = directory / "video.mp4"; path.write_bytes(b"video")
                return Video(path, 8, 720, 1280, True, "Test")

            async def mp3(video, settings):
                path = video.path.with_name("audio.mp3"); path.write_bytes(b"audio")
                audio_done.set()
                return path

            class FakeBot:
                id = 1
                username = "example_bot"
                async def upload(self, method, field, path, chat_id, **fields):
                    if field == "video":
                        await asyncio.wait_for(audio_done.wait(), timeout=1)
                    sent.append(field)

            store.accepted("a" * 32, 1, 1, "tiktok")
            try:
                with patch("video_pls_bot.app.download_video", download), patch("video_pls_bot.app.make_mp3", mp3):
                    await app.process(FakeBot(), {"from": {"id": 1}, "chat": {"id": 1}},
                                      VideoLink("https://example.test", "tiktok"), "a" * 32)
                self.assertEqual(sent, ["video", "audio"])
            finally:
                store.close()

    async def test_video_upload_failure_cancels_mp3_before_file_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            store = Analytics(root / "stats.sqlite3")
            app = Application(Settings((), root, root), store)
            started, stopped = asyncio.Event(), asyncio.Event()

            async def download(link, directory, settings):
                path = directory / "video.mp4"; path.write_bytes(b"video")
                return Video(path, 8, 720, 1280, True, "Test")

            async def mp3(video, settings):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.assertTrue(video.path.exists())
                    stopped.set()

            class FakeBot:
                id = 1
                username = "example_bot"
                async def text(self, *args): pass
                async def upload(self, *args, **kwargs):
                    await started.wait()
                    raise TelegramError(500)

            store.accepted("a" * 32, 1, 1, "tiktok")
            try:
                with patch("video_pls_bot.app.download_video", download), patch("video_pls_bot.app.make_mp3", mp3):
                    await app.process(FakeBot(), {"from": {"id": 1}, "chat": {"id": 1}},
                                      VideoLink("https://example.test", "tiktok"), "a" * 32)
                self.assertTrue(stopped.is_set())
                self.assertFalse(list(root.glob("job-*")))
            finally:
                store.close()

    async def test_local_file_upload_keeps_cookie_jar_private(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            video, cookies = root / "video.mp4", root / "cookies.txt"
            video.write_bytes(b"video"); cookies.write_text("secret"); cookies.chmod(0o600)
            bot = Bot("123:fake", "http://127.0.0.1:8081", MagicMock(), media_gid=os.getgid())
            bot.call = AsyncMock(return_value={"message_id": 1})
            await bot.upload("sendVideo", "video", video, 1)
            payload = bot.call.call_args.args[1]
            self.assertEqual(payload["video"], video.resolve().as_uri())
            self.assertEqual(cookies.stat().st_mode & 0o777, 0o600)
            self.assertEqual(video.stat().st_mode & 0o777, 0o640)


class ProgressPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_progress_lines_do_not_poison_the_next_warm_worker_result(self):
        stub = '''
import sys,json
print(json.dumps({'ready':True}),flush=True)
for line in sys.stdin:
 r=json.loads(line)
 if r.get('report_progress'):
  print(json.dumps({'progress':1234}),flush=True)
 print(json.dumps({'ok':True,'format':r.get('format_id')}),flush=True)
'''
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            pool = ExtractorPool(Settings((), directory, directory, concurrency=1), (sys.executable, '-u', '-c', stub))
            await pool.start()
            progress = AsyncMock()
            try:
                result = await pool.download(VideoLink('https://example.test/video', 'youtube'), directory,
                                             format_id='136+140', progress=progress)
                progress.assert_awaited_once_with(1234)
                self.assertEqual(result['format'], '136+140')
                again = await pool.download(VideoLink('https://example.test/video', 'youtube'), directory)
                self.assertIsNone(again['format'])
            finally:
                await pool.close()
