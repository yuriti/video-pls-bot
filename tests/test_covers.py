import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

from video_pls_bot.config import Settings
from video_pls_bot.covers import MAX_SOURCE_BYTES, fetch_youtube_cover, prepare_cover
from video_pls_bot.links import parse_link
from video_pls_bot.media import download_video
from video_pls_bot.process import ProcessError, run_process


class FetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_youtube_cover_is_bounded_and_uses_proxy_without_cookies(self):
        with tempfile.TemporaryDirectory() as root, patch("video_pls_bot.covers.aiohttp.ClientSession") as cls:
            session = cls.return_value.__aenter__.return_value
            missing, found = MagicMock(), MagicMock()
            missing.status = 404
            found.status, found.content_length = 200, None
            async def chunks(size):
                yield b"jpeg"
            found.content.iter_chunked = chunks
            contexts = []
            for response in (missing, found):
                context = MagicMock()
                context.__aenter__ = AsyncMock(return_value=response)
                contexts.append(context)
            session.get = MagicMock(side_effect=contexts)
            path = Path(root) / "source.jpg"
            self.assertTrue(await fetch_youtube_cover(parse_link("https://youtu.be/BaW_jenozKc"), path, "http://proxy"))
            self.assertEqual(path.read_bytes(), b"jpeg")
            self.assertEqual(session.get.call_args.args, ("https://i.ytimg.com/vi/BaW_jenozKc/hqdefault.jpg",))
            self.assertEqual(session.get.call_args.kwargs, {"proxy": "http://proxy", "allow_redirects": False})

    async def test_oversized_chunked_response_is_not_written(self):
        with tempfile.TemporaryDirectory() as root, patch("video_pls_bot.covers.aiohttp.ClientSession") as cls:
            session = cls.return_value.__aenter__.return_value
            response = MagicMock(status=200, content_length=None)
            async def chunks(size):
                yield b"x" * (MAX_SOURCE_BYTES + 1)
            response.content.iter_chunked = chunks
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=response)
            session.get = MagicMock(return_value=context)
            path = Path(root) / "source.jpg"
            self.assertFalse(await fetch_youtube_cover(parse_link("https://youtu.be/BaW_jenozKc"), path, "http://proxy"))
            self.assertFalse(path.exists())


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class CoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = Settings((), self.root, self.root, ffmpeg=shutil.which("ffmpeg"),
                                 ffprobe=shutil.which("ffprobe"))
        self.video = self.root / "video.mp4"
        await run_process(self.settings.ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                          "color=c=black:s=640x360:r=10:d=1", "-f", "lavfi", "-i",
                          "color=c=blue:s=640x360:r=10:d=2", "-filter_complex",
                          "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-c:v", "libx264",
                          "-threads", "1", str(self.video))

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def assert_images(self, images, expected_rgb):
        self.assertIsNotNone(images)
        for path, limit in zip(images, (1280, 320)):
            info = json.loads(await run_process(self.settings.ffprobe, "-v", "error", "-show_entries",
                                               "stream=codec_name,width,height", "-of", "json", str(path)))
            stream = info["streams"][0]
            self.assertEqual(stream["codec_name"], "mjpeg")
            self.assertLessEqual(max(stream["width"], stream["height"]), limit)
            if limit == 320:
                self.assertLess(path.stat().st_size, 200_000)
            pixel = await run_process(self.settings.ffmpeg, "-v", "error", "-i", str(path),
                                      "-vf", "scale=1:1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-")
            for actual, expected in zip(pixel, expected_rgb):
                self.assertAlmostEqual(actual, expected, delta=15)
        self.assertFalse((self.root / "cover-source.jpg").exists())

    async def test_frame_fallback_skips_black_intro_for_all_platforms(self):
        for url in ("https://youtu.be/BaW_jenozKc", "https://www.tiktok.com/@user/video/12345",
                    "https://www.instagram.com/reel/example/"):
            with self.subTest(url=url), patch("video_pls_bot.covers.fetch_youtube_cover", AsyncMock(return_value=False)):
                images = await prepare_cover(self.video, 3, parse_link(url), self.settings)
                await self.assert_images(images, (0, 0, 255))

    async def test_youtube_image_is_used_instead_of_video_frame(self):
        async def fetch(link, path, proxy):
            await run_process(self.settings.ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                              "color=c=red:s=1280x720", "-frames:v", "1", "-threads", "1", str(path))
            return True
        with patch("video_pls_bot.covers.fetch_youtube_cover", fetch):
            images = await prepare_cover(self.video, 3, parse_link("https://youtu.be/BaW_jenozKc"), self.settings)
        await self.assert_images(images, (255, 0, 0))

    async def test_bad_source_image_falls_back_to_a_video_frame(self):
        async def fetch(link, path, proxy):
            path.write_bytes(b"not an image")
            return True
        with patch("video_pls_bot.covers.fetch_youtube_cover", fetch):
            images = await prepare_cover(self.video, 3, parse_link("https://youtu.be/BaW_jenozKc"), self.settings)
        await self.assert_images(images, (0, 0, 255))

    async def test_source_network_failure_falls_back_to_a_video_frame(self):
        with patch("video_pls_bot.covers.fetch_youtube_cover", AsyncMock(side_effect=aiohttp.ClientError())):
            images = await prepare_cover(self.video, 3, parse_link("https://youtu.be/BaW_jenozKc"), self.settings)
        await self.assert_images(images, (0, 0, 255))

    async def test_all_black_video_does_not_produce_a_black_cover(self):
        black = self.root / "black.mp4"
        await run_process(self.settings.ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                          "color=c=black:s=64x64:r=10:d=1", "-c:v", "libx264", "-threads", "1", str(black))
        images = await prepare_cover(black, 1, parse_link("https://www.instagram.com/reel/example/"), self.settings)
        self.assertIsNone(images)
        self.assertFalse((self.root / "cover.jpg").exists())

    async def test_download_returns_cover_paths_and_keeps_video_unchanged(self):
        original = self.video.read_bytes()
        extractor = MagicMock()
        extractor.download = AsyncMock(return_value={"ok": True, "title": "Test"})
        video = await download_video(parse_link("https://www.instagram.com/reel/example/"), self.root,
                                     self.settings, extractor=extractor)
        await self.assert_images((video.cover, video.thumbnail), (0, 0, 255))
        self.assertEqual(original, self.video.read_bytes())
        self.assertTrue(video.timings["cover_ready"])

    async def test_cover_failure_is_optional_but_disk_limits_and_cancellation_propagate(self):
        link = parse_link("https://www.instagram.com/reel/example/")
        with patch("video_pls_bot.covers.run_process", AsyncMock(side_effect=ProcessError("process_failed"))):
            self.assertIsNone(await prepare_cover(self.video, 3, link, self.settings))
        for error in (ProcessError("size_limit"), asyncio.CancelledError()):
            with self.subTest(error=type(error)), patch("video_pls_bot.covers.run_process", AsyncMock(side_effect=error)):
                with self.assertRaises(type(error)):
                    await prepare_cover(self.video, 3, link, self.settings)

