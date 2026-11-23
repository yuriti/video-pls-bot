"""Start the project's Bot API without putting application secrets in argv."""

import os
import re
from pathlib import Path


def main():
    if os.environ.get("CREDENTIALS_DIRECTORY"):
        path = Path(os.environ["CREDENTIALS_DIRECTORY"]) / "telegram-api.env"
        values = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    else:
        values = {name: os.environ.get(name, "") for name in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH")}
    port = os.environ.get("TELEGRAM_API_PORT", "")
    if not re.fullmatch(r"\d{1,5}", port) or not 0 < int(port) < 65536:
        raise ValueError("TELEGRAM_API_PORT is required")
    if not re.fullmatch(r"\d+", values.get("TELEGRAM_API_ID", "")) or not re.fullmatch(r"[a-fA-F0-9]{32}", values.get("TELEGRAM_API_HASH", "")):
        raise ValueError("Invalid Telegram application credentials")
    os.environ.update(values)
    os.execv("/usr/local/bin/telegram-bot-api", ["telegram-bot-api", "--local", "--http-ip-address=127.0.0.1",
              f"--http-port={port}", "--dir=/var/lib/video-pls-api", "--temp-dir=/var/cache/video-pls-api",
              "--verbosity=0", "--memory-verbosity=0", "--log=/dev/null"])


if __name__ == "__main__":
    main()
