"""Isolated yt-dlp worker. Stdout is a small JSON result, never raw logs/URLs."""

import argparse
from contextlib import ExitStack, nullcontext
import os
import tempfile
import json
import math
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests as curl_requests

from yt_dlp import YoutubeDL
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.utils import DownloadError

from .links import parse_link
from .texts import t
from .parallel_http import parallel_http_downloads
from .metadata import handoff
from .fast_mux import fast_mp4_merges


class MediaRejected(Exception):
    pass


class QuietLogger:
    def debug(self, *args, **kwargs): pass
    def info(self, *args, **kwargs): pass
    def warning(self, *args, **kwargs): pass
    def error(self, *args, **kwargs): pass


def classify_error(message: str) -> str:
    text = message.lower()
    if any(word in text for word in ("log in for access", "login required", "requiring login", "sign in", "log into an account")):
        return "source_login_required"
    if any(word in text for word in ("sign in", "login", "log in", "cookies", "private", "not available", "unavailable", "removed", "403", "429", "challenge")):
        return "source_unavailable"
    if "size_limit" in text:
        return "size_limit"
    if "duration_limit" in text:
        return "duration_limit"
    return "download_failed"


def resolve_tiktok_short(url: str, proxy: str) -> str:
    """Stop at the canonical redirect instead of downloading its HEAD first."""
    parsed = urlsplit(url)
    if parsed.hostname not in ("vm.tiktok.com", "vt.tiktok.com") and not parsed.path.startswith("/t/"):
        return url
    try:
        with curl_requests.Session(impersonate="chrome", proxy=proxy, timeout=2) as session:
            response = session.head(url, allow_redirects=False)
            if response.status_code not in (301, 302, 303, 307, 308):
                return url
            target = parse_link(urljoin(url, response.headers.get("location", "")))
            if target.platform == "tiktok" and "/video/" in urlsplit(target.url).path:
                return target.url
    except Exception:
        pass  # Fall back to yt-dlp's normal resolver without exposing the URL.
    return url


def format_choices(info, max_bytes):
    """Only MP4/M4A pairs we can deliver; estimates include the audio stream."""
    duration = info.get("duration") or 0
    formats = info.get("formats") or ([info] if info.get("format_id") else [])
    audio = [f for f in formats if f.get("ext") == "m4a" and f.get("vcodec") == "none"
             and f.get("acodec") not in (None, "none")]
    audio = max(audio, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    has_audio = any(f.get("acodec") not in (None, "none") for f in formats)

    def estimate(f):
        return f.get("filesize") or f.get("filesize_approx") or (
            math.ceil(duration * f["tbr"] * 125) if duration and f.get("tbr") else None)

    levels = {}
    for f in formats:
        if f.get("ext") != "mp4" or f.get("vcodec") in (None, "none"):
            continue
        if not f.get("width") or not f.get("height"):
            continue
        quality = int(min(f["width"], f["height"]))
        if quality > 1080:
            continue
        streams = ([f] if f.get("acodec") not in (None, "none") else [f, audio] if audio
                   else [f] if not has_audio else [])
        if not streams:
            continue
        ids = [str(s.get("format_id", "")) for s in streams]
        if any(not re.fullmatch(r"[\w.-]+", key) for key in ids):
            continue
        sizes = [estimate(s) for s in streams]
        size = math.ceil(sum(sizes) * 1.03) + 1_000_000 if all(sizes) else None
        if size and size > max_bytes:
            continue
        rank = (str(f.get("vcodec", "")).startswith(("avc", "h264")), f.get("tbr") or 0)
        if quality not in levels or rank > levels[quality][0]:
            levels[quality] = (rank, {"format_id": "+".join(ids), "quality": quality, "bytes": size})
    return [levels[key][1] for key in sorted(levels, reverse=True)][:8]


def download(url: str, directory: Path, proxy: str, max_bytes: int, max_duration: int, node: str,
             cookies_file=None, format_id=None, report_progress=False, inspect_only=False,
             parallel_connections=8, metadata_handle=None, retain_metadata=False, fast_mux=True):
    started = time.perf_counter()
    link = parse_link(url)
    if link.platform == "tiktok":
        url = resolve_tiktok_short(link.url, proxy)
    else:
        url = link.url
    resolved = time.perf_counter()
    received = {}
    last_progress = 0
    postprocess_started = {}
    postprocess_seconds = 0.0

    def postprocess_progress(state):
        nonlocal postprocess_seconds
        key = state.get('postprocessor')
        if state.get('status') == 'started':
            postprocess_started[key] = time.perf_counter()
        elif state.get('status') == 'finished' and key in postprocess_started:
            postprocess_seconds += time.perf_counter() - postprocess_started.pop(key)

    def progress(state):
        nonlocal last_progress
        filename = state.get("filename", "video")
        received[filename] = max(received.get(filename, 0), state.get("downloaded_bytes") or 0)
        if sum(received.values()) > max_bytes:
            raise MediaRejected("size_limit")
        if report_progress and time.monotonic() - last_progress >= 3:
            print(json.dumps({"progress": sum(received.values())}), flush=True)
            last_progress = time.monotonic()

    options = {
        "proxy": proxy, "quiet": True, "no_warnings": True, "logger": QuietLogger(),
        "noplaylist": True, "cachedir": False, "socket_timeout": 15,
        "retries": 2, "fragment_retries": 2, "extractor_retries": 1,
        "concurrent_fragment_downloads": 4, "max_filesize": max_bytes,
        "video_pls_connections": max(1, min(8, parallel_connections)),
        "outtmpl": str(directory / "video.%(ext)s"), "restrictfilenames": True,
        "format": format_id or "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
        "format_sort": ["res:1080", "vcodec:h264", "acodec:aac"],
        "merge_output_format": "mp4", "overwrites": False,
        "progress_hooks": [progress], "js_runtimes": {"node": {"path": node}},
        "postprocessor_hooks": [postprocess_progress],
        "remote_components": set(), "ffmpeg_location": "/usr/bin/ffmpeg" if sys.platform == "linux" else None,
    }
    if link.platform != "youtube":
        options["impersonate"] = ImpersonateTarget(client="chrome")
    with ExitStack() as cleanup:
        if cookies_file:
            # yt-dlp updates its cookie jar on exit. Copy the read-only systemd
            # credential into a private jar. Production points COOKIE_TEMP_DIR to tmpfs.
            root = Path(os.environ.get("COOKIE_TEMP_DIR", directory))
            if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
                raise MediaRejected("download_failed")
            private = Path(cleanup.enter_context(tempfile.TemporaryDirectory(prefix=f"jar-{os.getpid()}-", dir=root)))
            cookies = private / "cookies.txt"
            shutil.copyfile(cookies_file, cookies)
            cookies.chmod(0o600)
            options["cookiefile"] = str(cookies)
        with YoutubeDL(options) as ydl:
            # Links are already platform-validated. Avoid scanning and compiling
            # thousands of unrelated extractors on each fresh worker's first request.
            extractor = {"youtube": "Youtube", "instagram": "Instagram", "tiktok":
                         "TikTok" if "/video/" in urlsplit(url).path else "TikTokVM"}[link.platform]
            selected = extractor if ydl.get_info_extractor(extractor).suitable(url) else None
            info = handoff.take(metadata_handle, url) if metadata_handle else None
            reused_metadata = info is not None
            if info is None:
                info = ydl.extract_info(url, download=False, ie_key=selected)
            extracted = time.perf_counter()
            if not info or info.get("_type", "video") != "video" or info.get("is_live") or info.get("live_status") == "is_upcoming":
                raise MediaRejected("single_video_required")
            duration = info.get("duration")
            # Instagram often omits duration entirely. In that case the bounded
            # download is validated with ffprobe before anything is sent to Telegram.
            if duration is not None and (duration <= 0 or (max_duration and duration > max_duration)):
                raise MediaRejected("duration_limit")
            # For vertical video 1080p means a short edge of at most 1080 pixels.
            if info.get("formats"):
                info["formats"] = [f for f in info["formats"] if not (f.get("width") and f.get("height"))
                                   or min(f["width"], f["height"]) <= 1080]
            choices = format_choices(info, max_bytes)
            needs_choice = (duration and duration > 180) or any((c["bytes"] or 0) > 100_000_000 for c in choices)
            if not format_id and (inspect_only or needs_choice):
                if not choices:
                    raise MediaRejected("size_limit")
                # Only presentation fields leave the worker; raw extractor metadata
                # and signed video stream URLs are never retained in the UI.
                thumbnail = info.get("thumbnail")
                if link.platform == "youtube" and re.fullmatch(r"[A-Za-z0-9_-]{11}", str(info.get("id", ""))):
                    thumbnail = f"https://i.ytimg.com/vi/{info['id']}/hqdefault.jpg"
                if not isinstance(thumbnail, str) or urlsplit(thumbnail).scheme != "https":
                    thumbnail = None
                return {"ok": True, "selection": True, "title": str(info.get("title") or t("title_video"))[:100],
                        "duration": duration, "choices": choices,
                        "thumbnail": thumbnail,
                        "metadata_handle": handoff.put(url, info) if retain_metadata else None}
            if format_id:
                if not re.fullmatch(r"[\w.-]+(?:\+[\w.-]+)?", format_id):
                    raise MediaRejected("download_failed")
                if not any(c["format_id"] == format_id for c in choices):
                    raise MediaRejected("format_unavailable")
            # Reuse extraction: process_ie_result selects/downloads from this metadata.
            with (parallel_http_downloads() if link.platform == 'youtube' else nullcontext(),
                  fast_mp4_merges() if fast_mux else nullcontext()):
                try:
                    result = ydl.process_ie_result(info, download=True)
                except DownloadError as exc:
                    if not reused_metadata or not any(code in str(exc) for code in ('403', '410', 'expired')):
                        raise
                    # A CDN URL can expire early. Retry fresh extraction once;
                    # no already-downloaded output is reused across this retry.
                    for path in directory.glob('video*'):
                        if path.is_file():
                            path.unlink()
                    return download(url, directory, proxy, max_bytes, max_duration, node,
                                    cookies_file, format_id, report_progress, inspect_only,
                                    parallel_connections, fast_mux=fast_mux)
            video = directory / "video.mp4"
            if not video.is_file() or video.stat().st_size > max_bytes:
                raise MediaRejected("size_limit" if video.is_file() else "download_failed")
            return {"ok": True, "duration": duration, "bytes": video.stat().st_size,
                    "title": str(result.get("title") or t("title_audio"))[:100],
                    "timings": {"resolve_s": round(resolved - started, 4),
                                "metadata_s": round(extracted - resolved, 4),
                                "metadata_reused": int(reused_metadata),
                                "postprocess_s": round(postprocess_seconds, 4),
                                "transfer_s": round(time.perf_counter() - extracted, 4)}}


def execute(parameters):
    try:
        parameters = dict(parameters)
        parameters["directory"] = Path(parameters["directory"])
        return download(**parameters)
    except MediaRejected as exc:
        return {"ok": False, "error": str(exc)}
    except DownloadError as exc:
        return {"ok": False, "error": classify_error(str(exc))}
    except Exception:
        return {"ok": False, "error": "download_failed"}


def serve():
    # Keep imports warm, not media or extracted video metadata. Every request
    # creates/closes its own YoutubeDL and private cookie jar.
    with YoutubeDL({"quiet": True, "no_warnings": True, "cachedir": False, "logger": QuietLogger()}) as warm:
        for name, sample in (("TikTok", "https://www.tiktok.com/@_/video/1"),
                             ("TikTokVM", "https://vm.tiktok.com/example"),
                             ("Youtube", "https://www.youtube.com/watch?v=BaW_jenozKc"),
                             ("Instagram", "https://www.instagram.com/reel/example/")):
            warm.get_info_extractor(name).suitable(sample)
    print(json.dumps({"ready": True}), flush=True)
    for line in sys.stdin:
        parameters = json.loads(line)
        result = execute(parameters)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        del parameters, result


def main():
    if sys.argv[1:] == ["--serve"]:
        serve()
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--max-bytes", type=int, default=2_000_000_000)
    parser.add_argument("--max-duration", type=int, default=0)
    parser.add_argument("--node", default="/usr/bin/node")
    parser.add_argument("--cookies-file")
    parser.add_argument("--format-id")
    parser.add_argument("--inspect-only", action="store_true")
    args = parser.parse_args()
    result = execute(vars(args))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
