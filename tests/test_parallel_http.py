import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from yt_dlp import YoutubeDL
from yt_dlp.downloader import PROTOCOL_MAP

from video_pls_bot.extract import MediaRejected, QuietLogger
from video_pls_bot.parallel_http import ParallelHttpFD, download_ranges, parallel_http_downloads


DATA = bytes(range(251)) * 8000


@contextlib.contextmanager
def source(mode='normal'):
    state = {'active': 0, 'peak': 0, 'ranges': 0, 'full': 0, 'failed_once': False}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            header = self.headers.get('Range')
            start, end = (map(int, header.removeprefix('bytes=').split('-'))
                          if header else (0, len(DATA) - 1))
            with lock:
                state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
                state['ranges' if header else 'full'] += 1
            try:
                time.sleep(0.01)
                if mode == 'retry' and header and not state['failed_once']:
                    state['failed_once'] = True
                    self.send_response(503)
                    self.end_headers()
                    return
                ranged = header and mode != 'ignore'
                body = DATA[start:end + 1] if ranged else DATA
                if mode == 'short' and header:
                    body = body[:-1]
                self.send_response(206 if ranged else 200)
                if ranged:
                    total = len(DATA) + (1 if mode == 'wrong_total' else 0)
                    self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    state['active'] -= 1

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/video', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class ParallelHttpTests(unittest.TestCase):
    def downloader(self, ydl):
        downloader = ParallelHttpFD(ydl, {**ydl.params, 'video_pls_connections': 4})
        return downloader

    def options(self):
        return {'quiet': True, 'no_warnings': True, 'logger': QuietLogger(),
                'proxy': '', 'socket_timeout': 2, 'max_filesize': len(DATA) * 2,
                'cachedir': False}

    def test_out_of_order_download_is_identical_and_progress_is_serialized(self):
        with source() as (url, state), tempfile.TemporaryDirectory() as tmp, YoutubeDL(self.options()) as ydl:
            path = Path(tmp) / 'result'
            progress = []
            download_ranges(ydl, {'url': url, 'filesize': len(DATA)}, path,
                            connections=4, chunk_bytes=65536, progress=progress.append)
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), hashlib.sha256(DATA).digest())
            self.assertGreater(state['peak'], 1)
            self.assertLessEqual(state['peak'], 4)
            self.assertEqual(progress, sorted(set(progress)))
            self.assertEqual(progress[-1], len(DATA))

    def test_bad_ranges_fall_back_to_native_without_reusing_sparse_file(self):
        for mode in ('ignore', 'wrong_total', 'short'):
            with self.subTest(mode=mode), source(mode) as (url, state), tempfile.TemporaryDirectory() as tmp, YoutubeDL(self.options()) as ydl:
                path = Path(tmp) / 'video.mp4'
                with patch('video_pls_bot.parallel_http.MIN_PARALLEL_BYTES', 1):
                    self.assertTrue(self.downloader(ydl).real_download(str(path), {'url': url, 'filesize': len(DATA)}))
                self.assertEqual(path.read_bytes(), DATA)
                self.assertGreaterEqual(state['full'], 1)
                self.assertFalse(Path(str(path) + '.part').exists())

    def test_network_failure_is_retried_without_double_counting_bytes(self):
        with source('retry') as (url, state), tempfile.TemporaryDirectory() as tmp, YoutubeDL(self.options()) as ydl:
            progress = []
            path = Path(tmp) / 'video.mp4'
            download_ranges(ydl, {'url': url, 'filesize': len(DATA)}, path,
                            connections=4, chunk_bytes=65536, progress=progress.append)
            self.assertTrue(state['failed_once'])
            self.assertEqual(progress[-1], len(DATA))
            self.assertEqual(path.read_bytes(), DATA)

    def test_budget_failure_stops_threads_and_does_not_fall_back(self):
        with source() as (url, state), tempfile.TemporaryDirectory() as tmp, YoutubeDL(self.options()) as ydl:
            path = Path(tmp) / 'video.mp4'
            downloader = self.downloader(ydl)
            def budget(state):
                raise MediaRejected('size_limit')
            downloader.add_progress_hook(budget)
            with patch('video_pls_bot.parallel_http.MIN_PARALLEL_BYTES', 1):
                with self.assertRaisesRegex(MediaRejected, 'size_limit'):
                    downloader.real_download(str(path), {'url': url, 'filesize': len(DATA)})
            self.assertEqual(state['full'], 0)
            self.assertFalse(list(Path(tmp).iterdir()))

    def test_native_downloader_registration_restored_after_failure(self):
        before = dict(PROTOCOL_MAP)
        with self.assertRaises(ValueError):
            with parallel_http_downloads():
                self.assertIs(PROTOCOL_MAP['https'], ParallelHttpFD)
                raise ValueError()
        self.assertEqual(PROTOCOL_MAP, before)

    def test_progress_failure_joins_inflight_workers(self):
        with source() as (url, state), tempfile.TemporaryDirectory() as tmp, YoutubeDL(self.options()) as ydl:
            calls = []
            def budget(received):
                calls.append(received)
                if received > 65536:
                    raise MediaRejected('size_limit')
            with self.assertRaisesRegex(MediaRejected, 'size_limit'):
                download_ranges(ydl, {'url': url, 'filesize': len(DATA)}, Path(tmp) / 'partial',
                                connections=4, chunk_bytes=65536, progress=budget)
            # No worker may continue writing after the method returns to cleanup.
            count = len(calls)
            time.sleep(0.05)
            self.assertEqual(len(calls), count)
            self.assertEqual(state['active'], 0)


if __name__ == '__main__':
    unittest.main()
