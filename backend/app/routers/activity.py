"""Activity log API (US-011): paginated, newest-first, optional type filter."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import ActivityLog

router = APIRouter(prefix="/api/activity", tags=["activity"])


def _out(r: ActivityLog) -> dict:
    return {
        "id": r.id,
        "ts": r.ts.isoformat() if r.ts else None,
        "event_type": r.event_type,
        "message": r.message,
        "ref_type": r.ref_type,
        "ref_id": r.ref_id,
    }


@router.get("")
async def list_activity(
    limit: int = 50,
    offset: int = 0,
    event_type: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    q = select(ActivityLog).order_by(ActivityLog.ts.desc(), ActivityLog.id.desc())
    if event_type:
        q = q.where(ActivityLog.event_type == event_type)
    q = q.limit(limit).offset(offset)
    rows = (await session.execute(q)).scalars().all()
    return [_out(r) for r in rows]
