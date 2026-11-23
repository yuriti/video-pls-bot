"""Reproducible uncached benchmarks; delivery is explicit and admin-only."""

import argparse
import asyncio
import dataclasses
import json
import shutil
import tempfile
import time
from pathlib import Path

import aiohttp

from .analytics import Analytics
from .app import Application
from .config import Settings
from .extractor_pool import ExtractorPool
from .links import parse_link
from .media import download_video, make_mp3
from .telegram import Bot


async def benchmark(args):
    settings = dataclasses.replace(Settings.load(), concurrency=1)
    link = parse_link(args.url)
    pool = None if args.cold else ExtractorPool(settings)
    if pool:
        await pool.start()
    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            for index in range(args.runs):
                directory = Path(tempfile.mkdtemp(prefix="benchmark-", dir=settings.temp_dir))
                try:
                    if args.deliver:
                        store = Analytics(directory / "analytics.sqlite3")
                        app = Application(settings, store)
                        app.extractors = pool
                        token = next(t for t in settings.tokens if int(t.split(":")[0]) == settings.alert_bot_id)
                        bot = Bot(token, settings.api_base, session, media_gid=settings.media_gid)
                        bot.username = (await bot.call("getMe"))["username"]
                        try:
                            await app.handle(bot, {"message": {"from": {"id": settings.alert_chat_id},
                                             "chat": {"id": settings.alert_chat_id, "type": "private"}, "text": link.url}})
                            await app.queue.close()
                            row = store.db.execute("SELECT status,elapsed,metrics,video_bytes FROM requests").fetchone()
                            result = {"status": row[0], "total_s": row[1], **json.loads(row[2]), "bytes": row[3]}
                        finally:
                            store.close()
                    else:
                        start = time.perf_counter()
                        video = await download_video(link, directory, settings, extractor=pool)
                        ready = time.perf_counter() - start
                        if video.has_audio:
                            await make_mp3(video, settings)
                        result = {"video_ready_s": round(ready, 4), "total_s": round(time.perf_counter() - start, 4),
                                  "bytes": video.path.stat().st_size, "duration": video.duration, **video.timings}
                    print(json.dumps({"run": index + 1, "platform": link.platform, "warm": pool is not None,
                                      "delivery": args.deliver, **result}), flush=True)
                finally:
                    shutil.rmtree(directory, ignore_errors=True)
    finally:
        if pool:
            await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--cold", action="store_true")
    parser.add_argument("--deliver", action="store_true", help="Send video+MP3 to ALERT_CHAT_ID via the ALERT_BOT_ID bot")
    args = parser.parse_args()
    if not 1 <= args.runs <= 10:
        parser.error("runs must be between 1 and 10")
    asyncio.run(benchmark(args))
