"""Watchlist poller (plan step 15): interval asyncio loop over CreatorWatches.

Every yt-dlp call runs via asyncio.to_thread (Decision C) and probes only —
no hand-rolled platform API clients (Principle 1). One flat probe per creator
feeds both the live-status and the new-post decision. Failures are isolated
per creator: one broken extractor never kills the sweep.
"""

import asyncio
import logging
import re

from sqlalchemy import select

from app import models
from app.services import events, ytdlp
from app.services.settings_store import aget_settings

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 300


def _db():
    """Fresh AsyncSession resolved at call time — module reloads (tests)
    must be honored."""
    import app.db

    return app.db.async_session()


def get_recorder():
    """Indirection so tests can stub the supervisor (and the import stays
    lazy against services/recorder.py, owned by another workstream)."""
    from app.services.recorder import recorder

    return recorder


def get_manager():
    """Indirection so tests can stub the download manager."""
    from app.services.downloader import manager

    return manager


_PROFILE_TEMPLATES = {
    "bilibili": "https://space.bilibili.com/{id}",
    "instagram": "https://www.instagram.com/{id}/",
    "tiktok": "https://www.tiktok.com/@{id}",
    "douyin": "https://www.douyin.com/user/{id}",
    "xhs": "https://www.xiaohongshu.com/user/profile/{id}",
}


# Platforms whose live room is a fixed path off the handle we already store.
# Unlike bilibili's rooms (a separate id space, see services/live_rooms), these
# need no lookup — which matters, because the alternative the poller falls back
# to does not work: a tiktok PROFILE probe routes to tiktok:user, an extractor
# that currently dies on "Unable to extract secondary user ID", and even when it
# answers it describes a video listing that carries no live flag at all. Pointed
# at /live instead, the same probe reaches tiktok:live and reports is_live.
_LIVE_TEMPLATES = {
    "tiktok": "https://www.tiktok.com/@{id}/live",
}


def _safe_id(watch: models.CreatorWatch) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]", "", str(watch.creator_id)) or "user"


def profile_url(watch: models.CreatorWatch) -> str:
    return _PROFILE_TEMPLATES.get(watch.platform, "").format(id=_safe_id(watch))


def live_url(watch: models.CreatorWatch) -> str | None:
    """The URL the live check should probe, or None when only the profile is
    left to look at. A stored room always wins: the user typed it, or
    live_rooms resolved it at add time."""
    if watch.live_url:
        return watch.live_url
    template = _LIVE_TEMPLATES.get(watch.platform)
    return template.format(id=_safe_id(watch)) if template else None


def room_url(watch: models.CreatorWatch) -> str:
    """Where a capture would point: the creator's live room when one is known,
    else their profile (douyin/instagram serve the stream off the profile URL
    itself)."""
    return live_url(watch) or profile_url(watch)


# yt-dlp's normal answer for an idle room (BiliLiveIE raises "Streamer is not
# live"). Offline is the expected state, not a broken sweep.
_OFFLINE_RE = re.compile(
    r"not live|is offline|no longer live|live event will begin|hasn.t started",
    re.IGNORECASE,
)


def is_live(info: dict) -> bool:
    """Flat-probe live signal: is_live bool or yt-dlp's live_status field."""
    return bool(info.get("is_live") or info.get("live_status") == "is_live")


def latest_entry(info: dict) -> dict | None:
    """Newest entry of a flat channel/timeline listing."""
    # ponytail: assumes newest-first flat listings (true for IG/TikTok/douyin/
    # bilibili timelines today); sort entries if a platform ships oldest-first.
    for e in info.get("entries") or []:
        if e.get("id"):
            return e
    return None


class PollerService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        # Sleep BEFORE the first sweep: boot-time catch-up is the recorder's
        # job (reconcile_on_boot), and short-lived apps (tests) must never
        # fire a stray probe tick.
        while True:
            try:
                interval = await self.current_interval()
            except Exception:
                log.exception("reading poll interval failed")
                interval = DEFAULT_INTERVAL_S
            await asyncio.sleep(interval)
            try:
                await self.poll_once()
            except Exception:
                log.exception("poll sweep failed")

    async def current_interval(self) -> int:
        """Poll interval from settings, floored at 60s (rate-limit guard)."""
        async with _db() as s:
            settings = await aget_settings(s)
            return max(60, int(settings.poll_interval_seconds))

    # ---- sweep -------------------------------------------------------------

    async def poll_once(self) -> None:
        """One sweep over every enabled watch."""
        async with _db() as s:
            watches = (
                (
                    await s.execute(
                        select(models.CreatorWatch).where(
                            models.CreatorWatch.enabled.is_(True)
                        )
                    )
                )
                .scalars()
                .all()
            )
        for w in watches:
            try:
                await self.poll_creator(w)
            except Exception as exc:
                log.exception(
                    "poll failed for %s/%s", w.platform, w.creator_id
                )
                events.publish(
                    {
                        "type": "watch.poll_error",
                        "platform": w.platform,
                        "creator_id": w.creator_id,
                        "error": str(exc)[:200],
                    }
                )

    async def poll_creator(self, watch: models.CreatorWatch) -> None:
        """Probe one creator and act per scope. Raises on probe failure."""
        url = profile_url(watch)
        if not url:
            raise ValueError(f"no profile template for platform {watch.platform}")
        wants_live = watch.scope in ("lives", "both")
        wants_posts = watch.scope in ("posts", "both")
        # A known room answers the live question on its own, so a lives-only
        # watch never touches the listing — which is what keeps bilibili lives
        # working while its space API is rate-limiting us (412), and what keeps
        # tiktok lives off the tiktok:user extractor entirely.
        room = live_url(watch)
        needs_listing = wants_posts or (wants_live and not room)

        # Same reason as the watchlist resolve probe: stored cookies decide
        # what a probe can see, and only the newest page matters — latest_entry
        # reads the head of the listing.
        from app.routers.credentials import aget_cookiefile

        cookiefile = await aget_cookiefile(watch.platform)
        cookie_path = str(cookiefile) if cookiefile else None
        try:
            listing = (
                await asyncio.to_thread(
                    ytdlp.probe,
                    url,
                    cookie_path,
                    extract_flat=True,
                    playlist_items="1-5",
                )
                if needs_listing
                else {}
            )
            if wants_live:
                await self._check_live(
                    watch, await self._live_info(watch, room, listing, cookie_path)
                )
        finally:
            if cookiefile:
                cookiefile.unlink(missing_ok=True)

        if wants_posts:
            await self._check_posts(watch, listing)

    async def _live_info(
        self,
        watch: models.CreatorWatch,
        room: str | None,
        listing: dict,
        cookie_path: str | None,
    ) -> dict:
        """Live status for one creator: their own room when one is known, else
        the live flags the profile listing already carries."""
        if not room:
            return listing
        try:
            return await asyncio.to_thread(ytdlp.probe, room, cookie_path)
        except Exception as exc:
            if _OFFLINE_RE.search(str(exc)):
                return {}
            raise

    # ---- decisions ---------------------------------------------------------

    async def _check_live(self, watch: models.CreatorWatch, info: dict) -> None:
        if not is_live(info):
            return
        if await self._has_active_recording(watch):
            return  # already capturing this creator
        async with _db() as s:
            rec = models.LiveRecording(
                room_url=room_url(watch),
                platform=watch.platform,
                creator=watch.display_name,
                origin="watchlist",
            )
            s.add(rec)
            await s.commit()
            await s.refresh(rec)
        events.publish(
            {
                "type": "watch.golive",
                "recording_id": rec.id,
                "creator": watch.display_name,
            }
        )
        # Sync fire-and-forget: spawns the supervise task; awaiting it would
        # stall this sweep until capture ends.
        get_recorder().start_recording(rec.id)

    async def _has_active_recording(self, watch: models.CreatorWatch) -> bool:
        async with _db() as s:
            row = (
                await s.execute(
                    select(models.LiveRecording.id).where(
                        models.LiveRecording.platform == watch.platform,
                        models.LiveRecording.creator == watch.display_name,
                        models.LiveRecording.status == "recording",
                    )
                )
            ).first()
        return row is not None

    async def _check_posts(self, watch: models.CreatorWatch, info: dict) -> None:
        entry = latest_entry(info)
        if not entry:
            return
        pid = str(entry["id"])
        if pid == watch.last_seen_post_id:
            return
        url = entry.get("url") or entry.get("webpage_url")
        if not url:
            log.warning(
                "post %s of %s/%s has no url; skipping",
                pid,
                watch.platform,
                watch.creator_id,
            )
            return

        async with _db() as s:
            job = models.DownloadJob(
                url=str(url),
                platform=watch.platform,
                kind="video",
                title=entry.get("title") or f"Post {pid}",
                creator=watch.display_name,
                format_id=None,
                selected_quality=(await aget_settings(s)).default_quality,
                status="queued",
                is_auto=True,
            )
            s.add(job)
            await s.commit()
            await s.refresh(job)

        get_manager().enqueue(job.id)
        events.publish(
            {
                "type": "watch.new_post",
                "job_id": job.id,
                "creator": watch.display_name,
                "post_id": pid,
            }
        )

        # Cursor advances only after the row committed AND the enqueue
        # returned (plan step 15); a crash before here just re-detects the
        # same post next sweep, where AC19's dup check absorbs the retry.
        async with _db() as s:
            row = await s.get(models.CreatorWatch, watch.id)
            if row:
                row.last_seen_post_id = pid
                await s.commit()


poller = PollerService()  # singleton wired into main.py lifespan (plan step 15)
