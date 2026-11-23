import json
import os
import re
import grp
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def required_int(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return int(value)


@dataclass(frozen=True)
class Settings:
    tokens: tuple[str, ...]
    data_dir: Path
    temp_dir: Path
    # Loopback HTTP proxy for every outbound download and the local Telegram Bot API.
    # Both are required in production (DOWNLOAD_PROXY / TELEGRAM_API_BASE); there is no default.
    proxy: str = ""
    api_base: str = ""
    concurrency: int = 2
    max_waiting: int = 20
    timeout: int = 3600
    max_duration: int = 0  # No duration cap: bounded by bytes and processing time.
    max_bytes: int = 2_000_000_000
    temp_budget_bytes: int = 10_000_000_000
    node: str = "/usr/bin/node"
    ffmpeg: str = "/usr/bin/ffmpeg"
    ffprobe: str = "/usr/bin/ffprobe"
    youtube_cookies: Path | None = None
    # Telegram chat that receives alerts and the daily digest, and the bot that sends them.
    alert_chat_id: int | None = None
    alert_bot_id: int | None = None
    media_gid: int | None = None
    tiktok_cookies: Path | None = None
    # Optional shared download queue (CORE_SOCKET); None: the bot keeps only its own limit.
    core_socket: str | None = None

    def __post_init__(self):
        if not 0 < self.max_bytes <= 2_000_000_000:
            raise ValueError("MAX_BYTES must fit the local Telegram upload limit")
        if self.temp_budget_bytes < self.workspace_bytes + 1_000_000_000:
            raise ValueError("TEMP_BUDGET_BYTES must include workspace and free-space reserve")

    @property
    def workspace_bytes(self):
        # Separate streams + merged MP4, or MP4 + MP3, plus small overhead.
        return self.max_bytes * 2 + 64_000_000

    @property
    def disk_concurrency(self):
        return min(self.concurrency, (self.temp_budget_bytes - 1_000_000_000) // self.workspace_bytes)

    def cookies_for(self, platform: str) -> Path | None:
        return {"youtube": self.youtube_cookies, "tiktok": self.tiktok_cookies}.get(platform)

    @classmethod
    def load(cls):
        inline = os.environ.get("BOT_TOKENS", "").strip()
        if inline:
            tokens = [t.strip() for t in inline.split(",") if t.strip()]
        else:
            credential = os.environ.get("BOT_TOKENS_FILE")
            if not credential:
                credential = str(Path(os.environ["CREDENTIALS_DIRECTORY"]) / "bot-tokens.json")
            tokens = json.loads(Path(credential).read_text())
        if not isinstance(tokens, list) or not tokens or any(
            not isinstance(t, str) or not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", t) for t in tokens
        ) or len(tokens) != len(set(t.split(":")[0] for t in tokens)):
            raise ValueError("Invalid or duplicate bot credentials")
        proxy = os.environ.get("DOWNLOAD_PROXY", "").strip()
        api = os.environ.get("TELEGRAM_API_BASE", "").strip().rstrip("/")
        if not proxy or not api:
            raise ValueError("DOWNLOAD_PROXY and TELEGRAM_API_BASE are required")
        # Media URLs/credentials must only be sent to our loopback services.
        for url in (proxy, api):
            parsed = urlsplit(url)
            if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
                raise ValueError("A loopback HTTP endpoint is required")
        cookies = os.environ.get("YOUTUBE_COOKIES_FILE")
        if not cookies and os.environ.get("CREDENTIALS_DIRECTORY"):
            candidate = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "youtube-cookies.txt"
            if candidate.is_file():
                cookies = str(candidate)
        tiktok_cookies = os.environ.get("TIKTOK_COOKIES_FILE")
        if not tiktok_cookies and os.environ.get("CREDENTIALS_DIRECTORY"):
            candidate = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "tiktok-cookies.txt"
            if candidate.is_file():
                tiktok_cookies = str(candidate)
        return cls(
            tokens=tuple(tokens),
            data_dir=Path(os.environ.get("DATA_DIR", "/var/lib/video-pls-bot")),
            temp_dir=Path(os.environ.get("TEMP_DIR", "/var/cache/video-pls-bot")),
            proxy=proxy, api_base=api,
            concurrency=int(os.environ.get("CONCURRENCY", "2")),
            max_waiting=int(os.environ.get("MAX_WAITING", "20")),
            timeout=int(os.environ.get("JOB_TIMEOUT", "3600")),
            max_duration=int(os.environ.get("MAX_DURATION", "0")),
            max_bytes=int(os.environ.get("MAX_BYTES", "2000000000")),
            temp_budget_bytes=int(os.environ.get("TEMP_BUDGET_BYTES", "10000000000")),
            node=os.environ.get("NODE_BINARY", "/usr/bin/node"),
            ffmpeg=os.environ.get("FFMPEG_BINARY", "/usr/bin/ffmpeg"),
            ffprobe=os.environ.get("FFPROBE_BINARY", "/usr/bin/ffprobe"),
            youtube_cookies=Path(cookies) if cookies else None,
            alert_chat_id=required_int("ALERT_CHAT_ID"),
            alert_bot_id=required_int("ALERT_BOT_ID"),
            media_gid=grp.getgrnam("telegram-bot-api").gr_gid if os.environ.get("TELEGRAM_LOCAL_FILES") == "1" else None,
            tiktok_cookies=Path(tiktok_cookies) if tiktok_cookies else None,
            core_socket=os.environ.get("CORE_SOCKET", "").strip() or None,
        )
