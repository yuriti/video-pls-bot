import asyncio
import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from video_pls_bot.analytics import Analytics
from video_pls_bot.app import Application
from video_pls_bot.config import Settings
from video_pls_bot.extract import MediaRejected, download, format_choices
from video_pls_bot.media import MediaError, Selection, Video
from video_pls_bot.queue import DownloadQueue, QueueLimits
from video_pls_bot.telegram import Bot, TelegramError


INFO = {
    'id': 'pySJJbS-1oo', 'title': 'Example', 'duration': 3600,
    'formats': [
        {'format_id': '140', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'mp4a', 'abr': 128, 'filesize': 60_000_000},
        {'format_id': '137', 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'none', 'width': 1920, 'height': 1080, 'filesize': 2_100_000_000},
        {'format_id': '136', 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'none', 'width': 1280, 'height': 720, 'filesize': 900_000_000},
        {'format_id': '135', 'ext': 'mp4', 'vcodec': 'avc1', 'acodec': 'none', 'width': 854, 'height': 480, 'tbr': 500},
    ],
}


class LargeExtractTests(unittest.TestCase):
    def test_choices_account_for_audio_and_estimate_missing_size(self):
        choices = format_choices(INFO, 2_000_000_000)
        self.assertEqual([c['quality'] for c in choices], [720, 480])
        self.assertEqual(choices[0]['format_id'], '136+140')
        self.assertGreater(choices[0]['bytes'], 960_000_000)
        self.assertGreater(choices[1]['bytes'], 225_000_000)

    def test_long_video_returns_preview_without_downloading_and_selected_format_is_reused(self):
        with tempfile.TemporaryDirectory() as root, patch('video_pls_bot.extract.YoutubeDL') as cls:
            worker = cls.return_value.__enter__.return_value
            worker.extract_info.side_effect = lambda *a, **k: copy.deepcopy(INFO)
            args = ('https://youtu.be/pySJJbS-1oo', Path(root), 'http://127.0.0.1:3128', 2_000_000_000, 0, '/usr/bin/node')
            result = download(*args)
            self.assertTrue(result['selection'])
            self.assertIn('pySJJbS-1oo', result['thumbnail'])
            worker.process_ie_result.assert_not_called()
            self.assertFalse(list(Path(root).iterdir()))
            def save(*a, **k):
                (Path(root) / 'video.mp4').write_bytes(b'video')
                return {'title': 'Example'}
            worker.process_ie_result.side_effect = save
            self.assertTrue(download(*args, format_id='136+140')['ok'])
            self.assertEqual(cls.call_args.args[0]['format'], '136+140')
            with self.assertRaisesRegex(MediaRejected, 'format_unavailable'):
                download(*args, format_id='137+140')

    def test_disk_budget_caps_workers_and_cannot_exceed_telegram_limit(self):
        settings = Settings((), Path('/tmp'), Path('/tmp'), concurrency=12)
        self.assertEqual(settings.disk_concurrency, 2)
        self.assertLess(settings.disk_concurrency * settings.workspace_bytes, 10_000_000_000)
        self.assertEqual(Settings((), Path('/tmp'), Path('/tmp'), temp_budget_bytes=6_000_000_000).disk_concurrency, 1)
        with self.assertRaises(ValueError):
            Settings((), Path('/tmp'), Path('/tmp'), max_bytes=3_000_000_000)


class SelectionFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Analytics(self.root / 'stats.sqlite3')
        self.app = Application(Settings((), self.root, self.root), self.store)
        self.bot = AsyncMock()
        self.bot.id, self.bot.username = 1, 'example_bot'
        self.bot.text.return_value = {'message_id': 10}
        self.bot.preview.return_value = {'message_id': 11}
        self.message = {'message_id': 42, 'from': {'id': 100}, 'chat': {'id': -1001, 'type': 'supergroup'},
                        'message_thread_id': 7, 'text': 'https://youtu.be/pySJJbS-1oo'}
        self.selection = Selection('Example', 3600, 'https://i.ytimg.com/vi/pySJJbS-1oo/hqdefault.jpg',
                                   format_choices(INFO, 2_000_000_000))

    async def asyncTearDown(self):
        await self.app.queue.close()
        self.store.close()
        self.tmp.cleanup()

    async def offer(self):
        with patch('video_pls_bot.app.download_video', AsyncMock(return_value=self.selection)):
            await self.app.handle(self.bot, {'message': self.message})
            while self.app.queue.running:
                await asyncio.sleep(0)
        return next(iter(self.app.selections))

    def callback(self, key, index='0', user=100):
        return {'id': 'callback', 'data': f'q:{key}:{index}', 'from': {'id': user},
                'message': {'chat': {'id': -1001}, 'message_id': 11}}

    async def test_preview_survives_download_and_upload_then_deleted_after_video(self):
        key = await self.offer()
        self.assertEqual(self.app.queue.running, 0)
        self.assertFalse(list(self.root.glob('job-*')))
        self.bot.delete_message.assert_awaited_once_with(-1001, 10)
        self.assertEqual(self.bot.preview.call_args.kwargs['message_thread_id'], 7)
        self.bot.delete_message.reset_mock()
        async def download_selected(link, directory, settings, **fields):
            self.assertEqual(fields['format_id'], '136+140')
            await fields['progress'](100_000_000)
            self.bot.delete_message.assert_not_awaited()
            path = directory / 'video.mp4'
            path.write_bytes(b'video')
            return Video(path, 3600, 1280, 720, False, 'Example')
        async def upload(*a, **k):
            self.bot.delete_message.assert_not_awaited()
            self.assertTrue(any('Uploading to Telegram' in c.args[2] for c in self.bot.update_progress.call_args_list))
            self.assertEqual(k['message_thread_id'], 7)
        self.bot.upload.side_effect = upload
        with patch('video_pls_bot.app.download_video', download_selected):
            await self.app.handle(self.bot, {'callback_query': self.callback(key)})
            await self.app.handle(self.bot, {'callback_query': self.callback(key)})
            await self.app.queue.close()
        self.assertFalse(self.app.selections)
        self.bot.upload.assert_awaited_once()
        self.bot.delete_message.assert_awaited_once_with(-1001, 11)
        self.assertTrue(any('100 MB' in c.args[2] for c in self.bot.update_progress.call_args_list))
        self.assertTrue(all(c.kwargs['photo'] for c in self.bot.update_progress.call_args_list))
        self.assertEqual(self.store.summary()['total']['requests'], 1)
        self.assertFalse(list(self.root.glob('job-*')))

    async def test_other_user_cannot_select_and_expiry_releases_actor(self):
        key = await self.offer()
        await self.app.handle_callback(self.bot, self.callback(key, user=101))
        self.assertIn(key, self.app.selections)
        self.assertEqual(self.app.queue.running, 0)
        self.app.selections[key].expires = time.monotonic() - 1
        await self.app.expire_selections()
        self.assertFalse(self.app.pending_actors)
        self.assertFalse(self.app.selections)
        self.assertEqual(self.bot.edit_progress.call_args.kwargs['reply_markup'], {'inline_keyboard': []})

    async def test_full_queue_retains_buttons_and_selection_for_retry(self):
        key = await self.offer()
        self.app.queue = DownloadQueue(QueueLimits(concurrency=1, max_waiting=0))
        release = asyncio.Event()
        self.app.queue.submit(bot_id=1, user_id=200, run=release.wait)
        try:
            await self.app.handle_callback(self.bot, self.callback(key))
            self.assertIn(key, self.app.selections)
            self.bot.edit_progress.assert_not_awaited()
        finally:
            release.set()

    async def test_cancel_and_preview_failure_fallback(self):
        self.bot.preview.side_effect = TelegramError(400)
        key = await self.offer()
        pending = self.app.selections[key]
        self.assertFalse(pending.photo)
        callback = self.callback(key, 'cancel')
        callback['message']['message_id'] = pending.progress_id
        await self.app.handle_callback(self.bot, callback)
        self.assertFalse(self.app.pending_actors)
        self.assertEqual(self.store.db.execute('SELECT status FROM requests').fetchone()[0], 'cancelled')

    async def test_insufficient_disk_rejects_before_starting_extractor(self):
        with patch('video_pls_bot.app.shutil.disk_usage') as usage:
            usage.return_value.free = 100
            with self.assertRaisesRegex(MediaError, 'disk_full'):
                self.app.reserve_workspace()
        self.assertFalse(self.app.workspaces)
        self.assertFalse(list(self.root.glob('job-*')))

    async def test_upload_rejects_oversized_file_without_network(self):
        path = self.root / 'oversized.mp4'
        with path.open('wb') as file:
            file.truncate(2_000_000_001)
        bot = Bot('123:fake', 'http://127.0.0.1:8081', None)
        bot.call = AsyncMock()
        with self.assertRaises(ValueError):
            await bot.upload('sendVideo', 'video', path, 1)
        bot.call.assert_not_awaited()

    async def test_free_space_check_includes_other_tasks_remaining_reservation(self):
        with patch('video_pls_bot.app.shutil.disk_usage') as usage:
            usage.return_value.free = 5_500_000_000
            first = self.app.reserve_workspace()
            try:
                with self.assertRaisesRegex(MediaError, 'disk_full'):
                    self.app.reserve_workspace()
                self.assertEqual(self.app.workspaces, {first})
            finally:
                self.app.workspaces.remove(first)
                first.rmdir()
