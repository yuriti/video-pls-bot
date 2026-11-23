"""One bounded, in-memory queue shared by all bot handlers in one process."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueueLimits:
    concurrency: int = 2
    max_waiting: int = 20
    timeout_seconds: float = 180

    def __post_init__(self) -> None:
        if self.concurrency < 1 or self.max_waiting < 0 or self.timeout_seconds <= 0:
            raise ValueError("Invalid queue limits")


class AdmissionStatus(StrEnum):
    ACCEPTED = "accepted"
    USER_BUSY = "user_busy"
    QUEUE_FULL = "queue_full"
    CLOSED = "closed"


class Outcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Ticket:
    job_id: str
    _result: asyncio.Future[Outcome]

    async def wait(self) -> Outcome:
        # A handler disconnecting must not cancel the underlying job/result.
        return await asyncio.shield(self._result)


@dataclass(frozen=True)
class Admission:
    status: AdmissionStatus
    ticket: Ticket | None = None


@dataclass(frozen=True)
class _Job:
    bot_id: int
    user_id: int
    run: Callable[[], Awaitable[None]]
    ticket: Ticket


class DownloadQueue:
    """FIFO admission with server-wide concurrency and user deduplication.

    Instantiate exactly once, then pass the same instance to every bot handler.
    A job must cover download, video upload, MP3 conversion/upload and cleanup.
    It must await its subprocesses and stop them in a finally block on timeout.
    No URLs, tokens, media or Telegram file IDs are retained here.
    """

    def __init__(self, limits: QueueLimits | None = None) -> None:
        self.limits = limits or QueueLimits()
        self._waiting: deque[_Job] = deque()
        self._active: dict[str, asyncio.Task[None]] = {}
        self._users: set[int] = set()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def running(self) -> int:
        return len(self._active)

    @property
    def waiting(self) -> int:
        return len(self._waiting)

    def submit(
        self,
        *,
        bot_id: int,
        user_id: int,
        run: Callable[[], Awaitable[None]],
    ) -> Admission:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise RuntimeError("All bots must use the same event loop and queue")
        self._loop = loop
        if self._closed:
            return Admission(AdmissionStatus.CLOSED)
        # Telegram user IDs are shared across bots: the key excludes bot_id.
        if user_id in self._users:
            return Admission(AdmissionStatus.USER_BUSY)
        if self.running >= self.limits.concurrency and self.waiting >= self.limits.max_waiting:
            return Admission(AdmissionStatus.QUEUE_FULL)

        ticket = Ticket(uuid4().hex, loop.create_future())
        job = _Job(bot_id, user_id, run, ticket)
        self._users.add(user_id)
        # Admission has no await: checking limits and reserving capacity is atomic
        # relative to all other handlers on this event loop.
        if self.running < self.limits.concurrency:
            self._launch(job)
        else:
            self._waiting.append(job)
        return Admission(AdmissionStatus.ACCEPTED, ticket)

    def _launch(self, job: _Job) -> None:
        assert self._loop is not None
        self._active[job.ticket.job_id] = self._loop.create_task(self._execute(job))

    async def _execute(self, job: _Job) -> None:
        outcome = Outcome.FAILED
        try:
            async with asyncio.timeout(self.limits.timeout_seconds):
                await job.run()
            outcome = Outcome.COMPLETED
        except TimeoutError:
            outcome = Outcome.TIMED_OUT
        except asyncio.CancelledError:
            outcome = Outcome.CANCELLED
            raise
        except Exception:
            # Extractor/subprocess exceptions can contain credential-bearing URLs.
            # Record only the outcome; analytics can observe the ticket result.
            outcome = Outcome.FAILED
        finally:
            logger.info(
                "job_finished job_id=%s bot_id=%s outcome=%s",
                job.ticket.job_id,
                job.bot_id,
                outcome.value,
            )
            self._users.discard(job.user_id)
            self._active.pop(job.ticket.job_id, None)
            if not job.ticket._result.done():
                job.ticket._result.set_result(outcome)
            while self._waiting and self.running < self.limits.concurrency:
                self._launch(self._waiting.popleft())

    async def close(self) -> None:
        """Reject new requests and finish all previously admitted jobs."""
        self._closed = True
        while self._active:
            # Shield jobs from a cancelled shutdown waiter. Individual jobs have
            # a timeout; cleanup finishes before their capacity is released.
            await asyncio.shield(asyncio.gather(*tuple(self._active.values()), return_exceptions=True))
