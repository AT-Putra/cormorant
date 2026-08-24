"""Activity log (US-011 / plan step 20): durable mirror of the event bus.

Every notable events.publish lands as an ActivityLog row; job.progress ticks
stay off the log (one row per percent would drown it). Suppression-type events
are canonicalized to 'notification.suppressed' regardless of who publishes
them, so AC25's "quiet hours show up in activity" holds for any producer.
"""

import asyncio
import logging

from app.models import ActivityLog, utcnow
from app.services import events

log = logging.getLogger(__name__)

SUPPRESSED_TYPE = "notification.suppressed"
SKIP_TYPES = frozenset({"job.progress"})

# First matching key wins: event payload -> ref columns.
_REF_KEYS = (
    ("job_id", "job"),
    ("recording_id", "recording"),
    ("creator_id", "watch"),
)


def _db():
    """Fresh AsyncSession resolved at call time — module reloads (tests) honored."""
    import app.db

    return app.db.async_session()


async def log_event(
    event_type: str, message: str, ref_type: str | None = None, ref_id: str | None = None
) -> None:
    async with _db() as s:
        s.add(
            ActivityLog(
                ts=utcnow(),
                event_type=event_type,
                message=message,
                ref_type=ref_type,
                ref_id=ref_id,
            )
        )
        await s.commit()


def _message(event: dict) -> str:
    extras = " ".join(f"{k}={v}" for k, v in sorted(event.items()) if k != "type")
    return f"{event.get('type', 'event')} {extras}".strip()


def _ref_of(event: dict) -> tuple[str | None, str | None]:
    for key, ref_type in _REF_KEYS:
        if event.get(key) is not None:
            return ref_type, str(event[key])
    return None, None


_pending: set[asyncio.Task] = set()


def _mirror(event: dict) -> None:
    """Sync subscriber: hop the write onto the running loop; drop if none."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(_write(event))
    _pending.add(t)
    t.add_done_callback(_pending.discard)


async def _write(event: dict) -> None:
    etype = event.get("type") or "event"
    if etype in SKIP_TYPES:
        return
    if "suppress" in etype and etype != SUPPRESSED_TYPE:
        etype = SUPPRESSED_TYPE
    try:
        await log_event(etype, _message(event), *_ref_of(event))
    except Exception:
        log.exception("activity log write failed for %s", etype)


def install() -> None:
    """Subscribe the mirror once (idempotent across app builds/test reloads)."""
    if _mirror not in events._subscribers:
        events.subscribe(_mirror)


install()
