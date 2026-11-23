"""Every text the bots send to Telegram (users, alerts, daily digest).

English is the built-in default. BOT_TEXTS_FILE may point to a JSON object with the same
keys to translate or reword them; unknown keys and values of the wrong type are ignored,
missing keys keep the default. A plural entry is a list: two forms (one, other) use the
English rule, three forms (one, few, many) use the East Slavic rule.
"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULTS = {
    # Private chat and download flow.
    "welcome": ("Hi! 👋\n\n"
                "Send me a link to a TikTok, Instagram or YouTube video: I will download it, send it here "
                "and then send its sound as MP3. Maximum video size: 2000 MB."),
    "send_one_link": "Send one link to a YouTube, TikTok or Instagram video.",
    "choose_previous_first": "First choose the quality of the previous video or press “Cancel”.",
    "user_busy": "Your previous request is still being processed. Please wait for the result.",
    "queue_busy": "Too many requests right now. Please try again a bit later.",
    "downloading": "Downloading ⏳",
    "queue_place": "In queue: #{place} ⏳",
    "queued": "In queue ⏳",
    "downloaded_mb": "{mb} MB",
    "of_estimate_mb": " of ≈ {mb} MB",
    "uploading": "Video downloaded. Uploading to Telegram ⏳\nA large file may take a while.",
    "no_audio": "This video has no audio track, so an MP3 cannot be made.",
    "timeout": "Processing took too long. Please try again later.",
    "mp3_failed": "The video was sent, but the MP3 could not be prepared or sent.",
    "internal_error": "An error occurred during processing. Please try again later.",
    # Quality selection card.
    "size_approx_mb": "≈ {mb} MB",
    "size_up_to": "up to 2000 MB",
    "cancel_button": "Cancel",
    "choose_quality": "Choose the video quality (up to 2000 MB).\nSizes are approximate. The choice is available for 10 minutes.",
    "selection_expired": "The choice has expired. Send the link again.",
    "selection_stale": "This choice is outdated. Send the link again.",
    "selection_other_message": "This choice belongs to another message.",
    "selection_owner_only": "Only the sender of the link can choose the quality.",
    "cancelled": "Cancelled",
    "download_cancelled": "Download cancelled.",
    "unknown_format": "Unknown format.",
    "queue_full_retry": "The queue is full right now. Press again a bit later.",
    # Fallback titles and the caption under every video and MP3.
    "title_video": "Video",
    "title_audio": "Audio",
    "caption": "❤️ Downloaded with <b>@{username}</b>",
    # Errors shown to users, by error code.
    "error.source_login_required": "The platform requires a login to access this video. Try another link.",
    "error.duration_limit": "The video is longer than allowed.",
    "error.size_limit": "The video is larger than 2000 MB. Send the link again and choose a lower quality.",
    "error.format_unavailable": "This quality is no longer available. Send the link again and choose another option.",
    "error.disk_full": "There is not enough free space for the video right now. Please try again later.",
    "error.source_unavailable": ("Could not get the video: it is unavailable or the platform restricted downloading. "
                                 "Try another link."),
    "error.single_video_required": "Send a link to a single video, not a playlist, collection or live stream.",
    "error.download_failed": "Could not download the video. Try another link or try again later.",
    # Admin alerts.
    "alert.title_problem": "⚠️ Bot problem",
    "alert.title_recovered": "✅ Recovered",
    "alert.title_test": "🧪 Alert test",
    "alert.all_bots": "All bots of the project",
    "alert.bot": "Bot",
    "alert.stage": "Stage",
    "alert.reason": "Reason",
    "alert.platform": "Platform",
    "alert.job": "Job",
    "alert.user": "User",
    "alert.video": "Video",
    "alert.video_last_failure": "Video of the last failure",
    "alert.events": "Events in this alert",
    "alert.time": "Time",
    "alert.default_stage": "Request processing",
    "alert.default_reason": "Telegram API error",
    "stage.startup": "Bot startup", "stage.polling": "Receiving messages", "stage.download": "Video download",
    "stage.video_upload": "Video upload", "stage.mp3": "MP3 creation", "stage.audio_upload": "MP3 upload",
    "stage.reply": "Sending a reply", "stage.cleanup": "Temporary file cleanup", "stage.analytics": "Analytics",
    "stage.queue": "Shared queue", "stage.service": "Service", "stage.proxy": "Proxy connection",
    "stage.telegram_api": "Local Telegram Bot API", "stage.test": "Alert test",
    "reason.source_login_required": "The platform requires authorization for this video; valid cookies of its account are needed",
    "reason.source_unavailable": "The platform restricted access or the video is unavailable",
    "reason.duration_limit": "The video exceeds the duration limit",
    "reason.size_limit": "Size limit exceeded", "reason.no_audio": "The source video has no audio",
    "reason.format_unavailable": "The selected quality is no longer available",
    "reason.disk_full": "Not enough space to process the video",
    "reason.single_video_required": "The link points to a collection or a live stream",
    "reason.download_failed": "The downloader could not get the video", "reason.process_failed": "yt-dlp or FFmpeg failed",
    "reason.timeout": "Processing time exceeded", "reason.output_limit": "Process output limit exceeded",
    "reason.queue_full": "The shared queue is full", "reason.cleanup_failed": "Could not delete temporary files",
    "reason.internal": "Unexpected error", "reason.analytics_failed": "Could not update analytics",
    "reason.webhook_exists": "The bot already has a webhook set", "reason.unavailable": "Service unavailable",
    "reason.heartbeat_stale": "The bot has not had a successful getUpdates response for a long time",
    "reason.recovered": "Recovered", "reason.test": "Alert delivery is enabled for all bots",
    "reason.alert_storage_failed": "Could not write the alert to disk",
    "reason.process_restarted": "systemd restarted the process after a failure",
    # Daily digest.
    "digest.title": "📊 Downloads in the last 24 hours",
    "digest.bot_fallback": "Bot {bot_id}",
    "digest.videos": ["{count} video", "{count} videos"],
    # Time zone label after dates in alerts and the digest (times are Europe/Moscow).
    "timezone_label": "MSK",
}


def load(path=None):
    texts = dict(DEFAULTS)
    path = path if path is not None else os.environ.get("BOT_TEXTS_FILE", "").strip()
    if not path:
        return texts
    try:
        override = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("bot_texts_unreadable")
        return texts
    if not isinstance(override, dict):
        return texts
    for key, value in override.items():
        default = DEFAULTS.get(key)
        if isinstance(default, str) and isinstance(value, str):
            texts[key] = value
        elif (isinstance(default, list) and isinstance(value, list) and len(value) in (2, 3)
              and all(isinstance(v, str) for v in value)):
            texts[key] = value
    return texts


TEXTS = load()


def t(key, **values):
    text = TEXTS[key]
    return text.format(**values) if values else text


def plural(key, count, **values):
    forms = TEXTS[key]
    if len(forms) == 2:
        form = forms[0] if count == 1 else forms[1]
    elif count % 10 == 1 and count % 100 != 11:
        form = forms[0]
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        form = forms[1]
    else:
        form = forms[2]
    return form.format(count=count, **values)


def prefixed(prefix):
    """{code: text} for keys like 'stage.download' -> {'download': ...}."""
    return {key[len(prefix) + 1:]: value for key, value in TEXTS.items() if key.startswith(prefix + ".")}
