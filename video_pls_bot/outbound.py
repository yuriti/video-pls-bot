"""One paced admission queue per bot; network transfers run independently."""

import asyncio
import time
from collections import OrderedDict


class Outbound:
    def __init__(self, *, ceiling=25.0, chat_interval=1.05, group_interval=3.05,
                 clock=time.monotonic):
        self.ceiling = self.rate = ceiling
        self.chat_interval = chat_interval
        self.group_interval = group_interval
        self.clock = clock
        self.next_request = self.blocked_until = self.last_recovery = 0.0
        self.chats = OrderedDict()
        self.waiters = (OrderedDict(), OrderedDict())
        self.changed = asyncio.Event()
        self.task = None
        self.closed = False

    async def acquire(self, chat_id=None, *, background=False):
        if self.closed:
            raise asyncio.CancelledError
        future = asyncio.get_running_loop().create_future()
        queue = self.waiters[int(background)]
        queue[future] = chat_id
        self.changed.set()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._pump())
        try:
            await future
        finally:
            queue.pop(future, None)
            self.changed.set()

    def limited(self, retry_after):
        now = self.clock()
        self.blocked_until = max(self.blocked_until, now + max(1, retry_after))
        self.rate = max(1.0, self.rate * 0.7)
        self.last_recovery = self.blocked_until
        self.changed.set()

    def succeeded(self):
        now = self.clock()
        # At most one small recovery step per second, regardless of concurrency.
        if now > max(self.blocked_until, self.last_recovery) + 1:
            self.rate = min(self.ceiling, self.rate + 0.1)
            self.last_recovery = now

    async def _pump(self):
        while any(self.waiters):
            self.changed.clear()
            now = self.clock()
            # Expired entries need no permanent per-user history.
            while self.chats and next(iter(self.chats.values())) <= now:
                self.chats.popitem(last=False)
            delay = max(self.next_request, self.blocked_until) - now
            if delay <= 0:
                eligible = None
                earliest = float('inf')
                for queue in self.waiters:  # foreground before progress
                    for future, chat in list(queue.items()):
                        if future.done():
                            queue.pop(future, None)
                            continue
                        ready = self.chats.get(chat, 0)
                        if ready <= now:
                            eligible = (queue, future, chat)
                            break
                        earliest = min(earliest, ready)
                    if eligible:
                        break
                if eligible:
                    queue, future, chat = eligible
                    queue.pop(future)
                    self.next_request = now + 1 / self.rate
                    if chat is not None:
                        interval = self.group_interval if isinstance(chat, int) and chat < 0 else self.chat_interval
                        self.chats[chat] = now + interval
                        # Group and private intervals differ; sort by expiry.
                        self.chats = OrderedDict(sorted(self.chats.items(), key=lambda item: item[1]))
                    future.set_result(None)
                    continue
                delay = earliest - now
                if not any(self.waiters):
                    break
            try:
                await asyncio.wait_for(self.changed.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def close(self):
        self.closed = True
        for queue in self.waiters:
            for future in queue:
                future.cancel()
            queue.clear()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
