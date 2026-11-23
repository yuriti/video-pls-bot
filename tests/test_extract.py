import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from video_pls_bot.extract import MediaRejected, classify_error, download
from video_pls_bot.config import Settings


class ExtractTests(unittest.TestCase):
    def test_tiktok_classified_post_reports_login_instead_of_missing_video(self):
        error = "[TikTok] 7702437387283991828: This post may not be comfortable for some audiences. Log in for access."
        self.assertEqual(classify_error(error), "source_login_required")
        self.assertEqual(classify_error("HTTP Error 403: Forbidden"), "source_unavailable")

    def test_platform_cookie_jars_are_not_shared_between_sites(self):
        settings = Settings((), Path("/tmp"), Path("/tmp"), youtube_cookies=Path("youtube.txt"),
                            tiktok_cookies=Path("tiktok.txt"))
        self.assertEqual(settings.cookies_for("youtube"), Path("youtube.txt"))
        self.assertEqual(settings.cookies_for("tiktok"), Path("tiktok.txt"))
        self.assertIsNone(settings.cookies_for("instagram"))

    def test_instagram_without_duration_downloads_once_for_later_probe(self):
        with tempfile.TemporaryDirectory() as root, patch("video_pls_bot.extract.YoutubeDL") as constructor:
            directory = Path(root)
            worker = MagicMock()
            constructor.return_value.__enter__.return_value = worker
            worker.extract_info.return_value = {"duration": None, "formats": []}

            def process(info, download):
                (directory / "video.mp4").write_bytes(b"video")
                return {"title": "Test"}

            worker.process_ie_result.side_effect = process
            result = download("https://www.instagram.com/reel/example/", directory,
                              "http://127.0.0.1:3128", 1000, 180, "/usr/bin/node")
            self.assertTrue(result["ok"])
            worker.extract_info.assert_called_once()
            worker.process_ie_result.assert_called_once()

    def test_long_recording_is_rejected_before_download(self):
        with tempfile.TemporaryDirectory() as root, patch("video_pls_bot.extract.YoutubeDL") as constructor:
            worker = MagicMock()
            constructor.return_value.__enter__.return_value = worker
            worker.extract_info.return_value = {"duration": 181}
            with self.assertRaisesRegex(MediaRejected, "duration_limit"):
                download("https://youtu.be/BaW_jenozKc", Path(root), "http://127.0.0.1:3128", 1000, 180, "/usr/bin/node")
            worker.process_ie_result.assert_not_called()

    def test_download_budget_is_shared_by_audio_and_video(self):
        with tempfile.TemporaryDirectory() as root, patch("video_pls_bot.extract.YoutubeDL") as constructor:
            worker = MagicMock()
            constructor.return_value.__enter__.return_value = worker
            worker.extract_info.return_value = {"duration": 10}

            def process(info, download):
                hook = constructor.call_args.args[0]["progress_hooks"][0]
                hook({"filename": "video", "downloaded_bytes": 700})
                hook({"filename": "audio", "downloaded_bytes": 400})

            worker.process_ie_result.side_effect = process
            with self.assertRaisesRegex(MediaRejected, "size_limit"):
                download("https://youtu.be/BaW_jenozKc", Path(root), "http://127.0.0.1:3128", 1000, 180, "/usr/bin/node")
