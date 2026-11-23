"""One-pass MP4 merge with a front moov reserve and normal merger fallback."""

import importlib
import math
import struct
from contextlib import contextmanager
from pathlib import Path

from yt_dlp.postprocessor.ffmpeg import FFmpegMergerPP, FFmpegPostProcessorError


def moov_bytes(path):
    size = Path(path).stat().st_size
    with open(path, 'rb') as stream:
        offset = 0
        while offset + 8 <= size:
            stream.seek(offset)
            length, kind = struct.unpack('>I4s', stream.read(8))
            header = 8
            if length == 1:
                raw = stream.read(8)
                if len(raw) != 8:
                    return None
                length = struct.unpack('>Q', raw)[0]
                header = 16
            elif length == 0:
                length = size - offset
            if length < header or offset + length > size:
                return None
            if kind == b'moov':
                return length
            offset += length
    return None


def reserve_bytes(sources, info):
    sizes = [moov_bytes(path) for path in sources]
    if not all(sizes):
        return None
    # Fragmented source MP4s have tiny moov atoms; their sample tables live in
    # moof boxes. Estimate the flattened tables from duration and frame rates.
    per_second = 0
    for fmt in info.get('requested_formats', []):
        if fmt.get('vcodec') not in (None, 'none'):
            per_second += (fmt.get('fps') or 60) * 32
        if fmt.get('acodec') not in (None, 'none'):
            per_second += (fmt.get('asr') or 48000) / 960 * 16
    needed = max(sum(sizes) * 2, per_second * (info.get('duration') or 0)) + 1024**2
    reserve = math.ceil(needed / 524288) * 524288
    return reserve if reserve <= 16 * 1024**2 else None


class ReservedMoovMerger(FFmpegMergerPP):
    @classmethod
    def pp_key(cls):
        return 'Merger'

    def _configuration_args(self, *args, **kwargs):
        # yt-dlp can return a shared default list. Never mutate it: doing so
        # leaks output-only flags into later input commands in the warm worker.
        result = list(super()._configuration_args(*args, **kwargs))
        keys = args[1] if len(args) > 1 else kwargs.get('keys', [])
        if getattr(self, '_reserve', None) and '_o' in keys:
            result += ['-movflags', '-faststart', '-moov_size', str(self._reserve)]
        return result

    def run(self, info):
        sources = info.get('__files_to_merge', [])
        self._reserve = None
        if (str(info.get('filepath', '')).endswith('.mp4') and sources
                and sum(Path(p).stat().st_size for p in sources) >= 128 * 1024**2):
            self._reserve = reserve_bytes(sources, info)
        try:
            result = super().run(info)
            limit = self.get_param('max_filesize')
            if self._reserve and limit and Path(info['filepath']).stat().st_size > limit:
                # Reserved free space must not make an otherwise deliverable
                # file exceed Telegram's limit. Reuse the intact source streams.
                self._reserve = None
                return super().run(info)
            return result
        except FFmpegPostProcessorError:
            if self._reserve is None:
                raise
            # Sources remain intact; FFmpeg -y replaces only the temporary mux.
            self._reserve = None
            return super().run(info)


@contextmanager
def fast_mp4_merges():
    module = importlib.import_module('yt_dlp.YoutubeDL')
    previous = module.FFmpegMergerPP
    module.FFmpegMergerPP = ReservedMoovMerger
    try:
        yield
    finally:
        module.FFmpegMergerPP = previous
