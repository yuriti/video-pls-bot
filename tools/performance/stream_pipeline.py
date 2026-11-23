"""Experimental bounded download → mux → MTProto upload. Never sends messages.

Not wired into the bot. A loopback server serves only fully validated ranges;
the uploader defers the MP4 header and the final part until mux succeeds.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time

from video_pls_bot.parallel_http import CHUNK_BYTES, download_ranges

PART = 512 * 1024


class RangeSource:
    def __init__(self, path, size):
        self.path, self.size = Path(path), size
        self.condition = threading.Condition()
        self.ready = set()
        self.stopped = False

    def committed(self, start, end):
        with self.condition:
            if self.stopped:
                raise OSError('source_stopped')
            self.ready.add(start // CHUNK_BYTES)
            self.condition.notify_all()

    def stop(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()

    def read(self, offset, length):
        chunk = offset // CHUNK_BYTES
        length = min(length, (chunk + 1) * CHUNK_BYTES - offset, self.size - offset)
        with self.condition:
            if not self.condition.wait_for(lambda: self.stopped or chunk in self.ready, timeout=60):
                raise TimeoutError('source_range_timeout')
            if self.stopped:
                raise OSError('source_stopped')
        with self.path.open('rb') as stream:
            stream.seek(offset)
            data = stream.read(length)
        if len(data) != length:
            raise OSError('incomplete_committed_range')
        return data


class RangeServer:
    def __init__(self, sources):
        token = secrets.token_hex(16)
        routes = {f'/{token}/{i}': source for i, source in enumerate(sources)}
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args): pass
            def do_GET(self):
                source = routes.get(self.path)
                if source is None:
                    self.send_error(404)
                    return
                match = re.fullmatch(r'bytes=(\d+)-(\d*)', self.headers.get('Range', 'bytes=0-'))
                if not match:
                    self.send_error(416)
                    return
                start = int(match[1])
                end = min(source.size - 1, int(match[2]) if match[2] else source.size - 1)
                if not 0 <= start <= end < source.size:
                    self.send_error(416)
                    return
                self.send_response(206)
                self.send_header('Content-Range', f'bytes {start}-{end}/{source.size}')
                self.send_header('Content-Length', str(end-start+1))
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()
                try:
                    while start <= end:
                        data = source.read(start, min(65536, end-start+1))
                        self.wfile.write(data)
                        start += len(data)
                except (OSError, TimeoutError):
                    self.close_connection = True
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.urls = [f'http://127.0.0.1:{self.server.server_port}{route}' for route in routes]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


async def upload_growing(path, producer, send, *, prefix, window=8, max_bytes=2_000_000_000):
    """send(index, total_parts, bytes) must acknowledge each uploaded part.

    Producer completion is a trusted barrier: it must mean successful mux and
    completed source downloads. No file or Telegram media reference is published.
    """
    if not 1 <= window <= 32 or prefix < 0:
        raise ValueError('invalid_upload_window')
    prefix_parts = math.ceil(prefix / PART)
    next_part = prefix_parts
    pending = set()
    sent = 0

    async def one(index, total, data):
        nonlocal sent
        if not await send(index, total, data):
            raise OSError('part_rejected')
        sent += len(data)

    async def drain(all_pending=False):
        nonlocal pending
        if pending:
            done, pending = await asyncio.wait(pending, return_when=(
                asyncio.ALL_COMPLETED if all_pending else asyncio.FIRST_COMPLETED))
            results = await asyncio.gather(*done, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result

    def read(index, length):
        with Path(path).open('rb') as stream:
            stream.seek(index * PART)
            data = stream.read(length)
        if len(data) != length:
            raise OSError('short_output_part')
        return data

    try:
        while not producer.done():
            size = Path(path).stat().st_size if Path(path).exists() else 0
            if size > max_bytes:
                raise OSError('size_limit')
            # Leave at least one byte for the final, total-bearing part.
            if size > (next_part + 1) * PART:
                pending.add(asyncio.create_task(one(next_part, -1, read(next_part, PART))))
                next_part += 1
                if len(pending) >= window:
                    await drain()
            else:
                if pending:
                    await drain()
                else:
                    await asyncio.sleep(0.05)
        await producer  # Never finalize parts after a failed/truncated mux.
        size = Path(path).stat().st_size
        if not 0 < size <= max_bytes:
            raise OSError('size_limit')
        total = math.ceil(size / PART)
        for index in [*range(min(prefix_parts, total - 1)), *range(next_part, total - 1)]:
            pending.add(asyncio.create_task(one(index, -1, read(index, PART))))
            if len(pending) >= window:
                await drain()
        await drain(True)
        await one(total - 1, total, read(total - 1, size - (total - 1) * PART))
        assert sent == size
        return total
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
