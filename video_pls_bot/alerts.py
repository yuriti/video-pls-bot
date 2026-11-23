"""Durable, grouped alerts with validated source links, never raw exceptions or message text."""

import argparse
import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

from .config import Settings
from .links import parse_link
from .telegram import Bot, TelegramError
from .texts import prefixed, t

log = logging.getLogger(__name__)


def parse_bot_names(value):
    """BOT_USERNAMES="123:first_bot,456:second_bot": fallback names when getMe is unavailable."""
    names = {}
    for item in (value or "").split(","):
        bot_id, _, name = item.strip().partition(":")
        if bot_id.isdigit() and re.fullmatch(r"[A-Za-z0-9_]{1,64}", name):
            names[int(bot_id)] = name
    return names


BOT_NAMES = parse_bot_names(os.environ.get("BOT_USERNAMES"))
STAGES = prefixed("stage")
REASONS = prefixed("reason")


def safe_code(value: str) -> str:
    if value in REASONS or re.fullmatch(r"telegram_\d{3}", value or ""):
        return value
    return "internal"


def safe_source_url(value: str) -> str:
    # Accept only a source link supported by the bot. parse_link removes tracking
    # parameters and fragments; downloader/API URLs and credentials are rejected.
    if not re.fullmatch(r"https?://[^\s<>]+", value or ""):
        return ""
    try:
        return parse_link(value).url
    except ValueError:
        return ""


def render_alert(row) -> str:
    recovered = row["code"] == "recovered"
    title = t("alert.title_recovered") if recovered else (
        t("alert.title_test") if row["stage"] == "test" else t("alert.title_problem"))
    username = row["username"] or BOT_NAMES.get(row["bot_id"], "")
    who = f"@{username} ({row['bot_id']})" if username else (str(row["bot_id"]) if row["bot_id"] else t("alert.all_bots"))
    code = row["code"]
    reason = REASONS.get(code, t("alert.default_reason"))
    when = datetime.fromtimestamp(row["last_seen"], ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M:%S")
    lines = [title, f"{t('alert.bot')}: {who}", f"{t('alert.stage')}: {STAGES.get(row['stage'], t('alert.default_stage'))}",
             f"{t('alert.reason')}: {reason} ({code})"]
    if row.get("platform"):
        lines.append(f"{t('alert.platform')}: {row['platform']}")
    if row.get("job_id"):
        lines.append(f"{t('alert.job')}: {row['job_id']}")
    if row.get("user_id"):
        lines.append(f"{t('alert.user')}: {row['user_id']}")
    if row.get("source_url"):
        label = t("alert.video_last_failure") if row["pending"] > 1 else t("alert.video")
        lines.append(f"{label}: {row['source_url']}")
    lines.extend([f"{t('alert.events')}: {row['pending']}", f"{t('alert.time')}: {when} {t('timezone_label')}"])
    return "\n".join(lines)


class AlertStore:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS alerts (
                key TEXT PRIMARY KEY, bot_id INTEGER, username TEXT, stage TEXT, code TEXT,
                platform TEXT, job_id TEXT, user_id INTEGER, first_seen REAL, last_seen REAL,
                pending INTEGER, next_send REAL DEFAULT 0, lease_until REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS checks (key TEXT PRIMARY KEY, okay INTEGER);
            CREATE TABLE IF NOT EXISTS heartbeats (bot_id INTEGER PRIMARY KEY, username TEXT, last_ok REAL);
            CREATE TABLE IF NOT EXISTS restarts (unit TEXT PRIMARY KEY, count INTEGER);
        """)
        # Serialize migration with the independent health service opening the
        # same database. Existing pending alerts remain deliverable.
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in self.db.execute("PRAGMA table_info(alerts)")}
            if "source_url" not in columns:
                self.db.execute("ALTER TABLE alerts ADD COLUMN source_url TEXT NOT NULL DEFAULT ''")

    def record(self, row):
        key = f"{row['bot_id']}:{row['stage']}:{row['code']}:{row['platform']}"
        with self.db:
            self.db.execute("""INSERT INTO alerts
                (key,bot_id,username,stage,code,platform,job_id,user_id,first_seen,last_seen,source_url,pending)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
                ON CONFLICT(key) DO UPDATE SET pending=pending+1,last_seen=excluded.last_seen,
                username=excluded.username,job_id=excluded.job_id,user_id=excluded.user_id,
                source_url=excluded.source_url""",
                (key, row["bot_id"], row["username"], row["stage"], row["code"], row["platform"],
                 row["job_id"], row["user_id"], row["last_seen"], row["last_seen"], row.get("source_url", "")))

    def claim(self, now):
        # The app and the independent health service share this outbox.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT * FROM alerts WHERE pending>0 AND next_send<=? AND lease_until<=? ORDER BY last_seen LIMIT 1", (now, now)).fetchone()
            if row:
                self.db.execute("UPDATE alerts SET lease_until=? WHERE key=?", (now + 30, row["key"]))
            self.db.commit()
            return dict(row) if row else None
        except BaseException:
            self.db.rollback()
            raise

    def sent(self, row, now, window):
        with self.db:
            self.db.execute("UPDATE alerts SET pending=MAX(0,pending-?),next_send=?,lease_until=0 WHERE key=?",
                            (row["pending"], now + window, row["key"]))

    def retry(self, row, now, delay):
        with self.db:
            self.db.execute("UPDATE alerts SET next_send=?,lease_until=0 WHERE key=?", (now + delay, row["key"]))

    def check(self, key, okay):
        row = self.db.execute("SELECT okay FROM checks WHERE key=?", (key,)).fetchone()
        with self.db:
            self.db.execute("INSERT INTO checks VALUES (?,?) ON CONFLICT(key) DO UPDATE SET okay=excluded.okay", (key, int(okay)))
        return (row is None and not okay) or (row is not None and bool(row[0]) != okay)

    def heartbeat(self, bot_id, username):
        with self.db:
            self.db.execute("INSERT INTO heartbeats VALUES (?,?,?) ON CONFLICT(bot_id) DO UPDATE SET username=excluded.username,last_ok=excluded.last_ok",
                            (bot_id, username, time.time()))

    def close(self):
        self.db.close()

    def restarted(self, unit, count):
        row = self.db.execute("SELECT count FROM restarts WHERE unit=?", (unit,)).fetchone()
        with self.db:
            self.db.execute("INSERT INTO restarts VALUES (?,?) ON CONFLICT(unit) DO UPDATE SET count=excluded.count", (unit, count))
        return row is not None and count > row[0]


class Reporter:
    def __init__(self, settings: Settings, bot: Bot):
        self.settings = settings
        self.bot = bot
        self.fallback = {}
        self.window = 300
        try:
            self.store = AlertStore(settings.data_dir / "alerts.sqlite3")
        except (sqlite3.Error, OSError):
            self.store = None
            log.error("alert_storage_unavailable")
        self._flush_lock = asyncio.Lock()

    def emit(self, *, bot_id=0, username="", stage="service", code="internal", platform="", job_id="", user_id=0,
             source_url=""):
        row = {"bot_id": int(bot_id), "username": username if re.fullmatch(r"[A-Za-z0-9_]{1,64}", username or "") else BOT_NAMES.get(bot_id, ""),
               "stage": stage if stage in STAGES else "service", "code": safe_code(code),
               "platform": platform if platform in ("youtube", "tiktok", "instagram") else "",
               "job_id": job_id if re.fullmatch(r"[a-f0-9]{32}", job_id or "") else "",
               "user_id": int(user_id or 0), "source_url": safe_source_url(source_url),
               "last_seen": time.time(), "pending": 1}
        if self.store:
            try:
                self.store.record(row)
                return
            except sqlite3.Error:
                log.error("alert_storage_write_failed")
        key = (row["bot_id"], row["stage"], row["code"], row["platform"])
        if key in self.fallback:
            row["pending"] += self.fallback[key]["pending"]
        # Hard bound even if storage is full and delivery is unavailable.
        if len(self.fallback) >= 100 and key not in self.fallback:
            key = (0, "service", "alert_storage_failed", "")
            row.update(bot_id=0, username="", stage="service", code="alert_storage_failed", platform="")
            row["pending"] += self.fallback.get(key, {}).get("pending", 0)
        self.fallback[key] = row

    def heartbeat(self, bot):
        if self.store:
            try:
                self.store.heartbeat(bot.id, bot.username)
            except sqlite3.Error:
                self.emit(stage="analytics", code="alert_storage_failed")

    def check(self, key, okay, **context):
        if not self.store:
            if not okay:
                self.emit(**context)
            return
        try:
            changed = self.store.check(key, okay)
        except sqlite3.Error:
            self.emit(stage="analytics", code="alert_storage_failed")
            return
        if changed:
            self.emit(**{**context, "code": "recovered" if okay else context.get("code", "unavailable")})

    async def flush(self, limit=3):
        async with self._flush_lock:
            delivered = 0
            for _ in range(limit):
                fallback_key = None
                try:
                    row = self.store.claim(time.time()) if self.store else None
                except sqlite3.Error:
                    row = None
                if row is None and self.fallback:
                    fallback_key, row = next(iter(self.fallback.items()))
                if row is None:
                    break
                try:
                    async with asyncio.timeout(5):
                        await self.bot.text(self.settings.alert_chat_id, render_alert(row))
                except (TelegramError, TimeoutError) as exc:
                    code = exc.code if isinstance(exc, TelegramError) else "timeout"
                    delay = max(30, min(getattr(exc, "retry_after", 0), 3600))
                    log.warning("admin_alert_delivery_failed code=%s", code)
                    if fallback_key is None:
                        self.store.retry(row, time.time(), delay)
                    break  # No recursive notifications about the notification itself.
                if fallback_key is not None:
                    self.fallback.pop(fallback_key, None)
                else:
                    self.store.sent(row, time.time(), self.window)
                delivered += 1
                await asyncio.sleep(1)
            return delivered

    async def run(self):
        while True:
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.error("admin_alert_flush_failed")
            await asyncio.sleep(3)

    def close(self):
        if self.store:
            self.store.close()


async def test_delivery(settings):
    async with aiohttp.ClientSession(trust_env=False) as session:
        token = next(t for t in settings.tokens if int(t.split(":")[0]) == settings.alert_bot_id)
        reporter = Reporter(settings, Bot(token, settings.api_base, session))
        try:
            reporter.emit(stage="test", code="test")
            delivered = await reporter.flush()
            print(json.dumps({"delivered": delivered, "chat_id": settings.alert_chat_id, "via_bot_id": settings.alert_bot_id}))
        finally:
            reporter.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true", required=True)
    parser.parse_args()
    asyncio.run(test_delivery(Settings.load()))
