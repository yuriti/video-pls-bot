import asyncio
import copy
import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from yt_dlp import YoutubeDL
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessorError, FFmpegMergerPP

from video_pls_bot.fast_mux import ReservedMoovMerger, moov_bytes
from video_pls_bot.metadata import MetadataHandoff
from video_pls_bot.extract import download
from test_large_videos import INFO


class HandoffTests(unittest.TestCase):
    def test_single_use_expiry_wrong_url_and_count_bound(self):
        now = [0]
        store = MetadataHandoff(ttl=2, limit=2, clock=lambda: now[0])
        one = store.put('a', {'formats': [1]})
        two = store.put('b', {'formats': [2]})
        three = store.put('c', {'formats': [3]})
        self.assertIsNone(store.take(one, 'a'))
        self.assertIsNone(store.take(two, 'wrong'))
        self.assertIsNone(store.take(two, 'b'))
        self.assertEqual(store.take(three, 'c'), {'formats': [3]})
        self.assertIsNone(store.take(three, 'c'))
        key = store.put('a', {})
        now[0] = 3
        self.assertIsNone(store.take(key, 'a'))
        self.assertIsNone(store.put('a', {'x': 'x' * 2_000_001}))

    def test_selection_consumes_metadata_once_and_expired_cdn_refetches(self):
        from yt_dlp.utils import DownloadError
        with tempfile.TemporaryDirectory() as tmp, patch('video_pls_bot.extract.YoutubeDL') as cls:
            worker = cls.return_value.__enter__.return_value
            worker.extract_info.side_effect = lambda *a, **k: copy.deepcopy(INFO)
            args = ('https://youtu.be/pySJJbS-1oo', Path(tmp), '', 2_000_000_000, 0, 'node')
            selected = download(*args, retain_metadata=True)
            def save(*a, **k):
                (Path(tmp) / 'video.mp4').write_bytes(b'test')
                return {'title': 'Test'}
            worker.process_ie_result.side_effect = save
            result = download(*args, format_id='136+140', metadata_handle=selected['metadata_handle'])
            self.assertEqual(worker.extract_info.call_count, 1)
            self.assertEqual(result['timings']['metadata_reused'], 1)
            selected = download(*args, retain_metadata=True)
            calls = [0]
            def fail_then_save(*a, **k):
                calls[0] += 1
                if calls[0] == 1:
                    (Path(tmp) / 'video.part').write_bytes(b'partial')
                    raise DownloadError('expired')
                return save()
            worker.process_ie_result.side_effect = fail_then_save
            result = download(*args, format_id='136+140', metadata_handle=selected['metadata_handle'])
            self.assertTrue(result['ok'])
            self.assertFalse((Path(tmp) / 'video.part').exists())
            self.assertEqual(worker.extract_info.call_count, 3)


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
class MuxTests(unittest.TestCase):
    def test_real_merge_reserves_front_moov_preserves_stream_and_retries_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.mp4'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                            'color=blue:s=160x90:d=1', '-c:v', 'libx264', '-threads', '1',
                            str(source)], check=True)
            # Valid sparse padding enables the large-file branch without a
            # huge fixture or a costly encode. Demuxers skip the free atom.
            with source.open('r+b') as stream:
                stream.seek(0, 2)
                stream.write(struct.pack('>I4s', 128 * 1024**2, b'free'))
                stream.seek(128 * 1024**2 - 9, 1)
                stream.write(b'\0')
            info = {'filepath': str(root / 'video.mp4'), '__files_to_merge': [str(source)],
                    'requested_formats': [{'acodec': 'none', 'vcodec': 'h264', 'protocol': 'https'}],
                    'ext': 'mp4'}
            with YoutubeDL({'quiet': True, 'no_warnings': True}) as ydl:
                merger = ReservedMoovMerger(ydl)
                merger.run(info)
                self.assertIsNotNone(merger._reserve)
                self.assertIsNotNone(moov_bytes(root / 'video.mp4'))
                def decoded(path):
                    return subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(path),
                                                    '-f', 'framemd5', '-'])
                self.assertEqual(decoded(source), decoded(root / 'video.mp4'))
                with (root / 'video.mp4').open('rb') as stream:
                    head = stream.read(128)
                self.assertIn(b'moov', head)
                # A second merge in the same warm worker must not inherit
                # output-only options as input flags from yt-dlp's defaults.
                FFmpegMergerPP(ydl).run({**info, 'filepath': str(root / 'second.mp4')})
                self.assertEqual(decoded(source), decoded(root / 'second.mp4'))
                ydl.params['max_filesize'] = 5000
                merger.run({**info, 'filepath': str(root / 'limited.mp4')})
                self.assertLess((root / 'limited.mp4').stat().st_size, 5000)
                self.assertIsNone(merger._reserve)
                self.assertEqual(decoded(source), decoded(root / 'limited.mp4'))
                with patch.object(FFmpegMergerPP, 'run', side_effect=[FFmpegPostProcessorError('reserve'), ([], info)]) as run:
                    merger.run(info)
                    self.assertEqual(run.call_count, 2)
                    self.assertIsNone(merger._reserve)
