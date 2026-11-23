"""Independent health check: reports transitions, remains quiet while healthy."""

import asyncio
import os
import time

import aiohttp

from .alerts import BOT_NAMES, Reporter
from .config import Settings
from .telegram import Bot, TelegramError


async def unit_status(unit):
    child = await asyncio.create_subprocess_exec("systemctl", "show", unit, "-p", "ActiveState", "-p", "NRestarts",
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        async with asyncio.timeout(3):
            output, _ = await child.communicate()
        values = dict(line.split("=", 1) for line in output.decode().splitlines() if "=" in line)
        return child.returncode == 0 and values.get("ActiveState") == "active", int(values.get("NRestarts", 0))
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


async def check():
    settings = Settings.load()
    async with aiohttp.ClientSession(trust_env=False) as session:
        token = next(t for t in settings.tokens if int(t.split(":")[0]) == settings.alert_bot_id)
        reporter = Reporter(settings, Bot(token, settings.api_base, session))
        try:
            main_active = False
            units = [("video-pls-bot.service", "service"), ("video-pls-api.service", "telegram_api")]
            # Optional systemd unit of the download proxy (DOWNLOAD_PROXY), checked like the others.
            proxy_unit = os.environ.get("HEALTH_PROXY_UNIT", "").strip()
            if proxy_unit:
                units.insert(1, (proxy_unit, "proxy"))
            for unit, stage in units:
                okay, restarts = await unit_status(unit)
                if stage == "service":
                    main_active = okay
                reporter.check(unit, okay, stage=stage, code="unavailable")
                if reporter.store and reporter.store.restarted(unit, restarts):
                    reporter.emit(stage=stage, code="process_restarted")
            if main_active and reporter.store:
                rows = reporter.store.db.execute("SELECT * FROM heartbeats").fetchall()
                for row in rows:
                    reporter.check(f"heartbeat:{row['bot_id']}", time.time() - row["last_ok"] < 100,
                                   bot_id=row["bot_id"], username=row["username"], stage="polling", code="heartbeat_stale")
            for token in settings.tokens:
                bot = Bot(token, settings.api_base, session)
                try:
                    await bot.call("getMe", timeout=3)
                    okay = True
                    code = "unavailable"
                except (TelegramError, TimeoutError) as exc:
                    okay = False
                    code = f"telegram_{exc.code}" if isinstance(exc, TelegramError) else "timeout"
                reporter.check(f"token:{bot.id}", okay, bot_id=bot.id, username=BOT_NAMES.get(bot.id, ""), stage="telegram_api", code=code)
            await reporter.flush(limit=2)
        finally:
            reporter.close()


if __name__ == "__main__":
    asyncio.run(check())
