import asyncio
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from video_pls_bot.analytics import Analytics
from video_pls_bot.config import Settings
from video_pls_bot.daily_reports import (
    DAY, MOSCOW, ReportStore, downloads, due_end, enable_delivery_tracking,
    initialize, render_digest, send_due,
)
from video_pls_bot.telegram import TelegramError
from video_pls_bot.texts import plural


def msk(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=MOSCOW).timestamp())


class DailyTrackingTests(unittest.TestCase):
    def test_moscow_boundary_uses_twenty_hours_even_on_utc_host(self):
        end = msk("2026-11-02T20:00:00")
        self.assertEqual(due_end(end - 1), end - DAY)
        self.assertEqual(due_end(end), end)
        self.assertEqual(due_end(end + 1), end)
        utc = datetime(2026, 11, 2, 17, tzinfo=timezone.utc).timestamp()
        self.assertEqual(due_end(utc), end)

    def test_migration_preserves_live_jobs_and_trigger_tracks_only_first_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "analytics.sqlite3"
            analytics = Analytics(path)
            try:
                analytics.accepted("live", 100, 1, "tiktok")
                analytics.accepted("old", 100, 1, "youtube")
                analytics.update("old", status="partial", video_sent=1)
                enable_delivery_tracking(path)
                self.assertEqual(analytics.db.execute(
                    "SELECT status FROM requests WHERE job_id='live'").fetchone()[0], "queued")
                self.assertEqual(analytics.db.execute(
                    "SELECT video_sent_at=created FROM requests WHERE job_id='old'").fetchone()[0], 1)
                before = int(time.time())
                analytics.update("live", status="running", video_sent=1)
                delivered = analytics.db.execute(
                    "SELECT video_sent_at FROM requests WHERE job_id='live'").fetchone()[0]
                self.assertGreaterEqual(delivered, before)
                # The final analytics update repeats video_sent=1 after MP3 work.
                analytics.db.execute("UPDATE requests SET video_sent_at=123 WHERE job_id='live'")
                analytics.db.commit()
                analytics.update("live", video_sent=1, status="partial")
                enable_delivery_tracking(path)
                self.assertEqual(analytics.db.execute(
                    "SELECT video_sent_at FROM requests WHERE job_id='live'").fetchone()[0], 123)
            finally:
                analytics.close()

    def test_counts_delivery_time_boundaries_partial_and_anonymous_deliveries(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "analytics.sqlite3"
            analytics = Analytics(path)
            enable_delivery_tracking(path)
            end = msk("2026-11-02T20:00:00")
            try:
                cases = [
                    ("start", 1, 1, end - DAY, "completed"),
                    ("partial", 1, 1, end - 1, "partial"),
                    ("next_day", 1, 1, end, "completed"),
                    ("too_old", 1, 1, end - DAY - 1, "completed"),
                    ("failed", 2, 0, None, "failed"),
                    ("channel", 3, 1, end - 100, "completed"),
                ]
                for job, bot, sent, when, status in cases:
                    analytics.accepted(job, None, bot, "youtube")
                    analytics.update(job, video_sent=sent, status=status)
                    # Deliberately place creation in another period.
                    analytics.db.execute("UPDATE requests SET video_sent_at=?,created=? WHERE job_id=?",
                                         (when, end - 2 * DAY, job))
                analytics.db.commit()
                self.assertEqual(downloads(path, end), {1: 2, 3: 1})
                self.assertEqual(downloads(path, end + DAY), {1: 1})
            finally:
                analytics.close()

    def test_empty_and_zero_bots_are_omitted_and_counts_are_correct(self):
        end = msk("2026-11-02T20:00:00")
        self.assertIsNone(render_digest(end, {}, {}))
        self.assertIsNone(render_digest(end, {1: 0}, {1: "empty"}))
        for count, noun in ((1, "video"), (2, "videos"), (11, "videos"), (21, "videos")):
            text = render_digest(end, {1: count, 2: 0}, {1: "testbot", 2: "empty"})
            self.assertIn(f"@testbot — {count} {noun}", text)
            self.assertNotIn("empty", text)
            self.assertIn("01.11.2026 20:00 — 02.11.2026 20:00 MSK", text)

    def test_three_form_plurals_use_the_east_slavic_rule(self):
        forms = ["{count} one", "{count} few", "{count} many"]
        with patch.dict("video_pls_bot.texts.TEXTS", {"digest.videos": forms}):
            for count, noun in ((1, "one"), (2, "few"), (5, "many"), (11, "many"), (12, "many"),
                                (21, "one"), (22, "few"), (111, "many")):
                self.assertEqual(plural("digest.videos", count), f"{count} {noun}")


class DailyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings((), self.root, self.root, alert_bot_id=1)
        self.analytics = Analytics(self.root / "analytics.sqlite3")
        self.end = msk("2026-11-02T20:00:00")
        initialize(self.settings, self.end - 100)
        self.sender = AsyncMock()
        self.sender.call.return_value = {"username": "testbot"}
        self.bots = {1: self.sender}

    def tearDown(self):
        self.analytics.close()
        self.temp.cleanup()

    def add_video(self, job="one", when=None, bot=1):
        self.analytics.accepted(job, 100, bot, "tiktok")
        self.analytics.update(job, video_sent=1, status="partial")
        self.analytics.db.execute("UPDATE requests SET video_sent_at=? WHERE job_id=?",
                                  (self.end - 1 if when is None else when, job))
        self.analytics.db.commit()

    async def test_waits_until_twenty_and_sends_once_to_configured_admin(self):
        self.add_video()
        self.assertEqual(await send_due(self.settings, self.bots, self.end - 1), 0)
        self.assertEqual(await send_due(self.settings, self.bots, self.end), 1)
        self.assertEqual(await send_due(self.settings, self.bots, self.end + 100), 0)
        self.sender.text.assert_awaited_once()
        chat, message = self.sender.text.call_args.args
        self.assertEqual(chat, self.settings.alert_chat_id)
        self.assertIn("@testbot — 1 video", message)

    async def test_zero_period_is_silent_and_not_revisited(self):
        self.assertEqual(await send_due(self.settings, self.bots, self.end), 0)
        self.sender.text.assert_not_awaited()
        self.sender.call.assert_not_awaited()
        self.add_video()
        self.assertEqual(await send_due(self.settings, self.bots, self.end + 1), 0)

    async def test_all_active_bots_share_one_message_including_new_bot_username(self):
        self.add_video()
        self.add_video("two", bot=4)
        other = AsyncMock()
        other.call.return_value = {"username": "new_example_bot"}
        self.bots[4] = other
        self.assertEqual(await send_due(self.settings, self.bots, self.end), 1)
        self.assertIn("@new_example_bot — 1 video", self.sender.text.call_args.args[1])
        other.text.assert_not_awaited()

    async def test_failed_send_retries_frozen_message_after_restart(self):
        self.add_video()
        self.sender.text.side_effect = TelegramError(503)
        with self.assertRaises(TelegramError):
            await send_due(self.settings, self.bots, self.end)
        message = self.sender.text.call_args.args[1]
        self.add_video("late_database_update")
        self.sender.text.side_effect = None
        self.assertEqual(await send_due(self.settings, self.bots, self.end + 100), 1)
        self.assertEqual(self.sender.text.call_args.args[1], message)
        self.assertEqual(await send_due(self.settings, self.bots, self.end + 101), 0)

    async def test_downtime_catches_up_each_period_and_initialization_is_idempotent(self):
        self.add_video()
        self.add_video("tomorrow", when=self.end + 100)
        # Deployment initialization must not reset an existing cursor/outbox.
        initialize(self.settings, self.end + DAY + 100)
        with patch("video_pls_bot.daily_reports.asyncio.sleep", new=AsyncMock()):
            self.assertEqual(await send_due(self.settings, self.bots, self.end + DAY), 2)
        messages = [call.args[1] for call in self.sender.text.call_args_list]
        self.assertIn("01.11.2026 20:00 — 02.11.2026 20:00", messages[0])
        self.assertIn("02.11.2026 20:00 — 03.11.2026 20:00", messages[1])

    async def test_concurrent_invocation_does_not_send_duplicate(self):
        self.add_video()
        started, release = asyncio.Event(), asyncio.Event()

        async def hold_send(*args):
            started.set()
            await release.wait()

        self.sender.text.side_effect = hold_send
        first = asyncio.create_task(send_due(self.settings, self.bots, self.end))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            self.assertEqual(await send_due(self.settings, self.bots, self.end), 0)
        finally:
            release.set()
            self.assertEqual(await first, 1)
        self.sender.text.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
