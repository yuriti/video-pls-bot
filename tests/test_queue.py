import asyncio
import unittest

from video_pls_bot.queue import AdmissionStatus, DownloadQueue, Outcome, QueueLimits


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_bots_share_concurrency_waiting_limit_and_fifo(self):
        queue = DownloadQueue(QueueLimits(concurrency=2, max_waiting=2))
        release = asyncio.Event()
        started = []
        active = 0
        peak = 0

        def job(number):
            async def run():
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                started.append(number)
                await release.wait()
                active -= 1
            return run

        admissions = [
            queue.submit(bot_id=index % 3, user_id=index, run=job(index))
            for index in range(4)
        ]
        rejected = queue.submit(bot_id=2, user_id=4, run=job(4))
        self.assertTrue(all(a.status == AdmissionStatus.ACCEPTED for a in admissions))
        self.assertEqual(rejected.status, AdmissionStatus.QUEUE_FULL)
        self.assertEqual((queue.running, queue.waiting), (2, 2))
        await asyncio.sleep(0)
        self.assertEqual(started, [0, 1])
        release.set()
        await queue.close()
        self.assertEqual(started, [0, 1, 2, 3])
        self.assertEqual(peak, 2)
        self.assertEqual((queue.running, queue.waiting), (0, 0))
        for admission in admissions:
            self.assertEqual(await admission.ticket.wait(), Outcome.COMPLETED)

    async def test_same_user_cannot_bypass_limit_by_switching_bots(self):
        queue = DownloadQueue(QueueLimits(concurrency=1, max_waiting=1))
        release = asyncio.Event()
        first = queue.submit(bot_id=1, user_id=100, run=release.wait)
        waiting = queue.submit(bot_id=2, user_id=200, run=release.wait)
        for bot_id in (1, 2, 3):
            for user_id in (100, 200):
                result = queue.submit(bot_id=bot_id, user_id=user_id, run=release.wait)
                self.assertEqual(result.status, AdmissionStatus.USER_BUSY)
        release.set()
        await first.ticket.wait()
        await waiting.ticket.wait()
        again = queue.submit(bot_id=3, user_id=100, run=release.wait)
        self.assertEqual(again.status, AdmissionStatus.ACCEPTED)
        await queue.close()

    async def test_failure_does_not_stall_next_job_or_leak_exception(self):
        queue = DownloadQueue(QueueLimits(concurrency=1))

        async def fail():
            raise RuntimeError("sensitive URL must not be logged")

        async def succeed():
            pass

        with self.assertLogs("video_pls_bot.queue", level="INFO") as logs:
            failed = queue.submit(bot_id=1, user_id=1, run=fail)
            following = queue.submit(bot_id=2, user_id=2, run=succeed)
            await queue.close()
        self.assertEqual(await failed.ticket.wait(), Outcome.FAILED)
        self.assertEqual(await following.ticket.wait(), Outcome.COMPLETED)
        self.assertNotIn("sensitive URL", " ".join(logs.output))

    async def test_timeout_waits_for_cleanup_before_starting_next_job(self):
        queue = DownloadQueue(QueueLimits(concurrency=1, timeout_seconds=0.02))
        events = []

        async def slow():
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                events.append("cleanup")

        async def following():
            events.append("next")

        first = queue.submit(bot_id=1, user_id=1, run=slow)
        second = queue.submit(bot_id=2, user_id=2, run=following)
        await asyncio.wait_for(queue.close(), timeout=1)
        self.assertEqual(await first.ticket.wait(), Outcome.TIMED_OUT)
        self.assertEqual(await second.ticket.wait(), Outcome.COMPLETED)
        self.assertEqual(events, ["cleanup", "next"])

    async def test_cancelling_ticket_waiter_does_not_cancel_job(self):
        queue = DownloadQueue()
        release = asyncio.Event()
        admission = queue.submit(bot_id=1, user_id=1, run=release.wait)
        waiter = asyncio.create_task(admission.ticket.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(queue.running, 1)
        release.set()
        await queue.close()
        self.assertEqual(await admission.ticket.wait(), Outcome.COMPLETED)

    async def test_no_waiting_capacity_and_closed_queue(self):
        queue = DownloadQueue(QueueLimits(concurrency=1, max_waiting=0))
        release = asyncio.Event()
        queue.submit(bot_id=1, user_id=1, run=release.wait)
        rejected = queue.submit(bot_id=2, user_id=2, run=release.wait)
        self.assertEqual(rejected.status, AdmissionStatus.QUEUE_FULL)
        closing = asyncio.create_task(queue.close())
        await asyncio.sleep(0)
        rejected = queue.submit(bot_id=3, user_id=3, run=release.wait)
        self.assertEqual(rejected.status, AdmissionStatus.CLOSED)
        release.set()
        await closing


if __name__ == "__main__":
    unittest.main()
