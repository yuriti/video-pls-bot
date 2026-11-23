import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from video_pls_bot.alerts import AlertStore, Reporter, render_alert
from video_pls_bot.analytics import Analytics
from video_pls_bot.app import Application
from video_pls_bot.config import Settings
from video_pls_bot.incoming import message_link
from video_pls_bot.media import MediaError, Video
from video_pls_bot.queue import AdmissionStatus
from video_pls_bot.telegram import TelegramError


class Sender:
    id = 1001
    username = "first_example_bot"

    def __init__(self):
        self.sent = []
        self.fail = False

    async def text(self, chat_id, text):
        if self.fail:
            raise TelegramError(503)
        self.sent.append((chat_id, text))


class AlertTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_bots_route_to_one_admin_and_repeats_are_grouped(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Settings((), Path(root), Path(root), alert_chat_id=5000)
            sender = Sender()
            reporter = Reporter(settings, sender)
            names = {1001: "first_example_bot", 1002: "second_example_bot", 1003: "third_example_bot"}
            try:
                for bot_id in names:
                    reporter.emit(bot_id=bot_id, stage="download", code="source_unavailable", platform="youtube")
                    reporter.emit(bot_id=bot_id, stage="download", code="source_unavailable", platform="youtube")
                with patch("video_pls_bot.alerts.asyncio.sleep", new=AsyncMock()), \
                        patch.dict("video_pls_bot.alerts.BOT_NAMES", names):
                    self.assertEqual(await reporter.flush(), 3)
                self.assertEqual([chat for chat, _ in sender.sent], [5000] * 3)
                self.assertIn("@first_example_bot", sender.sent[0][1])
                self.assertIn("@second_example_bot", sender.sent[1][1])
                self.assertIn("@third_example_bot", sender.sent[2][1])
                self.assertTrue(all("Events in this alert: 2" in text for _, text in sender.sent))
                reporter.emit(bot_id=1002, stage="download", code="source_unavailable", platform="youtube")
                self.assertEqual(await reporter.flush(), 0)
                reporter.store.db.execute("UPDATE alerts SET next_send=0")
                reporter.store.db.commit()
                with patch("video_pls_bot.alerts.asyncio.sleep", new=AsyncMock()):
                    self.assertEqual(await reporter.flush(), 1)
                self.assertIn("Events in this alert: 1", sender.sent[-1][1])
            finally:
                reporter.close()

    async def test_failed_delivery_survives_restart_without_recursive_alerts(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Settings((), Path(root), Path(root))
            sender = Sender()
            sender.fail = True
            reporter = Reporter(settings, sender)
            reporter.emit(bot_id=1002, stage="audio_upload", code="telegram_403")
            with self.assertLogs("video_pls_bot.alerts", level="WARNING"):
                self.assertEqual(await reporter.flush(), 0)
            self.assertEqual(reporter.store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 1)
            reporter.close()
            sender.fail = False
            reporter = Reporter(settings, sender)
            try:
                reporter.store.db.execute("UPDATE alerts SET next_send=0")
                reporter.store.db.commit()
                with patch("video_pls_bot.alerts.asyncio.sleep", new=AsyncMock()):
                    self.assertEqual(await reporter.flush(), 1)
                self.assertIn("telegram_403", sender.sent[0][1])
            finally:
                reporter.close()

    async def test_health_check_only_notifies_on_failure_and_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            reporter = Reporter(Settings((), Path(root), Path(root)), Sender())
            try:
                reporter.check("unit", True, stage="service")
                self.assertEqual(reporter.store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0)
                reporter.check("unit", False, stage="service", code="unavailable")
                reporter.check("unit", False, stage="service", code="unavailable")
                reporter.check("unit", True, stage="service")
                rows = reporter.store.db.execute("SELECT code,pending FROM alerts ORDER BY code").fetchall()
                self.assertEqual([(row[0], row[1]) for row in rows], [("recovered", 1), ("unavailable", 1)])
            finally:
                reporter.close()

    async def test_sensitive_unstructured_context_is_never_saved_or_sent(self):
        with tempfile.TemporaryDirectory() as root:
            reporter = Reporter(Settings((), Path(root), Path(root)), Sender())
            try:
                secret = "https://api.telegram.org/bot123:SECRET/sendMessage"
                reporter.emit(bot_id=1, username=secret, stage=secret, code=secret,
                              job_id=secret, platform=secret, source_url=secret)
                row = dict(reporter.store.db.execute("SELECT * FROM alerts").fetchone())
                self.assertNotIn("SECRET", render_alert(row))
                self.assertNotIn("SECRET", str(row))
            finally:
                reporter.close()

    async def test_real_processing_failure_reports_source_bot_and_stage(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            settings = Settings((), root, root)
            analytics = Analytics(root / "stats.sqlite3")
            app = Application(settings, analytics)
            reporter = Reporter(settings, Sender())
            app.reporter = reporter
            bot = Sender()
            bot.id = 1002
            bot.username = "second_example_bot"
            message = {"message": {"from": {"id": 100}, "chat": {"id": 100, "type": "private"},
                                   "text": "https://youtu.be/BaW_jenozKc"}}
            try:
                with patch("video_pls_bot.app.download_video", side_effect=MediaError("source_unavailable")):
                    await app.handle(bot, message)
                    await app.queue.close()
                row = dict(reporter.store.db.execute("SELECT * FROM alerts").fetchone())
                self.assertEqual((row["bot_id"], row["stage"], row["code"]), (1002, "download", "source_unavailable"))
                self.assertEqual(row["user_id"], 100)
                self.assertEqual(row["source_url"], "https://youtu.be/BaW_jenozKc")
                self.assertIn("Video: https://youtu.be/BaW_jenozKc", render_alert(row))
                self.assertFalse(any(p.name.startswith("job-") for p in root.iterdir()))
            finally:
                reporter.close()
                analytics.close()

    async def test_source_link_survives_restart_and_tracks_latest_grouped_failure(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Settings((), Path(root), Path(root))
            sender = Sender()
            reporter = Reporter(settings, sender)
            reporter.emit(stage="download", code="duration_limit", platform="youtube",
                          job_id="a" * 32, user_id=1, source_url="https://youtu.be/BaW_jenozKc?si=SECRET#SECRET")
            first = dict(reporter.store.db.execute("SELECT * FROM alerts").fetchone())
            self.assertEqual(first["source_url"], "https://youtu.be/BaW_jenozKc")
            self.assertNotIn("SECRET", str(first))
            reporter.emit(stage="download", code="duration_limit", platform="youtube",
                          job_id="b" * 32, user_id=2, source_url="https://youtube.com/watch?v=abcdefghijk&si=SECRET")
            reporter.close()
            reporter = Reporter(settings, sender)
            try:
                with patch("video_pls_bot.alerts.asyncio.sleep", new=AsyncMock()):
                    self.assertEqual(await reporter.flush(), 1)
                text = sender.sent[0][1]
                self.assertIn("Video of the last failure: https://youtube.com/watch?v=abcdefghijk", text)
                self.assertIn("Job: " + "b" * 32, text)
                self.assertIn("User: 2", text)
                self.assertIn("Events in this alert: 2", text)
                self.assertNotIn("BaW_jenozKc", text)
                self.assertNotIn("SECRET", text)
            finally:
                reporter.close()

    async def test_link_in_memory_fallback_and_system_alert_without_video(self):
        with tempfile.TemporaryDirectory() as root:
            sender = Sender()
            with patch("video_pls_bot.alerts.AlertStore", side_effect=sqlite3.OperationalError):
                with self.assertLogs("video_pls_bot.alerts", level="ERROR"):
                    reporter = Reporter(Settings((), Path(root), Path(root)), sender)
            reporter.emit(stage="mp3", code="no_audio", platform="instagram",
                          source_url="https://instagram.com/reel/Example123/?igsh=SECRET")
            reporter.emit(stage="service", code="unavailable")
            with patch("video_pls_bot.alerts.asyncio.sleep", new=AsyncMock()):
                self.assertEqual(await reporter.flush(), 2)
            self.assertIn("Video: https://www.instagram.com/reel/Example123/", sender.sent[0][1])
            self.assertNotIn("SECRET", sender.sent[0][1])
            self.assertNotIn("Video:", sender.sent[1][1])

    async def test_video_link_on_upload_and_mp3_failures(self):
        for stage in ("download", "video_upload", "mp3", "audio_upload"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                analytics = Analytics(root / "stats.sqlite3")
                app = Application(Settings((), root, root), analytics)
                reporter = Reporter(app.settings, Sender())
                app.reporter = reporter
                bot = Sender()

                async def download(link, directory, settings):
                    if stage == "download":
                        raise MediaError("duration_limit")
                    path = directory / "video.mp4"
                    path.write_bytes(b"test")
                    return Video(path, 10, 64, 64, True, "Test")

                async def upload(method, *args, **kwargs):
                    if (method == "sendVideo" and stage == "video_upload"
                            or method == "sendAudio" and stage == "audio_upload"):
                        raise TelegramError(403)

                async def audio(video, settings):
                    if stage == "mp3":
                        raise MediaError("process_failed")
                    return video.path.with_suffix(".mp3")

                bot.upload = upload
                message = {"from": {"id": 100}, "chat": {"id": 100, "type": "private"},
                           "caption": "https://www.tiktok.com/@example/video/123456789?tracking=SECRET"}
                try:
                    with patch("video_pls_bot.app.download_video", download), patch("video_pls_bot.app.make_mp3", audio):
                        await app.process(bot, message, message_link(message), "a" * 32)
                    row = dict(reporter.store.db.execute("SELECT * FROM alerts").fetchone())
                    self.assertEqual(row["stage"], stage)
                    self.assertEqual(row["source_url"], "https://www.tiktok.com/@example/video/123456789")
                    self.assertIn("Video: " + row["source_url"], render_alert(row))
                finally:
                    reporter.close()
                    analytics.close()

    async def test_reply_and_queue_failure_include_original_hidden_video_link(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            analytics = Analytics(root / "stats.sqlite3")
            app = Application(Settings((), root, root), analytics)
            reporter = Reporter(app.settings, Sender())
            app.reporter = reporter
            bot = Sender()
            bot.fail = True
            message = {"from": {"id": 100}, "chat": {"id": 100, "type": "private"},
                       "text": "Video", "entities": [{"type": "text_link", "offset": 0, "length": 5,
                       "url": "https://youtu.be/BaW_jenozKc?si=SECRET"}]}
            try:
                with patch.object(app.queue, "submit", return_value=SimpleNamespace(status=AdmissionStatus.QUEUE_FULL)):
                    await app.handle(bot, {"message": message})
                rows = [dict(row) for row in reporter.store.db.execute("SELECT * FROM alerts")]
                self.assertEqual({row["stage"] for row in rows}, {"reply", "queue"})
                self.assertTrue(all(row["source_url"] == "https://youtu.be/BaW_jenozKc" for row in rows))
            finally:
                reporter.close()
                analytics.close()


class LeaseTests(unittest.TestCase):
    def test_existing_database_migrates_without_losing_pending_alerts(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "alerts.sqlite3"
            with sqlite3.connect(path) as db:
                db.execute("""CREATE TABLE alerts (
                    key TEXT PRIMARY KEY, bot_id INTEGER, username TEXT, stage TEXT, code TEXT,
                    platform TEXT, job_id TEXT, user_id INTEGER, first_seen REAL, last_seen REAL,
                    pending INTEGER, next_send REAL DEFAULT 0, lease_until REAL DEFAULT 0)""")
                db.execute("""INSERT INTO alerts (key,stage,code,last_seen,pending)
                            VALUES ('old','service','unavailable',?,1)""", (time.time(),))
            db.close()
            store = AlertStore(path)
            try:
                row = store.claim(time.time())
                self.assertEqual(row["key"], "old")
                self.assertEqual(row["pending"], 1)
                self.assertEqual(row["source_url"], "")
            finally:
                store.close()

    def test_two_deliverers_cannot_claim_same_alert_and_new_events_are_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "alerts.sqlite3"
            first, second = AlertStore(path), AlertStore(path)
            row = {"bot_id": 1, "username": "example_bot", "stage": "download", "code": "timeout",
                   "platform": "youtube", "job_id": "", "user_id": 1, "last_seen": time.time()}
            try:
                first.record(row)
                claimed = first.claim(time.time())
                self.assertIsNone(second.claim(time.time()))
                second.record(row)
                first.sent(claimed, time.time(), 0)
                self.assertEqual(second.claim(time.time())["pending"], 1)
            finally:
                first.close()
                second.close()
