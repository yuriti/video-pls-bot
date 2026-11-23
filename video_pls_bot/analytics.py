import json
import sqlite3
import time
from pathlib import Path


class Analytics:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER, bot_id INTEGER, first_seen INTEGER, last_seen INTEGER,
                PRIMARY KEY(user_id, bot_id));
            CREATE TABLE IF NOT EXISTS requests (
                job_id TEXT PRIMARY KEY, user_id INTEGER, bot_id INTEGER, platform TEXT,
                created INTEGER, status TEXT, elapsed REAL DEFAULT 0,
                video_bytes INTEGER DEFAULT 0, video_sent INTEGER DEFAULT 0,
                audio_sent INTEGER DEFAULT 0, error_code TEXT);
            CREATE INDEX IF NOT EXISTS requests_created ON requests(created);
            CREATE TABLE IF NOT EXISTS counters (
                day TEXT, bot_id INTEGER, name TEXT, count INTEGER,
                PRIMARY KEY(day, bot_id, name));
            CREATE TABLE IF NOT EXISTS offsets (bot_id INTEGER PRIMARY KEY, offset INTEGER);
        """)
        if "metrics" not in {row[1] for row in self.db.execute("PRAGMA table_info(requests)")}:
            self.db.execute("ALTER TABLE requests ADD COLUMN metrics TEXT NOT NULL DEFAULT '{}'")
        # Accepted jobs are in memory and cannot safely be replayed after a crash.
        self.db.execute("UPDATE requests SET status='interrupted', error_code='restart' WHERE status IN ('queued','running','awaiting_choice')")
        self.db.commit()

    def seen(self, user_id: int, bot_id: int):
        now = int(time.time())
        with self.db:
            self.db.execute("""INSERT INTO users VALUES (?,?,?,?)
                ON CONFLICT(user_id,bot_id) DO UPDATE SET last_seen=excluded.last_seen""",
                (user_id, bot_id, now, now))

    def count(self, bot_id: int, name: str):
        with self.db:
            self.db.execute("""INSERT INTO counters VALUES (date('now'),?,?,1)
                ON CONFLICT(day,bot_id,name) DO UPDATE SET count=count+1""", (bot_id, name))

    def accepted(self, job_id: str, user_id: int | None, bot_id: int, platform: str):
        with self.db:
            self.db.execute("INSERT INTO requests(job_id,user_id,bot_id,platform,created,status) VALUES (?,?,?,?,?,'queued')",
                            (job_id, user_id, bot_id, platform, int(time.time())))

    def update(self, job_id: str, **fields):
        allowed = {"status", "elapsed", "video_bytes", "video_sent", "audio_sent", "error_code", "metrics"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid analytics fields")
        if "metrics" in fields:
            fields["metrics"] = json.dumps(fields["metrics"], separators=(",", ":"))
        with self.db:
            self.db.execute("UPDATE requests SET " + ",".join(f"{k}=?" for k in fields) + " WHERE job_id=?",
                            (*fields.values(), job_id))

    def offset(self, bot_id: int) -> int:
        row = self.db.execute("SELECT offset FROM offsets WHERE bot_id=?", (bot_id,)).fetchone()
        return row[0] if row else 0

    def set_offset(self, bot_id: int, offset: int):
        with self.db:
            self.db.execute("INSERT INTO offsets VALUES (?,?) ON CONFLICT(bot_id) DO UPDATE SET offset=excluded.offset",
                            (bot_id, offset))

    def summary(self, days: int = 30):
        self.db.row_factory = sqlite3.Row
        since = int(time.time()) - days * 86400
        def aggregate(bot_id=None):
            condition = "created>=?" + (" AND bot_id=?" if bot_id is not None else "")
            args = (since, bot_id) if bot_id is not None else (since,)
            row = self.db.execute(f"""SELECT COUNT(*) requests, COUNT(DISTINCT user_id) download_users,
                SUM(status='completed') completed, SUM(status='partial') partial,
                SUM(status IN ('failed','timed_out','interrupted')) failed,
                SUM(video_sent) videos_sent, SUM(audio_sent) mp3_sent,
                SUM(video_bytes) downloaded_bytes, AVG(elapsed) average_seconds,
                AVG(json_extract(metrics,'$.video_delivered_s')) average_video_seconds,
                AVG(json_extract(metrics,'$.video_upload_s')) average_video_upload_seconds
                FROM requests WHERE {condition}""", args).fetchone()
            seen_condition = "last_seen>=?" + (" AND bot_id=?" if bot_id is not None else "")
            result = dict(row)
            result["active_users"] = self.db.execute(
                f"SELECT COUNT(DISTINCT user_id) FROM users WHERE {seen_condition}", args).fetchone()[0]
            result["new_users"] = self.db.execute(
                "SELECT COUNT(*) FROM (SELECT user_id, MIN(first_seen) first_seen FROM users "
                + ("WHERE bot_id=? " if bot_id is not None else "")
                + "GROUP BY user_id) WHERE first_seen>=?",
                (bot_id, since) if bot_id is not None else (since,)).fetchone()[0]
            result["returning_users"] = result["active_users"] - result["new_users"]
            return result
        bots = [row[0] for row in self.db.execute("SELECT bot_id FROM users UNION SELECT bot_id FROM requests")]
        platforms = [dict(row) for row in self.db.execute(
            "SELECT platform,COUNT(*) requests, SUM(status='completed') completed FROM requests WHERE created>=? GROUP BY platform", (since,))]
        errors = [dict(row) for row in self.db.execute(
            "SELECT error_code,COUNT(*) count FROM requests WHERE created>=? AND error_code IS NOT NULL GROUP BY error_code", (since,))]
        counters = [dict(row) for row in self.db.execute(
            "SELECT bot_id,name,SUM(count) count FROM counters WHERE day>=date('now',?) GROUP BY bot_id,name", (f"-{days} days",))]
        return {"days": days, "total": aggregate(), "bots": {str(b): aggregate(b) for b in bots},
                "platforms": platforms, "errors": errors, "counters": counters}

    def close(self):
        self.db.close()
