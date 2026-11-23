import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Settings
from .covers import prepare_cover
from .links import VideoLink
from .process import run_process


class MediaError(Exception):
    pass


@dataclass(frozen=True)
class Video:
    path: Path
    duration: float
    width: int
    height: int
    has_audio: bool
    title: str
    timings: dict = field(default_factory=dict)
    cover: Path | None = None
    thumbnail: Path | None = None


@dataclass(frozen=True)
class Selection:
    title: str
    duration: float
    thumbnail: str | None
    choices: list[dict]
    metadata_handle: str | None = None


async def download_video(link: VideoLink, directory: Path, settings: Settings, *, extractor=None,
                         format_id=None, progress=None, metadata_handle=None) -> Video | Selection:
    started = time.perf_counter()
    selected_cookies = settings.cookies_for(link.platform)
    cookies = ["--cookies-file", str(selected_cookies)] if selected_cookies else []
    if extractor:
        result = await extractor.download(link, directory, format_id=format_id, progress=progress,
                                          **({'metadata_handle': metadata_handle} if metadata_handle else {}))
    else:
        output = await run_process(
            sys.executable, "-m", "video_pls_bot.extract", link.url, str(directory),
            "--proxy", settings.proxy, "--max-bytes", str(settings.max_bytes),
            "--max-duration", str(settings.max_duration), "--node", settings.node,
            *cookies, *(["--format-id", format_id] if format_id else []),
            timeout=settings.timeout - 30, workspace=directory, max_disk_bytes=settings.workspace_bytes,
        )
        result = json.loads(output)
    if not result.get("ok"):
        raise MediaError(result.get("error", "download_failed"))
    if result.get("selection"):
        return Selection(result["title"], result["duration"], result.get("thumbnail"), result["choices"],
                         result.get('metadata_handle'))
    video = directory / "video.mp4"
    downloaded = time.perf_counter()
    details = json.loads(await run_process(
        settings.ffprobe, "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,width,height", "-of", "json", str(video), timeout=10,
    ))
    duration = float(details.get("format", {}).get("duration", 0))
    if duration <= 0 or (settings.max_duration and duration > settings.max_duration + 0.5):
        raise MediaError("duration_limit")
    streams = details.get("streams", [])
    image = next((s for s in streams if s.get("codec_type") == "video"), None)
    if image is None:
        raise MediaError("no_video")
    if video.stat().st_size > settings.max_bytes:
        raise MediaError("size_limit")
    probed = time.perf_counter()
    images = await prepare_cover(video, duration, link, settings)
    return Video(video, duration, image.get("width", 0), image.get("height", 0),
                 any(s.get("codec_type") == "audio" for s in streams), result["title"],
                 {**result.get("timings", {}), "extract_download_s": round(downloaded - started, 4),
                  "probe_s": round(probed - downloaded, 4),
                  "cover_s": round(time.perf_counter() - probed, 4), "cover_ready": bool(images)},
                 *(images or (None, None)))


async def make_mp3(video: Video, settings: Settings) -> Path:
    audio = video.path.with_name("audio.mp3")
    await run_process(settings.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                      "-i", str(video.path), "-vn", "-map", "0:a:0", "-c:a", "libmp3lame",
                      "-b:a", "192k", "-threads", "1", "-fs", str(settings.max_bytes + 1), "-y", str(audio),
                      timeout=min(settings.timeout - 30, max(60, video.duration)), workspace=video.path.parent,
                      max_disk_bytes=settings.workspace_bytes)
    # -fs can stop FFmpeg with a successful return code and a truncated MP3.
    if audio.stat().st_size >= settings.max_bytes:
        raise MediaError("size_limit")
    return audio
