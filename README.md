# video-pls-bot

A self-hosted video download gateway for Telegram. Users send a link to a public
YouTube, TikTok or Instagram video; the gateway downloads it with
[yt-dlp](https://github.com/yt-dlp/yt-dlp), sends the MP4 back to the same chat, and
follows up with the audio as an MP3.

It is built to stay predictable under load rather than to be fast for one user:

- **One process, many bots.** Every bot token in the configuration is served by the same
  process, the same download queue and the same limits. Adding a bot does not multiply
  the load on the server.
- **One bounded queue.** A single FIFO queue admits a fixed number of active jobs and a
  fixed number of waiting ones, one request per Telegram user across all bots and chats.
  When it is full, new requests are refused immediately instead of piling up.
- **A disk budget, not hope.** Every active job reserves its worst-case workspace up front;
  concurrency shrinks automatically when the budget does not fit, and a watchdog kills a
  job whose files outgrow its reservation.
- **Warm extractors.** Two isolated yt-dlp worker processes stay loaded, so a request does
  not pay the start-up cost; a timed-out worker is killed with its children and replaced.
- **Fast transfers.** HLS/DASH fragments are fetched in parallel, large HTTP streams with
  ranged requests over several connections; streams are muxed by FFmpeg without
  re-encoding, and the result goes to a local Telegram Bot API server as `file://`
  (no extra upload copy, files up to 2000 MB).
- **Pluggable network egress.** All downloads go through one loopback HTTP proxy
  (`DOWNLOAD_PROXY`), so the exit network can be swapped without touching the code.
- **Shared with other services.** An optional external queue (`CORE_SOCKET`) lets other
  services on the same server that use the same proxy and accounts coordinate who
  downloads right now; the bot asks for a turn before each download and fails open.

The gateway never stores message texts, links, tokens, media or Telegram `file_id`s.

## Sources (providers)

A source is a video platform that yt-dlp can extract plus the gateway's own rules for it.
YouTube, TikTok and Instagram are supported today. A source is defined in three places:

1. `video_pls_bot/links.py` — `parse_link()` recognizes and normalizes the source's URLs
   (tracking parameters are dropped) and returns a `VideoLink(url, platform)`.
2. `video_pls_bot/extract.py` — per-platform yt-dlp options (impersonation, short-link
   resolution, format selection); the extraction itself is yt-dlp's extractor.
3. `video_pls_bot/config.py` — optional per-platform cookies (`cookies_for()`), kept in
   separate files so platform sessions never mix.

To add a source, add its URL patterns to `parse_link()`, any options it needs to
`extract.py`, cookies to `config.py` if it requires a login, and tests next to the
existing ones in `tests/test_service.py` and `tests/test_extract.py`.

Only single public videos are accepted: playlists, carousels, profiles and live streams
are refused. A video may be up to 2 000 000 000 bytes (up to 1080p on the short side);
there is no duration cap by default (`MAX_DURATION=0`) and a one-hour processing timeout.

## How a request flows

1. A message with exactly one supported link arrives in a private chat, a group (topics
   included) or a channel. Other messages are dropped by a local filter before the queue,
   without being logged or counted.
2. The queue admits the request (or answers that the user already has one, or that the
   queue is full) and the bot replies "Downloading ⏳" in the same chat and topic.
3. For long videos (over 3 minutes) or variants over 100 MB the bot first shows a preview
   with quality buttons and approximate sizes; the choice is kept in memory for 10 minutes.
4. The video is downloaded through the proxy, merged if needed, checked against the limits
   and sent with a cover, a thumbnail and a caption naming the sending bot. The status
   message shows progress and is removed once the video is delivered.
5. The MP3 (192 kbit/s) is prepared while the video uploads and is sent next.
6. The job directory is deleted in every case, including errors and timeouts.

The `run` callable given to the queue owns the whole cycle (download, upload, MP3,
cleanup), so the space and the slot are released only when everything is done:

```python
from video_pls_bot.queue import AdmissionStatus, DownloadQueue

downloads = DownloadQueue()  # One instance for all bot tokens.

# result = downloads.submit(bot_id=bot.id, user_id=user_id, run=lambda: process(message))
# result.status is accepted / user_busy / queue_full / closed.
```

## Groups and channels

The bot handles new messages and channel posts that contain one supported link: in the
text, a caption or a text link. Commands `/start` and `/help` work in private chats.
Results go to the original chat as a reply, keeping the forum topic. Links in quoted
messages, previews and buttons are ignored; edits do not trigger a new download. Messages
from other bots are skipped; posts on behalf of a chat or an anonymous admin are served
with a separate per-chat limit.

For each bot: allow groups in @BotFather (`/setjoingroups`) and either disable Group
Privacy (`/setprivacy` → Disable, then re-add the bot) or make the bot an administrator.
See [Privacy Mode](https://core.telegram.org/bots/features#privacy-mode).

## Requirements

Python 3.11+, FFmpeg/ffprobe, Node.js 22+ (for yt-dlp's JavaScript challenges), a local
[Telegram Bot API server](https://github.com/tdlib/telegram-bot-api) (built on TDLib) and
a loopback HTTP proxy for outbound traffic.

## Configuration

Everything site-specific comes from environment variables; [`.env.example`](.env.example)
lists all of them with comments. Required:

| Variable | Meaning |
| --- | --- |
| `BOT_TOKENS` | Bot tokens, comma-separated (or `BOT_TOKENS_FILE`, or the systemd credential `bot-tokens.json`) |
| `DOWNLOAD_PROXY` | Loopback HTTP proxy for all downloads |
| `TELEGRAM_API_BASE` | Local Bot API URL |
| `TELEGRAM_API_PORT` | Port of the local Bot API (for `api_server` and its unit) |
| `ALERT_CHAT_ID`, `ALERT_BOT_ID` | Where error alerts and the daily digest go, and which bot sends them |

Texts sent to Telegram are English by default; `BOT_TEXTS_FILE` points to a JSON file
with the same keys as `video_pls_bot/texts.py` to translate or reword them.

## Run locally

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
cp .env.example .env        # fill in the values
set -a; . ./.env; set +a
.venv/bin/python -m video_pls_bot.app
```

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

## Deployment (systemd)

`deploy/install.sh RELEASE_DIRECTORY` (as root) creates the service users and
directories, a venv from `requirements.lock`, links `/opt/video-pls-bot/current` and
installs the units. Credentials are provisioned separately with `systemd-creds`
(`/etc/video-pls-bot/*.cred`); non-secret settings go to
`/etc/video-pls-bot/video-pls-bot.env` (`EnvironmentFile=` of every unit).

| Unit | Role |
| --- | --- |
| `video-pls-bot.service` | All bots, the queue and the extractor workers; loopback-only network, CPU/memory/task limits |
| `video-pls-api.service` | Dedicated local Telegram Bot API (`video_pls_bot.api_server`) |
| `video-pls-api-route.service` | Optional nftables rule sending only the Bot API user's TCP to the proxy's transparent inbound (`TELEGRAM_API_REDIRECT_PORT`) |
| `video-pls-health.timer` | Independent health check every minute |
| `video-pls-daily.timer` | Daily digest at 20:00 Moscow time |

Optional installer variables: `STATS_SUDO_USER` (a user allowed to run
`video-pls-stats` via sudo) and `VIDEO_PLS_SITE_HOOK` (an executable called with the
release directory, for site-specific units and drop-ins such as the proxy service).

```sh
systemctl enable --now video-pls-api-route video-pls-api video-pls-bot
systemctl enable --now video-pls-health.timer video-pls-daily.timer
```

Before enabling the digest timer, initialize it once so that past periods are not sent:

```sh
systemd-run --unit=video-pls-daily-init --wait --collect \
  --property=User=video-pls-bot --property=Group=video-pls-bot \
  --property=WorkingDirectory=/opt/video-pls-bot/current \
  --property=LoadCredentialEncrypted=bot-tokens.json:/etc/video-pls-bot/bot-tokens.json.cred \
  --property=EnvironmentFile=/etc/video-pls-bot/video-pls-bot.env \
  /opt/video-pls-bot/venv/bin/python -m video_pls_bot.daily_reports --initialize
```

Graceful stop finishes accepted jobs (up to 13 hours for a full queue with one-hour
timeouts). `LimitFSIZE=2100000000` allows files up to the Telegram limit.

## Cookies

Some videos require a logged-in account. Cookies are per platform: `YOUTUBE_COOKIES_FILE`
/ `TIKTOK_COOKIES_FILE`, or the systemd credentials `youtube-cookies.txt` /
`tiktok-cookies.txt` (only the platform's own domains are kept). Each job gets a private
temporary copy (`COOKIE_TEMP_DIR`, tmpfs in production) that is deleted with the job.
A platform login wall is reported as `source_login_required`, not as a deleted video.

The `vpb` CLI performs the login in a separate Chrome window on an admin machine and
installs verified, encrypted cookies over a restricted SSH account: see
[docs/vpb-cli.md](docs/vpb-cli.md).

## Analytics, alerts and the daily digest

- **Analytics** (`$DATA_DIR/analytics.sqlite3`): Telegram user and bot ids, time,
  platform, outcome, sizes, per-stage timings and error codes. No texts, links, tokens or
  `file_id`s. Summary: `video-pls-stats --days 7` (`python -m video_pls_bot.app --stats`).
- **Alerts** (`$DATA_DIR/alerts.sqlite3`): failures of downloads, uploads, MP3, cleanup,
  analytics, startup, polling and a full queue go to `ALERT_CHAT_ID` from `ALERT_BOT_ID`.
  Identical errors of one bot, stage and platform are grouped (the first one immediately,
  then at most one message per 5 minutes) and carry a validated, cleaned source link.
  Undelivered alerts are kept until Telegram is reachable again.
- **Health check**: the timer checks the units (and the proxy unit named in
  `HEALTH_PROXY_UNIT`), the local Bot API, every token and the freshness of `getUpdates`,
  reports failures, restarts and recovery, and stays silent while all is well.
- **Daily digest**: one message per day with the number of delivered videos per bot,
  only for bots with deliveries; a cursor in `$DATA_DIR/daily-reports.sqlite3` prevents
  duplicates and catches up after downtime.

Technical logs go to journald with rate limits; exception texts that may contain secret
URLs are not logged.

## License

No license has been chosen yet; all rights reserved by the author until one is added.
