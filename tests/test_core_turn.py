import asyncio
import json
import os
import tempfile
import unittest

from video_pls_bot.core_turn import CoreQueue


class FakeCore:
    """Two tickets ahead of ours; each status call moves the line by one."""

    def __init__(self, ahead=2, gone_once=False):
        self.ahead = ahead
        self.gone_once = gone_once
        self.requests = []

    async def handle(self, reader, writer):
        request = json.loads(await reader.readline())
        self.requests.append(request)
        action = request["action"]
        if action == "acquire":
            self.acquired = getattr(self, "acquired", 0) + 1
            answer = {"ok": True, "ticket": f"t{self.acquired}", "state": "waiting" if self.ahead else "ready", "position": self.ahead}
        elif action == "status":
            if self.gone_once:
                self.gone_once = False
                answer = {"ok": False, "code": "ticket_unknown"}
            else:
                self.ahead = max(0, self.ahead - 1)
                answer = {"ok": True, "state": "waiting" if self.ahead else "ready", "position": self.ahead}
        else:
            answer = {"ok": True, "released": True}
        writer.write(json.dumps(answer).encode() + b"\n")
        await writer.drain()
        writer.close()


class CoreTurnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory(dir="/tmp")
        self.path = os.path.join(self.dir.name, "bot.sock")

    async def asyncTearDown(self):
        self.dir.cleanup()

    async def serve(self, core):
        server = await asyncio.start_unix_server(core.handle, path=self.path)
        self.addAsyncCleanup(self.close, server)
        return server

    @staticmethod
    async def close(server):
        server.close()
        await server.wait_closed()

    async def test_waits_shows_its_place_then_downloads_and_gives_back_with_bytes(self):
        core = FakeCore(ahead=2)
        await self.serve(core)
        places = []

        async def on_place(n):
            places.append(n)

        turn = await CoreQueue(self.path, poll_s=0.01).turn(key="job1", user_id=42, platform="tiktok", on_place=on_place)
        self.assertEqual(places, [2, 1])
        await turn.release("ok", 1234)
        await turn.release("ok", 1234)
        self.assertEqual(core.requests[0], {"action": "acquire", "key": "job1", "platform": "tiktok", "principal": "42"})
        self.assertEqual([r for r in core.requests if r["action"] == "release"],
                         [{"action": "release", "ticket": "t1", "outcome": "ok", "bytes": 1234}])

    async def test_a_lost_ticket_is_asked_again_with_the_same_key(self):
        core = FakeCore(ahead=1, gone_once=True)
        await self.serve(core)
        turn = await CoreQueue(self.path, poll_s=0.01).turn(key="job1", user_id=None, platform="youtube")
        await turn.release("ok")
        self.assertEqual([r["action"] for r in core.requests][:3], ["acquire", "status", "acquire"])
        self.assertEqual(core.requests[2]["key"], "job1")
        self.assertEqual(turn.ticket, "t2", "the new ticket, not the lost one")
        self.assertEqual(core.requests[-1]["ticket"], "t2")

    async def test_a_held_turn_is_renewed_until_released(self):
        core = FakeCore(ahead=0)
        await self.serve(core)
        turn = await CoreQueue(self.path, renew_s=0.01).turn(key="job1", user_id=1, platform="tiktok")
        await asyncio.sleep(0.08)
        await turn.release("ok", 5)
        renewals = [r for r in core.requests if r["action"] == "status"]
        self.assertGreaterEqual(len(renewals), 2)
        await asyncio.sleep(0.05)
        self.assertEqual(len([r for r in core.requests if r["action"] == "status"]), len(renewals), "no renewal after release")

    async def test_no_core_means_the_bot_goes_on_with_its_own_limit(self):
        turn = await CoreQueue(self.path).turn(key="job1", user_id=1, platform="tiktok")
        self.assertIsNone(turn.ticket)
        await turn.release("ok", 1)
        turn = await CoreQueue(None).turn(key="job1", user_id=1, platform="tiktok")
        self.assertIsNone(turn.ticket)

    async def test_a_very_long_wait_goes_ahead_and_frees_the_ticket(self):
        core = FakeCore(ahead=1000)
        await self.serve(core)
        clock = [0.0]

        def tick():
            clock[0] += 400
            return clock[0]

        turn = await CoreQueue(self.path, poll_s=0.0, wait_max_s=900, clock=tick).turn(key="job1", user_id=1, platform="tiktok")
        self.assertIsNone(turn.ticket)
        self.assertEqual(core.requests[-1], {"action": "release", "ticket": "t1", "outcome": "wait_exceeded"})

    async def test_a_broken_place_message_never_stops_the_download(self):
        await self.serve(FakeCore(ahead=1))

        async def on_place(n):
            raise RuntimeError("telegram down")

        turn = await CoreQueue(self.path, poll_s=0.01).turn(key="job1", user_id=1, platform="tiktok", on_place=on_place)
        self.assertEqual(turn.ticket, "t1")
        await turn.release("ok")

    async def test_an_outcome_the_core_would_refuse_goes_as_failed(self):
        core = FakeCore(ahead=0)
        await self.serve(core)
        turn = await CoreQueue(self.path).turn(key="job1", user_id=1, platform="tiktok")
        await turn.release("Download Error!", 7)
        self.assertEqual(core.requests[-1], {"action": "release", "ticket": "t1", "outcome": "failed", "bytes": 7})

    async def test_cancelled_while_waiting_gives_the_ticket_back(self):
        core = FakeCore(ahead=1000)
        await self.serve(core)
        task = asyncio.create_task(CoreQueue(self.path, poll_s=0.01).turn(key="job1", user_id=1, platform="tiktok"))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(core.requests[-1], {"action": "release", "ticket": "t1", "outcome": "cancelled"})


if __name__ == "__main__":
    unittest.main()
