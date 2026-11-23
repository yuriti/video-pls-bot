"""Daily Moscow-time download digest, independent of the polling process."""

import argparse
import asyncio
import fcntl
import logging
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

from .alerts import BOT_NAMES
from .texts import plural, t
from .config import Settings
from .telegram import Bot

log = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")
DAY = 86400


def due_end(now: float) -> int:
    local = datetime.fromtimestamp(now, MOSCOW)
    end = local.replace(hour=20, minute=0, second=0, microsecond=0)
    if local < end:
        end -= timedelta(days=1)
    return int(end.timestamp())


def enable_delivery_tracking(path: Path):
    # Do not construct Analytics: its startup recovery interrupts live jobs.
    # A DB trigger also works with an already-running, older polling process.
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, timeout=5)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            columns = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
            if "video_sent_at" not in columns:
                db.execute("ALTER TABLE requests ADD COLUMN video_sent_at INTEGER")
                # Historical deliveries have no wall-clock delivery timestamp.
                db.execute("UPDATE requests SET video_sent_at=created WHERE video_sent=1")
            db.execute("""CREATE TRIGGER IF NOT EXISTS requests_video_delivered
                AFTER UPDATE OF video_sent ON requests
                WHEN NEW.video_sent=1 AND OLD.video_sent=0
                BEGIN
                    UPDATE requests SET video_sent_at=CAST(strftime('%s','now') AS INTEGER)
                    WHERE job_id=NEW.job_id;
                END""")
            db.execute("CREATE INDEX IF NOT EXISTS requests_video_sent_at ON requests(video_sent_at)")
            db.commit()
        except BaseException:
            db.rollback()
            raise


def downloads(path: Path, end: int) -> dict[int, int]:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)) as db:
        return dict(db.execute("""SELECT bot_id, COUNT(*) FROM requests
            WHERE video_sent=1 AND video_sent_at>=? AND video_sent_at<?
            GROUP BY bot_id ORDER BY bot_id""", (end - DAY, end)))


def render_digest(end: int, counts: dict[int, int], names: dict[int, str]) -> str | None:
    lines = []
    for bot_id, count in counts.items():
        if count <= 0:
            continue
        name = names.get(bot_id) or BOT_NAMES.get(bot_id)
        label = f"@{name}" if name else t("digest.bot_fallback", bot_id=bot_id)
        lines.append(f"{label} — {plural('digest.videos', count)}")
    if not lines:
        return None
    start = datetime.fromtimestamp(end - DAY, MOSCOW).strftime("%d.%m.%Y %H:%M")
    finish = datetime.fromtimestamp(end, MOSCOW).strftime("%d.%m.%Y %H:%M")
    return f"{t('digest.title')}\n{start} — {finish} {t('timezone_label')}\n\n" + "\n".join(lines)


class ReportStore:
    def __init__(self, path: Path, baseline: int):
        self.db = sqlite3.connect(path, timeout=5)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cursor (id INTEGER PRIMARY KEY CHECK(id=1), period_end INTEGER);
            CREATE TABLE IF NOT EXISTS reports (
                period_end INTEGER PRIMARY KEY, message TEXT, sent INTEGER NOT NULL DEFAULT 0);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO cursor VALUES (1,?)", (baseline,))

    def last_end(self) -> int:
        return self.db.execute("SELECT period_end FROM cursor WHERE id=1").fetchone()[0]

    def message(self, end: int):
        return self.db.execute("SELECT message FROM reports WHERE period_end=?", (end,)).fetchone()

    def save(self, end: int, message: str | None):
        with self.db:
            self.db.execute("INSERT INTO reports(period_end,message) VALUES (?,?)", (end, message))

    def complete(self, end: int):
        with self.db:
            self.db.execute("UPDATE reports SET sent=1 WHERE period_end=?", (end,))
            self.db.execute("UPDATE cursor SET period_end=? WHERE id=1", (end,))

    def close(self):
        self.db.close()


def initialize(settings: Settings, now: float | None = None):
    enable_delivery_tracking(settings.data_dir / "analytics.sqlite3")
    with closing(ReportStore(settings.data_dir / "daily-reports.sqlite3", due_end(time.time() if now is None else now))):
        pass  # First deployment starts with the upcoming 20:00 digest.


async def send_due(settings: Settings, bots: dict[int, Bot], now: float | None = None) -> int:
    latest = due_end(time.time() if now is None else now)
    with (settings.data_dir / "daily-reports.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        analytics_path = settings.data_dir / "analytics.sqlite3"
        enable_delivery_tracking(analytics_path)
        with closing(ReportStore(settings.data_dir / "daily-reports.sqlite3", latest - DAY)) as store:
            delivered = 0
            end = store.last_end() + DAY
            while end <= latest:
                row = store.message(end)
                if row is None:
                    counts = downloads(analytics_path, end)
                    names = {}
                    for bot_id in counts:
                        if bot_id in bots:
                            me = await bots[bot_id].call("getMe", timeout=5)
                            names[bot_id] = me["username"]
                    message = render_digest(end, counts, names)
                    store.save(end, message)
                else:
                    message = row[0]  # Retry the same frozen digest after delivery errors.
                if message:
                    async with asyncio.timeout(10):
                        await bots[settings.alert_bot_id].text(settings.alert_chat_id, message)
                    delivered += 1
                store.complete(end)
                log.info("daily_digest period_end=%s sent=%s", end, bool(message))
                end += DAY
                if message and end <= latest:
                    await asyncio.sleep(1)
            return delivered


async def run(settings: Settings):
    async with aiohttp.ClientSession(trust_env=False) as session:
        bots = {int(t.split(":")[0]): Bot(t, settings.api_base, session) for t in settings.tokens}
        await send_due(settings, bots)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--initialize", action="store_true", help="Enable timestamps and initialize without sending")
    parser.add_argument("--preview", action="store_true", help="Print the current reporting period without sending")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.CRITICAL)
    os.umask(0o077)
    settings = Settings.load()
    try:
        if args.initialize:
            initialize(settings)
        elif args.preview:
            end = due_end(time.time()) + DAY
            print(render_digest(end, downloads(settings.data_dir / "analytics.sqlite3", end), {}) or "No downloads")
        else:
            asyncio.run(run(settings))
    except Exception:
        # Never expose request URLs, tokens or raw exceptions in journald.
        log.error("daily_digest_failed")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
