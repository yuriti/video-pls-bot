"""Fixed, warm subprocess pool with hard cancellation of the active worker."""

import asyncio
import json
import sys
import time
from collections import OrderedDict

from .process import ProcessError, stop_process_group, watch_workspace


class ExtractorPool:
    def __init__(self, settings, command=None):
        self.settings = settings
        self.command = command or (sys.executable, "-m", "video_pls_bot.extract", "--serve")
        self.available = asyncio.Queue(maxsize=settings.disk_concurrency)
        self.workers = set()
        self.closed = False
        self.handoffs = OrderedDict()

    async def spawn(self):
        child = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=1_000_000,
        )
        self.workers.add(child)
        try:
            async with asyncio.timeout(15):
                ready = json.loads(await child.stdout.readline())
            if ready != {"ready": True}:
                raise ProcessError("process_failed")
            return child
        except BaseException:
            await stop_process_group(child)
            self.workers.discard(child)
            raise

    async def start(self):
        try:
            # Sequential warm-up avoids a CPU spike while bringing the service up.
            for _ in range(self.settings.disk_concurrency):
                self.available.put_nowait(await self.spawn())
        except BaseException:
            await self.close()
            raise

    async def download(self, link, directory, *, format_id=None, progress=None, metadata_handle=None):
        if self.closed:
            raise ProcessError("process_failed")
        preferred = self.handoffs.pop(metadata_handle, (None, 0))[0]
        # Prefer the idle worker holding this request's metadata. Never wait
        # behind a busy worker just to save an extraction; fall back normally.
        idle = []
        while not self.available.empty():
            idle.append(self.available.get_nowait())
        child = preferred if preferred in idle else (idle[0] if idle else None)
        for worker in idle:
            if worker is not child:
                self.available.put_nowait(worker)
        if child is None:
            child = await self.available.get()
        reader = watcher = None
        reusable = False
        try:
            if child.returncode is not None:
                await stop_process_group(child)
                self.workers.discard(child)
                child = await self.spawn()
            request = {"url": link.url, "directory": str(directory), "proxy": self.settings.proxy,
                       "max_bytes": self.settings.max_bytes, "max_duration": self.settings.max_duration,
                       "node": self.settings.node, "cookies_file": str(self.settings.cookies_for(link.platform))
                       if self.settings.cookies_for(link.platform) else None,
                       "format_id": format_id, "report_progress": progress is not None,
                       "metadata_handle": metadata_handle, "retain_metadata": True}
            async def read_result():
                while True:
                    line = await child.stdout.readline()
                    try:
                        result = json.loads(line)
                    except (ValueError, UnicodeError):
                        raise ProcessError("process_failed") from None
                    if isinstance(result, dict) and "progress" in result:
                        if progress:
                            await progress(result["progress"])
                        continue
                    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
                        raise ProcessError("process_failed")
                    return result

            async with asyncio.timeout(self.settings.timeout - 30):
                child.stdin.write((json.dumps(request) + "\n").encode())
                await child.stdin.drain()
                reader = asyncio.create_task(read_result())
                watcher = asyncio.create_task(watch_workspace(directory, self.settings.workspace_bytes))
                done, _ = await asyncio.wait((reader, watcher), return_when=asyncio.FIRST_COMPLETED)
                if watcher in done:
                    watcher.result()
                result = await reader
                for key, (_, expires) in list(self.handoffs.items()):
                    if expires <= time.monotonic():
                        del self.handoffs[key]
                if result.get('metadata_handle'):
                    self.handoffs[result['metadata_handle']] = (child, time.monotonic() + 120)
                    while len(self.handoffs) > 32:
                        self.handoffs.popitem(last=False)
                reusable = True
                return result
        finally:
            for task in (reader, watcher):
                if task:
                    task.cancel()
            await asyncio.gather(*(task for task in (reader, watcher) if task), return_exceptions=True)
            if not reusable or self.closed:
                await stop_process_group(child)
                self.workers.discard(child)
                if not self.closed:
                    self.available.put_nowait(await self.spawn())
            else:
                self.available.put_nowait(child)

    async def close(self):
        self.closed = True
        await asyncio.gather(*(stop_process_group(child) for child in tuple(self.workers)), return_exceptions=True)
        self.workers.clear()
        self.handoffs.clear()
