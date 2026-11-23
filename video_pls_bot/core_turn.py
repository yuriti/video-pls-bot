"""A place in the server's shared download queue (an optional external "core" service on a unix socket).

Several services on one server may share the same download proxy and account: the core decides who
downloads now. The bot asks for a turn right before a download and gives it back as soon as the bytes
are here (the Telegram upload does not need the proxy). Other clients may go first; the bot waits and
is never refused. Enabled only when CORE_SOCKET is set.

Fail-open: if the core is not there (not installed, restarting) the bot downloads with its own limit,
as before. A wait longer than WAIT_MAX_S also goes ahead: a person is never stuck for ever.
No URLs, titles or tokens go to the core: a job key, the Telegram user id and the platform name.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

log = logging.getLogger(__name__)

SOCKET = None  # set via Settings.core_socket (CORE_SOCKET)
WAIT_MAX_S = 15 * 60
POLL_S = 2.0
RENEW_S = 30.0
CALL_TIMEOUT_S = 5.0
# what the core takes as an outcome (anything else it refuses, and the place would wait for its lease)
OUTCOME = re.compile(r"[a-z0-9_]{1,40}")


class CoreUnavailable(Exception):
    pass


async def call(path: str, request: dict, timeout: float = CALL_TIMEOUT_S) -> dict:
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_unix_connection(path)
            try:
                writer.write(json.dumps(request).encode() + b"\n")
                await writer.drain()
                line = await reader.readline()
            finally:
                writer.close()
    except (OSError, TimeoutError) as exc:
        raise CoreUnavailable(type(exc).__name__) from None
    try:
        answer = json.loads(line)
    except ValueError:
        raise CoreUnavailable("bad_answer") from None
    if not isinstance(answer, dict):
        raise CoreUnavailable("bad_answer")
    return answer


class Turn:
    """A held place: renewed while the download runs, given back once."""

    def __init__(self, queue: "CoreQueue", ticket: str | None):
        self.queue = queue
        self.ticket = ticket
        self.renewal = asyncio.create_task(self._renew()) if ticket else None
        self.released = False

    async def _renew(self):
        while True:
            await asyncio.sleep(self.queue.renew_s)
            try:
                answer = await call(self.queue.path, {"action": "status", "ticket": self.ticket})
                if answer.get("ok") is not True:
                    # the core forgot the ticket (restart, lease): this download goes on outside the shared limit
                    log.warning("core_ticket_lost code=%s", str(answer.get("code"))[:40])
                    return
            except CoreUnavailable:
                log.info("core_renew_failed")
            except Exception as exc:  # noqa: BLE001 - renewal must not die silently
                log.warning("core_renew_error error=%s", type(exc).__name__)

    async def release(self, outcome: str, nbytes: int | None = None) -> None:
        if self.released:
            return
        self.released = True
        if self.renewal:
            self.renewal.cancel()
        if not self.ticket:
            return
        request = {"action": "release", "ticket": self.ticket, "outcome": outcome if OUTCOME.fullmatch(outcome or "") else "failed"}
        if isinstance(nbytes, int) and nbytes >= 0:
            request["bytes"] = nbytes
        try:
            answer = await call(self.queue.path, request)
            if answer.get("ok") is not True:
                # refused: the place would wait for its lease (90 s) with nobody promoted
                log.warning("core_release_refused code=%s", str(answer.get("code"))[:40])
        except CoreUnavailable as exc:
            # the core frees the place on its lease anyway
            log.info("core_release_failed reason=%s", exc)


class CoreQueue:
    def __init__(self, path: str | None = SOCKET, *, wait_max_s: float = WAIT_MAX_S, poll_s: float = POLL_S,
                 renew_s: float = RENEW_S, clock=time.monotonic):
        self.path = path
        self.wait_max_s = wait_max_s
        self.poll_s = poll_s
        self.renew_s = renew_s
        self.clock = clock

    async def turn(self, *, key: str, user_id: int | None, platform: str, on_place=None) -> Turn:
        """Waits for the place; `await on_place(n)` whenever the place in the line changes (n >= 1)."""
        if not self.path:
            return Turn(self, None)
        request = {"action": "acquire", "key": key[:80], "platform": platform}
        if user_id is not None:
            request["principal"] = str(user_id)
        started = self.clock()
        shown = None
        ticket = None
        try:
            answer = await call(self.path, request)
            ticket = answer.get("ticket")
            while answer.get("ok") is True and answer.get("state") == "waiting":
                place = answer.get("position")
                if on_place and isinstance(place, int) and place > 0 and place != shown:
                    shown = place
                    try:
                        await on_place(place)
                    except Exception:  # noqa: BLE001 - a status message must never stop the download
                        log.info("core_place_message_failed")
                if self.clock() - started > self.wait_max_s:
                    log.warning("core_wait_exceeded waited_s=%d", int(self.clock() - started))
                    await call(self.path, {"action": "release", "ticket": answer.get("ticket"), "outcome": "wait_exceeded"})
                    return Turn(self, None)
                await asyncio.sleep(self.poll_s)
                status = await call(self.path, {"action": "status", "ticket": answer.get("ticket")})
                # the core restarted or the lease ran out: the same key asks again (it is not counted twice)
                answer = {**status, "ticket": answer.get("ticket")} if status.get("ok") else await call(self.path, request)
            if answer.get("ok") is not True:
                log.warning("core_refused code=%s", str(answer.get("code"))[:40])
                return Turn(self, None)
            return Turn(self, answer.get("ticket"))
        except CoreUnavailable as exc:
            log.warning("core_unavailable reason=%s", exc)
            return Turn(self, None)
        except BaseException:
            # cancelled (the job timed out, the bot stops) while waiting: the place must not stay taken until the lease
            if ticket:
                try:
                    await asyncio.shield(call(self.path, {"action": "release", "ticket": ticket, "outcome": "cancelled"}))
                except (CoreUnavailable, asyncio.CancelledError):
                    pass
            raise
