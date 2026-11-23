"""Bounded HTTP range downloads using yt-dlp's existing proxy/cookie session."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import os
from pathlib import Path
import re
import threading
import time

from yt_dlp.downloader import PROTOCOL_MAP
from yt_dlp.downloader.http import HttpFD
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import TransportError, HTTPError


CHUNK_BYTES = 8 * 1024 * 1024
MIN_PARALLEL_BYTES = 32 * 1024 * 1024


class RangeDownloadError(Exception):
    """The source cannot reliably serve the requested byte ranges."""


def download_ranges(ydl, info, target, *, connections, progress, chunk_bytes=CHUNK_BYTES,
                    range_ready=None):
    """Write disjoint ranges directly to one file, validating every response.

    No URLs or cookie values leave the yt-dlp session. Callbacks are serialized;
    cancellation by the parent still kills the whole isolated worker process.
    """
    size = info['filesize']
    stopped = threading.Event()
    lock = threading.Lock()
    received = 0
    headers = {k: v for k, v in info.get('http_headers', {}).items()
               if k.lower() not in ('range', 'accept-encoding')}
    headers['Accept-Encoding'] = 'identity'

    with open(target, 'wb') as output:
        def fetch(start):
            nonlocal received
            end = min(size, start + chunk_bytes) - 1
            for attempt in range(3):
                if stopped.is_set():
                    return
                try:
                    request = Request(info['url'], headers={**headers, 'Range': f'bytes={start}-{end}'})
                    with ydl.urlopen(request) as response:
                        match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',
                                             response.headers.get('Content-Range', ''))
                        if response.status != 206 or not match or tuple(map(int, match.groups())) != (start, end, size):
                            raise RangeDownloadError('range_not_supported')
                        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                            raise RangeDownloadError('encoded_range')
                        # A chunk is small and committed only after complete validation.
                        # At most connections * 8 MiB of payload is held in memory.
                        data = bytearray()
                        while len(data) <= end - start:
                            if stopped.is_set():
                                return
                            block = response.read(min(65536, end - start + 1 - len(data)))
                            if not block:
                                raise RangeDownloadError('short_range')
                            data.extend(block)
                        if response.read(1):
                            raise RangeDownloadError('oversized_range')
                    offset = 0
                    while offset < len(data):
                        written = os.pwrite(output.fileno(), memoryview(data)[offset:], start + offset)
                        if not written:
                            raise OSError('short_write')
                        offset += written
                    with lock:
                        if range_ready:
                            range_ready(start, end + 1)
                        received += len(data)
                        progress(received)
                    return
                except (TransportError, HTTPError):
                    if attempt == 2:
                        raise RangeDownloadError('range_request_failed') from None
                except RangeDownloadError:
                    raise

        try:
            # Probe with the first real chunk, before opening the remaining connections.
            fetch(0)
            with ThreadPoolExecutor(max_workers=connections) as pool:
                futures = [pool.submit(fetch, start) for start in range(chunk_bytes, size, chunk_bytes)]
                try:
                    for future in as_completed(futures):
                        future.result()
                finally:
                    stopped.set()
                    for future in futures:
                        future.cancel()
            if received != size or os.fstat(output.fileno()).st_size != size:
                raise RangeDownloadError('incomplete_file')
        finally:
            stopped.set()


class ParallelHttpFD(HttpFD):
    def real_download(self, filename, info_dict):
        size = info_dict.get('filesize')
        count = self.params.get('video_pls_connections', 8)
        temporary = self.temp_name(filename)
        if (count <= 1 or not isinstance(size, int) or size < MIN_PARALLEL_BYTES
                or size > self.params.get('max_filesize', 2_000_000_000)
                or filename == '-' or temporary == filename or Path(temporary).exists()
                or info_dict.get('request_data') or info_dict.get('impersonate')
                or self.params.get('test') or self.params.get('ratelimit')
                or any(k.lower() == 'range' for k in info_dict.get('http_headers', {}))):
            return super().real_download(filename, info_dict)
        started = time.monotonic()

        def progress(received, status='downloading'):
            elapsed = time.monotonic() - started
            self._hook_progress({'status': status, 'filename': filename,
                                 'tmpfilename': temporary, 'downloaded_bytes': received,
                                 'total_bytes': size, 'elapsed': elapsed,
                                 'speed': received / max(elapsed, 0.001)}, info_dict)

        try:
            download_ranges(self.ydl, info_dict, temporary, connections=min(count, 8), progress=progress)
        except RangeDownloadError:
            # Never resume native HTTP from a file containing out-of-order ranges.
            Path(temporary).unlink(missing_ok=True)
            return super().real_download(filename, info_dict)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        self.try_rename(temporary, filename)
        progress(size, 'finished')
        return True


@contextmanager
def parallel_http_downloads():
    """Workers handle one job at a time; other platforms keep native downloaders."""
    previous = {protocol: PROTOCOL_MAP.get(protocol) for protocol in ('http', 'https')}
    try:
        PROTOCOL_MAP.update(http=ParallelHttpFD, https=ParallelHttpFD)
        yield
    finally:
        for protocol, downloader in previous.items():
            if downloader is None:
                PROTOCOL_MAP.pop(protocol, None)
            else:
                PROTOCOL_MAP[protocol] = downloader
