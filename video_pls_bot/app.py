import argparse
import asyncio
import fcntl
import json
import logging
import os
import shutil
import signal
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import aiohttp

from .analytics import Analytics
from .alerts import Reporter
from .config import Settings
from .extractor_pool import ExtractorPool
from .incoming import message_link, reply_fields, sender_id
from .media import MediaError, Selection, download_video, make_mp3
from .messages import build_video_caption
from .process import ProcessError
from .core_turn import OUTCOME, CoreQueue
from .queue import AdmissionStatus, DownloadQueue, QueueLimits
from .telegram import Bot, TelegramError
from .texts import TEXTS, t

log = logging.getLogger(__name__)



def user_error(code):
    key = f"error.{code}"
    return t(key) if key in TEXTS else t("error.download_failed")


@dataclass
class PendingSelection:
    bot: Bot
    message: dict
    link: object
    job_id: str
    selection: Selection
    progress_id: int
    photo: bool
    expires: float


class ReplyGate:
    """Bounded throttle for service messages; it never creates a reply queue."""
    def __init__(self):
        self.users = OrderedDict()
        self.global_times = []

    def allow(self, bot_id: int, user_id: int) -> bool:
        now = time.monotonic()
        key = (bot_id, user_id)
        previous = self.users.get(key, 0)
        self.global_times = [t for t in self.global_times if now - t < 1]
        if now - previous < 3 or len(self.global_times) >= 10:
            return False
        self.users[key] = now
        self.users.move_to_end(key)
        if len(self.users) > 5000:
            self.users.popitem(last=False)
        self.global_times.append(now)
        return True


class Application:
    def __init__(self, settings: Settings, analytics: Analytics):
        self.settings = settings
        self.analytics = analytics
        self.queue = DownloadQueue(QueueLimits(settings.disk_concurrency, settings.max_waiting, settings.timeout))
        self.core = CoreQueue(settings.core_socket)
        self.stop = asyncio.Event()
        self.replies = ReplyGate()
        self.reporter = None
        self.extractors = None
        self.selections: dict[str, PendingSelection] = {}
        self.pending_actors: set[int] = set()
        self.workspaces: set[Path] = set()

    def reserve_workspace(self):
        # Calls run on one event loop without awaits, including reservation.
        used = active_used = 0
        for path in self.settings.temp_dir.glob("job-*/*"):
            try:
                size = path.stat().st_size if path.is_file() else 0
            except FileNotFoundError:
                continue  # yt-dlp may rename/remove a fragment while we count.
            used += size
            if path.parent in self.workspaces:
                active_used += size
        remaining = (len(self.workspaces) + 1) * self.settings.workspace_bytes - active_used
        if (used + remaining + 1_000_000_000 > self.settings.temp_budget_bytes
                or shutil.disk_usage(self.settings.temp_dir).free < remaining + 1_000_000_000):
            raise MediaError("disk_full")
        directory = Path(tempfile.mkdtemp(prefix="job-", dir=self.settings.temp_dir))
        self.workspaces.add(directory)
        return directory

    async def edit_status(self, bot, message, progress_id, text, *, photo=False, markup=None, background=False):
        if progress_id is None:
            return
        try:
            edit = bot.update_progress if background else bot.edit_progress
            await edit(message["chat"]["id"], progress_id, text, photo=photo,
                       reply_markup=markup or {"inline_keyboard": []})
        except (TelegramError, TimeoutError):
            # Stale/missing status messages must never interrupt the download.
            log.info("progress_edit_failed bot_id=%s", bot.id)

    async def offer_selection(self, bot, message, link, job_id, selection):
        if len(self.selections) >= max(1, self.settings.max_waiting):
            raise MediaError("download_failed")
        key = uuid4().hex[:20]
        buttons = []
        for index, choice in enumerate(selection.choices):
            size = t("size_approx_mb", mb=f"{choice['bytes'] / 1_000_000:.0f}") if choice["bytes"] else t("size_up_to")
            buttons.append([{"text": f"{choice['quality']}p · {size}", "callback_data": f"q:{key}:{index}"}])
        buttons.append([{"text": t("cancel_button"), "callback_data": f"q:{key}:cancel"}])
        caption = f"{selection.title}\n\n{t('choose_quality')}"
        photo = bool(selection.thumbnail)
        try:
            if photo:
                sent = await bot.preview(message["chat"]["id"], selection.thumbnail, caption,
                                         reply_markup={"inline_keyboard": buttons}, **reply_fields(message))
            else:
                sent = await bot.text(message["chat"]["id"], caption,
                                      reply_markup={"inline_keyboard": buttons}, **reply_fields(message))
        except (TelegramError, TimeoutError):
            photo = False
            sent = await bot.text(message["chat"]["id"], caption,
                                  reply_markup={"inline_keyboard": buttons}, **reply_fields(message))
        pending = PendingSelection(bot, message, link, job_id, selection, sent["message_id"], photo,
                                   time.monotonic() + 600)
        self.selections[key] = pending
        self.pending_actors.add(sender_id(message) or message["chat"]["id"])

    def forget_selection(self, key):
        pending = self.selections.pop(key)
        self.pending_actors.discard(sender_id(pending.message) or pending.message["chat"]["id"])
        return pending

    async def expire_selections(self, *, all_pending=False):
        for key, pending in list(self.selections.items()):
            if self.selections.get(key) is pending and (all_pending or pending.expires <= time.monotonic()):
                self.forget_selection(key)
                self.analytics.update(pending.job_id, status="expired", error_code="selection_expired")
                await self.edit_status(pending.bot, pending.message, pending.progress_id,
                                       t("selection_expired"), photo=pending.photo)

    async def selection_maintenance(self):
        while not self.stop.is_set():
            await self.expire_selections()
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=15)
            except TimeoutError:
                pass

    async def handle_callback(self, bot, callback):
        async def answer(text=""):
            try:
                await bot.answer_callback(callback["id"], text)
            except (TelegramError, TimeoutError):
                pass
        parts = callback.get("data", "").split(":")
        pending = self.selections.get(parts[1]) if len(parts) == 3 and parts[0] == "q" else None
        if not pending or pending.bot.id != bot.id:
            await answer(t("selection_stale"))
            return
        origin = callback.get("message", {})
        if (origin.get("chat", {}).get("id") != pending.message["chat"]["id"]
                or origin.get("message_id") != pending.progress_id):
            await answer(t("selection_other_message"))
            return
        owner = sender_id(pending.message)
        if owner and callback.get("from", {}).get("id") != owner:
            await answer(t("selection_owner_only"))
            return
        if pending.expires <= time.monotonic():
            await answer(t("selection_stale"))
            await self.expire_selections()
            return
        key, index = parts[1:]
        if index == "cancel":
            self.forget_selection(key)
            self.analytics.update(pending.job_id, status="cancelled")
            await answer(t("cancelled"))
            await self.edit_status(bot, pending.message, pending.progress_id, t("download_cancelled"), photo=pending.photo)
            return
        if not index.isdigit() or int(index) >= len(pending.selection.choices):
            await answer(t("unknown_format"))
            return
        choice = pending.selection.choices[int(index)]
        ready = asyncio.Event()
        submitted = time.monotonic()
        async def run():
            await ready.wait()
            await self.process(bot, pending.message, pending.link, pending.job_id, submitted,
                               progress_message_id=pending.progress_id, progress_photo=pending.photo,
                               selected_format=choice["format_id"], selection=pending.selection, choice=choice)
        admission = self.queue.submit(bot_id=bot.id, user_id=owner or pending.message["chat"]["id"], run=run)
        if admission.status != AdmissionStatus.ACCEPTED:
            await answer(t("queue_full_retry"))
            return
        self.forget_selection(key)  # Atomic with admission: duplicate clicks cannot launch another job.
        try:
            self.analytics.update(pending.job_id, status="queued")
            await self.edit_status(bot, pending.message, pending.progress_id,
                                   f"{pending.selection.title}\n\n{choice['quality']}p · {t('queued')}", photo=pending.photo,
                                   background=True)
        finally:
            ready.set()
        await answer()

    def report(self, bot=None, *, stage, code, message=None, **context):
        if self.reporter:
            try:
                if message:
                    try:
                        link = message_link(message)
                    except ValueError:
                        pass
                    else:
                        context.setdefault("source_url", link.url)
                        context.setdefault("platform", link.platform)
                self.reporter.emit(bot_id=bot.id if bot else 0,
                                   username=bot.username if bot else "", stage=stage, code=code, **context)
            except Exception:
                log.error("admin_alert_record_failed")

    async def notify(self, bot: Bot, chat_id: int, text: str, *, message=None):
        try:
            return await bot.text(chat_id, text, **(reply_fields(message) if message else {}))
        except (TelegramError, TimeoutError) as exc:
            log.info("notification_failed bot_id=%s", bot.id)
            self.report(bot, stage="reply", code=f"telegram_{exc.code}" if isinstance(exc, TelegramError) else "timeout",
                        user_id=sender_id(message) if message else chat_id, message=message)

    async def process(self, bot, message, link, job_id, submitted_at=None, *, progress_message_id=None,
                      progress_photo=False, selected_format=None, selection=None, choice=None,
                      progress_future=None):
        started = time.monotonic()
        metrics = {"queue_s": round(started - submitted_at, 4) if submitted_at is not None else 0}
        audio_task = None
        directory = None
        video_sent = False
        audio_sent = False
        status = "failed"
        error = None
        stage = "download"
        context = {"platform": link.platform, "job_id": job_id, "user_id": sender_id(message),
                   "source_url": link.url}
        async def bind_progress():
            nonlocal progress_message_id, progress_future
            if progress_future is not None:
                sent = await asyncio.shield(progress_future)
                progress_message_id = sent.get('message_id') if sent else None
                progress_future = None
        async def progress(downloaded):
            if not selection or progress_message_id is None:
                return
            estimate = choice.get("bytes")
            amount = t("downloaded_mb", mb=f"{downloaded / 1_000_000:.0f}")
            if estimate:
                amount += t("of_estimate_mb", mb=f"{estimate / 1_000_000:.0f}")
            await bot.update_progress(message["chat"]["id"], progress_message_id,
                                      f"{selection.title}\n\n{choice['quality']}p · {t('downloading')}\n{amount}",
                                      photo=progress_photo, reply_markup={"inline_keyboard": []})

        async def remove_progress():
            nonlocal progress_message_id
            if progress_message_id is None:
                return
            try:
                await bot.delete_message(message["chat"]["id"], progress_message_id)
            except (TelegramError, TimeoutError) as exc:
                log.info("progress_delete_failed bot_id=%s", bot.id)
                self.report(bot, stage="reply", code=f"telegram_{exc.code}" if isinstance(exc, TelegramError) else "timeout",
                            **context)
            progress_message_id = None

        try:
            directory = self.reserve_workspace()
            self.analytics.update(job_id, status="running")
            options = {}
            if selected_format:
                options.update(format_id=selected_format, progress=progress)
                if selection.metadata_handle:
                    options['metadata_handle'] = selection.metadata_handle
                await self.edit_status(bot, message, progress_message_id,
                                       f"{selection.title}\n\n{choice['quality']}p · {t('downloading')}", photo=progress_photo,
                                       background=True)
            waited = False

            async def show_place(place):
                # The optional shared download queue (CORE_SOCKET): other clients may go first, the bot waits its turn.
                nonlocal waited
                waited = True
                await bind_progress()
                text = t("queue_place", place=place)
                if selection:
                    text = f"{selection.title}\n\n{choice['quality']}p · {text}"
                await self.edit_status(bot, message, progress_message_id, text, photo=progress_photo, background=True)

            turn = await self.core.turn(key=job_id, user_id=context["user_id"], platform=link.platform, on_place=show_place)
            turn_outcome, turn_bytes = "failed", None
            try:
                if self.extractors:
                    video = await download_video(link, directory, self.settings, extractor=self.extractors, **options)
                else:
                    video = await download_video(link, directory, self.settings, **options)
                if isinstance(video, Selection):
                    turn_outcome = "ok"      # a choice offered is not a failed download (the journal counts outcomes)
                else:
                    turn_outcome, turn_bytes = "ok", video.path.stat().st_size
            except MediaError as exc:
                code = str(exc)[:40]
                turn_outcome = code if OUTCOME.fullmatch(code) else "failed"
                raise
            except (TimeoutError, asyncio.CancelledError):
                turn_outcome = "timed_out"
                raise
            finally:
                # The bytes are here (or not): the place goes to the next one before the Telegram upload.
                await asyncio.shield(turn.release(turn_outcome, turn_bytes))
            await bind_progress()
            if waited and not isinstance(video, Selection):
                text = f"{selection.title}\n\n{choice['quality']}p · {t('downloading')}" if selection else t("downloading")
                await self.edit_status(bot, message, progress_message_id, text, photo=progress_photo, background=True)
            if isinstance(video, Selection):
                await self.offer_selection(bot, message, link, job_id, video)
                status = "awaiting_choice"
                return
            metrics.update(video.timings)
            metrics["video_ready_s"] = round(time.monotonic() - started, 4)
            # Keep the status visible while Telegram receives the whole file.
            await self.edit_status(bot, message, progress_message_id,
                                   t("uploading"), photo=progress_photo,
                                   background=True)
            self.analytics.update(job_id, video_bytes=video.path.stat().st_size, metrics=metrics)
            async def prepare_audio():
                audio_started = time.monotonic()
                try:
                    return await make_mp3(video, self.settings)
                finally:
                    metrics["mp3_s"] = round(time.monotonic() - audio_started, 4)
            if video.has_audio:
                audio_task = asyncio.create_task(prepare_audio())
            stage = "video_upload"
            upload_started = time.monotonic()
            await bot.upload("sendVideo", "video", video.path, message["chat"]["id"],
                             cover=video.cover, thumbnail=video.thumbnail,
                             caption=build_video_caption(bot.username), parse_mode="HTML",
                             supports_streaming="true", duration=int(video.duration),
                             width=video.width, height=video.height, **reply_fields(message))
            metrics["video_upload_s"] = round(time.monotonic() - upload_started, 4)
            metrics["video_delivered_s"] = round(time.monotonic() - started, 4)
            video_sent = True
            self.analytics.update(job_id, video_sent=1, metrics=metrics)
            await remove_progress()
            stage = "mp3"
            if video.has_audio:
                audio = await audio_task
                stage = "audio_upload"
                upload_started = time.monotonic()
                await bot.upload("sendAudio", "audio", audio, message["chat"]["id"],
                                 title=video.title, duration=int(video.duration),
                                 caption=build_video_caption(bot.username), parse_mode="HTML", **reply_fields(message))
                metrics["audio_upload_s"] = round(time.monotonic() - upload_started, 4)
                audio_sent = True
                status = "completed"
            else:
                status = "partial"
                error = "no_audio"
                await self.notify(bot, message["chat"]["id"], t("no_audio"), message=message)
        except asyncio.CancelledError:
            status = "partial" if video_sent else "timed_out"
            error = "timeout"
            # A short notification is bounded and does not hold the slot indefinitely.
            try:
                async with asyncio.timeout(5):
                    await self.notify(bot, message["chat"]["id"],
                                      t("timeout"), message=message)
            except TimeoutError:
                pass
            raise
        except (MediaError, ProcessError, TelegramError, TimeoutError, ValueError, OSError) as exc:
            error = str(exc) if isinstance(exc, (MediaError, ProcessError)) else (
                f"telegram_{exc.code}" if isinstance(exc, TelegramError) else ("timeout" if isinstance(exc, TimeoutError) else "internal"))
            status = "partial" if video_sent else "failed"
            text = t("mp3_failed") if video_sent else user_error(error)
            await self.notify(bot, message["chat"]["id"], text, message=message)
        except Exception:
            error = "internal"
            status = "partial" if video_sent else "failed"
            await self.notify(bot, message["chat"]["id"], t("internal_error"), message=message)
        finally:
            await bind_progress()
            await remove_progress()
            if audio_task:
                if not audio_task.done():
                    audio_task.cancel()
                await asyncio.gather(audio_task, return_exceptions=True)
            if error:
                self.report(bot, stage=stage, code=error, **context)
            if directory:
                try:
                    shutil.rmtree(directory)
                except OSError:
                    self.report(bot, stage="cleanup", code="cleanup_failed", **context)
                    log.error("workspace_cleanup_failed job_id=%s", job_id)
                finally:
                    self.workspaces.discard(directory)
            try:
                self.analytics.update(job_id, status=status, elapsed=round(time.monotonic() - started, 3),
                                      video_sent=int(video_sent), audio_sent=int(audio_sent), error_code=error, metrics=metrics)
            except Exception:
                self.report(bot, stage="analytics", code="analytics_failed", **context)
                log.error("analytics_update_failed job_id=%s", job_id)
            log.info("request_finished bot_id=%s job_id=%s status=%s error=%s seconds=%.3f video_seconds=%s",
                     bot.id, job_id, status, error, time.monotonic() - started, metrics.get("video_delivered_s"))

    async def handle(self, bot: Bot, update):
        if update.get("callback_query"):
            await self.handle_callback(bot, update["callback_query"])
            return
        message = update.get("message") or update.get("channel_post")
        if not message or message.get("chat", {}).get("type") not in ("private", "group", "supergroup", "channel"):
            return
        chat_id = message["chat"]["id"]
        private = message["chat"]["type"] == "private"
        user = message.get("from", {})
        user_id = sender_id(message)
        if (user.get("is_bot") and not message.get("sender_chat")) or (private and not user_id):
            return
        if private:
            self.analytics.seen(user_id, bot.id)
        text = message.get("text", "")
        command = text.split(maxsplit=1)[0].lower() if text else ""
        if private and command in ("/start", "/help", f"/start@{bot.username.lower()}", f"/help@{bot.username.lower()}"):
            self.analytics.count(bot.id, "start")
            if self.replies.allow(bot.id, user_id):
                await self.notify(bot, chat_id, t("welcome"), message=message)
            return
        try:
            link = message_link(message)
        except ValueError:
            if not private:
                return
            self.analytics.count(bot.id, "invalid_link")
            if self.replies.allow(bot.id, user_id):
                await self.notify(bot, chat_id, t("send_one_link"), message=message)
            return
        if not private and user_id:
            self.analytics.seen(user_id, bot.id)
        # Real users share one limit across chats/bots. Anonymous posts use the
        # destination's negative chat ID; channels never become analytics users.
        actor_id = user_id or chat_id
        if actor_id in self.pending_actors:
            if self.replies.allow(bot.id, actor_id):
                await self.notify(bot, chat_id, t("choose_previous_first"), message=message)
            return
        # submit() never awaits, so the closure's ticket is assigned before run starts.
        submitted_at = time.monotonic()
        progress_future = asyncio.get_running_loop().create_future()

        async def run():
            await self.process(bot, message, link, admission.ticket.job_id, submitted_at,
                               progress_future=progress_future)

        admission = self.queue.submit(bot_id=bot.id, user_id=actor_id, run=run)
        if admission.status == AdmissionStatus.ACCEPTED:
            self.analytics.accepted(admission.ticket.job_id, user_id, bot.id, link.platform)
            progress_message = None
            try:
                async with asyncio.timeout(5):
                    progress_message = await self.notify(bot, chat_id, t("downloading"), message=message)
            except TimeoutError:
                self.report(bot, stage="reply", code="timeout", user_id=user_id,
                            job_id=admission.ticket.job_id, platform=link.platform, source_url=link.url)
            finally:
                progress_future.set_result(progress_message)
        else:
            self.analytics.count(bot.id, admission.status.value)
            if admission.status == AdmissionStatus.QUEUE_FULL:
                self.report(bot, stage="queue", code="queue_full", platform=link.platform, user_id=user_id,
                            source_url=link.url)
            if self.replies.allow(bot.id, chat_id):
                await self.notify(bot, chat_id, t("user_busy")
                                  if admission.status == AdmissionStatus.USER_BUSY else t("queue_busy"), message=message)

    async def poll(self, bot: Bot):
        offset = self.analytics.offset(bot.id)
        failures = 0
        while not self.stop.is_set():
            try:
                updates = await bot.call("getUpdates", {"offset": offset, "timeout": 25,
                                                        "limit": 50, "allowed_updates": ["message", "channel_post", "callback_query"]}, timeout=35)
                failures = 0
                if self.reporter:
                    self.reporter.heartbeat(bot)
                    self.reporter.check(f"polling:{bot.id}", True, bot_id=bot.id, username=bot.username, stage="polling")
                for update in updates:
                    await self.handle(bot, update)
                    offset = update["update_id"] + 1
                    self.analytics.set_offset(bot.id, offset)
                    if self.stop.is_set():
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                code = exc.code if isinstance(exc, TelegramError) else "internal"
                log.warning("poll_failed bot_id=%s code=%s", bot.id, code)
                self.report(bot, stage="polling", code=f"telegram_{code}" if isinstance(code, int) else "internal")
                if self.reporter and self.reporter.store:
                    try:
                        self.reporter.store.check(f"polling:{bot.id}", False)
                    except Exception:
                        pass
                delay = min(30, 2 ** min(failures, 5))
                if isinstance(exc, TelegramError) and exc.retry_after:
                    delay = max(delay, min(exc.retry_after, 60))
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=delay)
                except TimeoutError:
                    pass

    async def run(self, check_only=False):
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=20), trust_env=False) as session:
            bots = [Bot(token, self.settings.api_base, session, media_gid=self.settings.media_gid) for token in self.settings.tokens]
            loop = asyncio.get_running_loop()
            sender_task = None
            previous_handler = loop.get_exception_handler()
            if not check_only:
                alert_bot = next(bot for bot in bots if bot.id == self.settings.alert_bot_id)
                self.reporter = Reporter(self.settings, alert_bot)
                sender_task = asyncio.create_task(self.reporter.run())
                def unhandled(_loop, _context):
                    self.report(stage="service", code="internal")
                    log.error("unhandled_async_error")
                loop.set_exception_handler(unhandled)
            try:
                for bot in bots:
                    try:
                        me = await bot.call("getMe")
                        bot.username = me["username"]
                        webhook = await bot.call("getWebhookInfo")
                        if webhook.get("url"):
                            self.report(bot, stage="startup", code="webhook_exists")
                            raise RuntimeError("Existing webhook")
                    except Exception as exc:
                        self.report(bot, stage="startup", code=f"telegram_{exc.code}" if isinstance(exc, TelegramError) else "internal")
                        raise
                    log.info("bot_ready bot_id=%s username=%s", bot.id, bot.username)
                    if self.reporter:
                        self.reporter.heartbeat(bot)
                if check_only:
                    return
                self.extractors = ExtractorPool(self.settings)
                await self.extractors.start()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.add_signal_handler(sig, self.stop.set)
                tasks = [asyncio.create_task(self.poll(bot)) for bot in bots]
                tasks.append(asyncio.create_task(self.selection_maintenance()))
                try:
                    await self.stop.wait()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await self.queue.close()
                    await self.expire_selections(all_pending=True)
            finally:
                if self.extractors:
                    await self.extractors.close()
                    self.extractors = None
                if sender_task:
                    sender_task.cancel()
                    await asyncio.gather(sender_task, return_exceptions=True)
                if self.reporter:
                    try:
                        await self.reporter.flush()
                    except Exception:
                        log.error("admin_alert_shutdown_flush_failed")
                    self.reporter.close()
                    self.reporter = None
                await asyncio.gather(*(bot.close() for bot in bots))
                loop.set_exception_handler(previous_handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.CRITICAL)
    if args.stats:
        path = Path(os.environ.get("DATA_DIR", "/var/lib/video-pls-bot")) / "analytics.sqlite3"
        # A read-only connection avoids marking live jobs interrupted.
        analytics = Analytics.__new__(Analytics)
        import sqlite3
        analytics.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            print(json.dumps(analytics.summary(args.days), ensure_ascii=False, indent=2))
        finally:
            analytics.close()
        return
    os.umask(0o077)
    settings = Settings.load()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    with (settings.data_dir / "service.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for directory in settings.temp_dir.glob("job-*"):
            if directory.is_dir() and not directory.is_symlink():
                shutil.rmtree(directory)
        analytics = Analytics(settings.data_dir / "analytics.sqlite3")
        try:
            asyncio.run(Application(settings, analytics).run(check_only=args.check))
        finally:
            analytics.close()


if __name__ == "__main__":
    main()
