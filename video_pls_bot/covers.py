"""Small, temporary JPEGs attached to the video itself, not a separate message."""

import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiohttp

from .links import parse_link
from .process import ProcessError, run_process

log = logging.getLogger(__name__)
MAX_SOURCE_BYTES = 2_000_000


async def fetch_youtube_cover(link, path: Path, proxy: str) -> bool:
    # Construct a fixed public image URL; never fetch arbitrary extractor URLs
    # or forward cookies. The bot service can reach the Internet only via DOWNLOAD_PROXY.
    canonical = parse_link(link.url)
    if canonical.platform != "youtube":
        return False
    parsed = urlsplit(canonical.url)
    video_id = (parse_qs(parsed.query)["v"][0] if parsed.path == "/watch"
                else parsed.path.rstrip("/").rsplit("/", 1)[-1])
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as session:
        for name in ("maxresdefault", "hqdefault"):
            try:
                async with session.get(f"https://i.ytimg.com/vi/{video_id}/{name}.jpg",
                                       proxy=proxy, allow_redirects=False) as response:
                    if response.status != 200 or (response.content_length or 0) > MAX_SOURCE_BYTES:
                        continue
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data) > MAX_SOURCE_BYTES:
                            break
                    else:
                        if data:
                            path.write_bytes(data)
                            return True
            except (aiohttp.ClientError, TimeoutError):
                continue
    return False


async def prepare_cover(video_path: Path, duration: float, link, settings) -> tuple[Path, Path] | None:
    cover = video_path.with_name("cover.jpg")
    thumbnail = video_path.with_name("thumbnail.jpg")
    source = video_path.with_name("cover-source.jpg")

    async def render(input_path, output_path, size, *, seek=None, reject_black=False):
        output_path.unlink(missing_ok=True)
        filters = []
        if reject_black:
            filters += ["blackframe=amount=0:threshold=24",
                        "metadata=select:key=lavfi.blackframe.pblack:value=98:function=less"]
        filters += [f"scale=w='min({size},iw)':h='min({size},ih)':force_original_aspect_ratio=decrease",
                    "setsar=1"]
        try:
            await run_process(
                settings.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                "-threads", "1", "-protocol_whitelist", "file,pipe",
                *(["-ss", f"{seek:.3f}"] if seek is not None else []), "-i", str(input_path),
                "-t", "0.2", "-map", "0:v:0", "-an", "-vf", ",".join(filters),
                "-frames:v", "1", "-c:v", "mjpeg", "-q:v", "5", "-threads", "1",
                "-y", str(output_path), timeout=12, workspace=video_path.parent,
                max_disk_bytes=settings.workspace_bytes,
            )
            return output_path.is_file() and 0 < output_path.stat().st_size < (
                200_000 if size == 320 else MAX_SOURCE_BYTES)
        except ProcessError as exc:
            if str(exc) == "size_limit":
                raise
            return False
        except (TimeoutError, OSError):
            return False

    try:
        ready = False
        if link.platform == "youtube":
            try:
                fetched = await fetch_youtube_cover(link, source, settings.proxy)
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
                fetched = False
            if fetched:
                ready = await render(source, cover, 1280, reject_black=True)
        if not ready:
            # Skip a black intro; inspect a few bounded windows rather than
            # decoding an entire long video or changing its contents.
            for seek in (min(5, duration * 0.1), duration * 0.5, duration * 0.9):
                if await render(video_path, cover, 1280, seek=seek, reject_black=True):
                    ready = True
                    break
        if ready and await render(cover, thumbnail, 320):
            return cover, thumbnail
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
        pass
    finally:
        source.unlink(missing_ok=True)
    # Optional presentation must not prevent delivery of a valid video.
    cover.unlink(missing_ok=True)
    thumbnail.unlink(missing_ok=True)
    log.warning("video_cover_unavailable platform=%s", link.platform)
    return None
