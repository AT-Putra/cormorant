"""Notification channel API + event->webhook glue (plan step 18).

One NotificationRule row (single channel). Token/config stored Fernet-
encrypted via app.crypto. Event glue subscribes to services/events and
routes go-live / recording-end / download-failed through Notifier.send_event,
honoring per-CreatorWatch toggles and global quiet hours. Quiet-hours
suppression publishes notification.suppressed so the activity log (US-011)
records it.
"""

import asyncio
import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crypto, models
from app.db import get_session
from app.services import events
from app.services import notifier as notifier_mod
from app.services.notifier import Notifier

router = APIRouter(prefix="/api/notifications", tags=["notifications"])
log = logging.getLogger(__name__)

# services/events type -> notifier event type. Manual recordings never
# notify go-live (origin checked below); recording failures share the
# recording toggle via "recording_finished".
_EVENT_MAP = {
    "recording.started": "golive",
    "recording.finished": "recording_finished",
    "recording.failed": "recording_finished",
    "recording.interrupted": "recording_finished",
    "job.failed": "download_failed",
    # Not tied to any creator, so it carries no per-watch toggle: a dead cookie
    # jar degrades every tiktok capture at once, and only the user can fix it.
    "credentials.stale": "credentials_stale",
}


# ---- config CRUD -----------------------------------------------------------


class ChannelConfig(BaseModel):
    channel_type: str = Field(pattern="^(ntfy|telegram|discord)$")
    target: str = Field(min_length=1)
    token: str | None = None
    quiet_hours_start: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    quiet_hours_end: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")


class TestSend(BaseModel):
    message: str | None = None


def _mask_target(channel_type: str, target: str) -> str:
    """Never echo secrets: discord webhook URLs embed their token in the last
    path segment, so only the numeric id is shown."""
    if channel_type == "discord":
        m = re.match(r"https://discord\.com/api/webhooks/\d+", target)
        return f"{m.group(0)}/..." if m else "..."
    return "..." + target[-4:]


def _public(row: models.NotificationRule | None) -> dict:
    if row is None:
        return {
            "configured": False,
            "channel_type": None,
            "target_masked": None,
            "quiet_hours_start": None,
            "quiet_hours_end": None,
        }
    return {
        "configured": True,
        "channel_type": row.channel_type,
        "target_masked": _mask_target(row.channel_type, row.target),
        "quiet_hours_start": row.quiet_hours_start,
        "quiet_hours_end": row.quiet_hours_end,
    }


def _rule_dict(row: models.NotificationRule) -> dict:
    """Shape Notifier expects, token decrypted out of the blob."""
    token = json.loads(crypto.decrypt_cookie_blob(row.encrypted_config)).get("token")
    return {
        "channel_type": row.channel_type,
        "target": row.target,
        "token": token,
        "quiet_hours_start": row.quiet_hours_start,
        "quiet_hours_end": row.quiet_hours_end,
    }


async def _rule_row(session: AsyncSession) -> models.NotificationRule | None:
    return (
        (await session.execute(select(models.NotificationRule))).scalars().first()
    )


@router.get("/config")
async def read_config(session: AsyncSession = Depends(get_session)):
    return _public(await _rule_row(session))


@router.put("/config")
async def write_config(
    body: ChannelConfig, session: AsyncSession = Depends(get_session)
):
    for v in (body.quiet_hours_start, body.quiet_hours_end):
        if v is not None and notifier_mod._parse_hhmm(v) is None:
            raise HTTPException(422, detail="quiet hours must be HH:MM (00:00-23:59)")

    blob = crypto.encrypt_cookie_text(json.dumps({"token": body.token or ""}))
    row = await _rule_row(session)
    if row is None:
        row = models.NotificationRule(
            channel_type=body.channel_type,
            target=body.target,
            encrypted_config=blob,
            quiet_hours_start=body.quiet_hours_start,
            quiet_hours_end=body.quiet_hours_end,
        )
        session.add(row)
    else:
        row.channel_type = body.channel_type
        row.target = body.target
        row.encrypted_config = blob
        row.quiet_hours_start = body.quiet_hours_start
        row.quiet_hours_end = body.quiet_hours_end
    await session.commit()
    await session.refresh(row)
    return _public(row)


@router.post("/test")
async def send_test(body: TestSend, session: AsyncSession = Depends(get_session)):
    row = await _rule_row(session)
    if row is None:
        raise HTTPException(409, detail="not configured")
    # Stored quiet hours apply here too — the button exercises the real path.
    delivered = await Notifier(_rule_dict(row)).send_event(
        "test", {"message": body.message or "Cormorant test notification"}
    )
    return {"delivered": delivered}


@router.delete("/config", status_code=204)
async def delete_config(session: AsyncSession = Depends(get_session)) -> None:
    row = await _rule_row(session)
    if row:
        await session.delete(row)
        await session.commit()


# ---- event glue ------------------------------------------------------------


def _lazy_db():
    """Fresh module ref — tests reload app.db per test (see poller._db)."""
    import app.db

    return app.db


async def _toggles(session: AsyncSession, platform: str, creator: str) -> dict:
    """Per-creator webhook toggles from the matching CreatorWatch, if any.
    Empty dict = no watch = notifier defaults to sending."""
    watch = (
        (
            await session.execute(
                select(models.CreatorWatch).where(
                    models.CreatorWatch.platform == platform,
                    models.CreatorWatch.display_name == creator,
                )
            )
        )
        .scalars()
        .first()
    )
    if watch is None:
        return {}
    return {
        "notify_golive": watch.notify_golive,
        "notify_recording": watch.notify_recording,
        "notify_posts": watch.notify_posts,
    }


async def _build_context(etype: str, event: dict) -> dict | None:
    if etype == "credentials_stale":
        platform = event.get("platform", "?")
        return {
            "platform": platform,
            "title": f"{platform} login needs attention",
            "message": (
                f"[{platform}] {event.get('detail') or event.get('state')}\n"
                "Re-export the cookies in Settings -> Credentials; until then "
                "captures fall back to whatever an anonymous client is offered."
            ),
        }
    db = _lazy_db()
    async with db.async_session() as s:
        if etype == "golive":
            rec = await s.get(models.LiveRecording, event.get("recording_id"))
            if rec is None or rec.origin != "watchlist":
                return None  # manual starts: the user is already watching
            return {
                "platform": rec.platform,
                "creator": event.get("creator") or rec.creator,
                "title": f"{rec.creator} is live",
                "url": rec.room_url,
                "creator_toggles": await _toggles(s, rec.platform, rec.creator),
            }
        if etype == "recording_finished":
            rec = await s.get(models.LiveRecording, event.get("recording_id"))
            if rec is None:
                return None
            word = event["type"].removeprefix("recording.")
            title = f"{rec.creator} — recording {word}"
            if event.get("error"):
                title += f": {event['error']}"
            return {
                "platform": rec.platform,
                "creator": rec.creator,
                "title": title,
                "url": rec.output_path or rec.room_url,
                "creator_toggles": await _toggles(s, rec.platform, rec.creator),
            }
        # download_failed
        job = await s.get(models.DownloadJob, event.get("job_id"))
        if job is None:
            return None
        return {
            "platform": job.platform,
            "creator": job.creator,
            "title": f"{job.title} — failed",
            "url": job.url,
        }


async def _dispatch_notification(event: dict) -> None:
    db = _lazy_db()
    async with db.async_session() as s:
        rule_row = (await s.execute(select(models.NotificationRule))).scalars().first()
    if rule_row is None:
        return
    etype = _EVENT_MAP[event["type"]]
    ctx = await _build_context(etype, event)
    if ctx is None:
        return
    # Same gate order as Notifier.send_event, but observable: opted-out
    # creators get true silence; only quiet hours publish .suppressed.
    toggle = notifier_mod.EVENT_TOGGLES.get(etype)
    if toggle is not None and (ctx.get("creator_toggles") or {}).get(toggle) is False:
        return
    rule = _rule_dict(rule_row)
    # Pre-gate quiet hours so suppression is observable (activity log);
    # Notifier re-checks harmlessly on the send path.
    if notifier_mod.in_quiet_hours(
        notifier_mod._now(), rule["quiet_hours_start"], rule["quiet_hours_end"]
    ):
        events.publish(
            {
                "type": "notification.suppressed",
                "event": event["type"],
                "creator": ctx.get("creator"),
            }
        )
        return
    await Notifier(rule).send_event(etype, ctx)


async def _safe_handle(event: dict) -> None:
    try:
        await _dispatch_notification(event)
    except Exception:
        log.exception("notification handling failed for %s", event.get("type"))


# Live handler tasks — lets tests drain deterministically and shutdown cancel.
_pending: set[asyncio.Task] = set()


def _on_event(event: dict) -> None:
    """Sync bridge: services publish from inside running loops."""
    if event.get("type") not in _EVENT_MAP:
        return
    try:
        task = asyncio.get_running_loop().create_task(_safe_handle(event))
    except RuntimeError:
        log.debug("event %s published off-loop; notification skipped", event["type"])
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


events.subscribe(_on_event)
