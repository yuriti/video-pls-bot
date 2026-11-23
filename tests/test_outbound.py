import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from video_pls_bot.outbound import Outbound
from video_pls_bot.telegram import Bot, TelegramError


async def settle():
    for _ in range(12):
        await asyncio.sleep(0)


class OutboundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 100.0
        self.gate = Outbound(clock=lambda: self.now)
        self.addAsyncCleanup(self.gate.close)

    async def advance(self, seconds):
        self.now += seconds
        self.gate.changed.set()
        await settle()

    async def test_no_burst_and_foreground_preempts_progress(self):
        self.gate.next_request = 101
        order = []
        async def request(chat, background):
            await self.gate.acquire(chat, background=background)
            order.append(chat)
        tasks = [asyncio.create_task(request(1, True)), asyncio.create_task(request(2, False)),
                 asyncio.create_task(request(3, True))]
        await settle()
        self.assertEqual(order, [])
        await self.advance(1)
        self.assertEqual(order, [2])
        await self.advance(.041)
        self.assertEqual(order, [2, 1])
        await self.advance(.041)
        self.assertEqual(order, [2, 1, 3])
        await asyncio.gather(*tasks)

    async def test_chat_group_limits_do_not_block_other_chats(self):
        await self.gate.acquire(-1)
        tasks = [asyncio.create_task(self.gate.acquire(chat)) for chat in (-1, 2, 2)]
        await settle()
        await self.advance(.041)
        self.assertTrue(tasks[1].done())
        self.assertFalse(tasks[0].done())
        await self.advance(1.051)
        self.assertTrue(tasks[2].done())
        self.assertFalse(tasks[0].done())
        await self.advance(2)
        await asyncio.gather(*tasks)
        await self.advance(4)
        await self.gate.acquire(3)
        self.assertEqual(list(self.gate.chats), [3])

    async def test_retry_after_is_not_capped_and_recovery_is_gradual(self):
        self.gate.limited(120)
        reduced = self.gate.rate
        task = asyncio.create_task(self.gate.acquire(1))
        await settle()
        await self.advance(119)
        self.assertFalse(task.done())
        self.gate.succeeded()
        self.assertEqual(self.gate.rate, reduced)
        await self.advance(1)
        await task
        await self.advance(2)
        self.gate.succeeded()
        self.assertAlmostEqual(self.gate.rate, reduced + .1)
        for _ in range(100):
            self.gate.succeeded()
        self.assertAlmostEqual(self.gate.rate, reduced + .1)

    async def test_cancelled_waiter_is_removed(self):
        self.gate.blocked_until = 200
        task = asyncio.create_task(self.gate.acquire(1))
        await settle()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await settle()
        self.assertFalse(any(self.gate.waiters))


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 100.0
        self.bot = Bot('123:fake', 'http://localhost', MagicMock())
        self.bot.outbound = Outbound(clock=lambda: self.now)
        self.bot._request = AsyncMock(return_value=True)
        self.addAsyncCleanup(self.bot.close)

    async def advance(self, seconds):
        self.now += seconds
        self.bot.outbound.changed.set()
        await settle()

    async def test_thousand_downloads_coalesce_and_receive_fair_turns(self):
        self.bot.outbound.blocked_until = 101
        for version in range(5):
            for chat in range(1, 1001):
                await self.bot.update_progress(chat, 10, str(version), photo=True)
        await settle()
        self.assertEqual(len(self.bot._progress), 1000)
        self.assertEqual(len(self.bot.outbound.waiters[1]), 1)
        self.bot._request.assert_not_awaited()
        foreground = asyncio.create_task(self.bot.text(2000, 'ready'))
        await settle()
        await self.advance(1)
        await foreground
        self.assertEqual(self.bot._request.call_args.args[0], 'sendMessage')
        for _ in range(1000):
            await self.advance(.041)
        edits = [c for c in self.bot._request.call_args_list if c.args[0] == 'editMessageCaption']
        self.assertEqual(len(edits), 1000)
        self.assertEqual([c.args[1]['chat_id'] for c in edits], list(range(1, 1001)))
        self.assertTrue(all(c.args[1]['caption'] == '4' for c in edits))
        for chat in range(1, 1001):
            await self.bot.update_progress(chat, 10, '4', photo=True)
        await settle()
        self.assertEqual(self.bot._request.await_count, 1001)

    async def test_delete_drops_queued_progress(self):
        self.bot.outbound.blocked_until = 101
        await self.bot.update_progress(1, 10, 'old')
        await settle()
        delete = asyncio.create_task(self.bot.delete_message(1, 10))
        await settle()
        await self.advance(1)
        await delete
        self.assertEqual([c.args[0] for c in self.bot._request.call_args_list], ['deleteMessage'])
        self.assertFalse(self.bot._progress)

    async def test_idle_bot_updates_without_old_five_second_timer(self):
        await self.bot.update_progress(1, 10, 'first')
        await settle()
        await self.bot.update_progress(1, 10, 'second')
        await self.advance(1.051)
        self.assertEqual(self.bot._request.await_count, 2)

    async def test_polling_and_other_bot_do_not_wait_for_this_bot_cooldown(self):
        self.bot.outbound.limited(120)
        await self.bot.call('getUpdates', {'offset': 0})
        other = Bot('456:fake', 'http://localhost', MagicMock())
        self.addAsyncCleanup(other.close)
        other._request = AsyncMock(return_value=True)
        await other.text(1, 'ready')
        other._request.assert_awaited_once()

    async def test_stage_waits_for_issued_edit_then_discards_pending_value(self):
        release = asyncio.Event()
        async def request(method, *a, **k):
            if method == 'editMessageText':
                await release.wait()
        self.bot._request.side_effect = request
        await self.bot.update_progress(1, 10, 'old')
        await settle()
        await self.bot.update_progress(1, 10, 'newer')
        stage = asyncio.create_task(self.bot.edit_progress(1, 10, 'finished'))
        await settle()
        self.assertFalse(stage.done())
        release.set()
        await settle()
        await self.advance(1.051)
        await stage
        self.assertEqual([c.args[1]['text'] for c in self.bot._request.call_args_list], ['old', 'finished'])

    async def test_progress_retry_sends_latest_after_bot_wide_pause(self):
        self.bot._request.side_effect = [TelegramError(429, 10), True, True]
        await self.bot.update_progress(1, 10, 'old')
        await settle()
        await self.bot.update_progress(1, 10, 'latest')
        foreground = asyncio.create_task(self.bot.text(2, 'result'))
        await settle()
        await self.advance(9)
        self.assertEqual(self.bot._request.await_count, 1)
        await self.advance(1)
        await foreground
        await self.advance(.1)
        self.assertEqual(self.bot._request.call_args.args[1]['text'], 'latest')

    async def test_network_failure_is_not_retried_but_next_change_can_be_sent(self):
        self.bot._request.side_effect = [TelegramError(503), True]
        await self.bot.update_progress(1, 10, 'old')
        await settle()
        self.assertEqual(self.bot._request.await_count, 1)
        await self.bot.update_progress(1, 10, 'new')
        await self.advance(1.051)
        self.assertEqual(self.bot._request.await_count, 2)

    async def test_large_upload_does_not_serialize_other_chats(self):
        release = asyncio.Event()
        async def request(method, *a, **k):
            if method == 'sendVideo':
                await release.wait()
        self.bot._request.side_effect = request
        video = asyncio.create_task(self.bot.call('sendVideo', {'chat_id': 1}))
        await settle()
        reply = asyncio.create_task(self.bot.text(2, 'hello'))
        await self.advance(.041)
        await reply
        self.assertFalse(video.done())
        release.set()
        await video

    async def test_429_multipart_retry_reopens_file_from_start(self):
        seen = []
        async def request(method, payload, *, data, **k):
            source = next(value for _, _, value in data._fields if hasattr(value, 'read'))
            seen.append(source.read())
            if len(seen) == 1:
                raise TelegramError(429, 10)
            return True
        self.bot._request.side_effect = request
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'video.mp4'
            path.write_bytes(b'complete-video')
            task = asyncio.create_task(self.bot.upload('sendVideo', 'video', path, 1))
            await settle()
            await self.advance(10)
            await task
        self.assertEqual(seen, [b'complete-video', b'complete-video'])
