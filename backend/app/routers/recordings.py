"""Recordings API (US-010 / plan step 17): manual record, list, retry, stop."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import LiveRecording
from app.services.recorder import begin_recording, recorder
from app.util.platform import detect_platform


def get_recorder():
    """Indirection so tests can stub the supervisor."""
    return recorder


router = APIRouter(tags=["recordings"])


class RecordRequest(BaseModel):
    url: str


def _rec_out(r: LiveRecording) -> dict:
    return {
        "id": r.id,
        "room_url": r.room_url,
        "platform": r.platform,
        "creator": r.creator,
        "origin": r.origin,
        "status": r.status,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        "output_path": r.output_path,
        "error": r.error,
        # Bytes on disk right now. A 'recording' row with a size that climbs
        # between polls is the only proof from outside the container that the
        # capture is still alive; without it the UI cannot tell a running
        # capture from one whose engine died an hour ago.
        "size_bytes": _size_of(r.output_path),
    }


def _size_of(path: str | None) -> int | None:
    """Bytes on disk, following the engine's in-flight name.

    yt-dlp writes <name>.part and renames only when the capture ends, so for
    the whole length of a recording the claimed path does not exist yet.
    Reading just that reported no size for precisely the case this field is
    here to answer -- is anything still arriving.
    """
    if not path:
        return None
    for candidate in (path, path + ".part"):
        try:
            return os.path.getsize(candidate)
        except OSError:
            continue
    return None


@router.post("/api/downloads/record-live", status_code=201)
async def record_live(
    body: RecordRequest, session: AsyncSession = Depends(get_session)
) -> dict:
    platform = detect_platform(body.url)
    if not platform:
        raise HTTPException(400, detail="Unsupported URL")
    rec = await begin_recording(body.url, platform, creator="", origin="manual")
    return _rec_out(rec)


@router.get("/api/recordings")
async def list_recordings(
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    q = (
        select(LiveRecording)
        .order_by(LiveRecording.started_at.desc(), LiveRecording.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = (await session.execute(q)).scalars().all()
    return [_rec_out(r) for r in rows]


async def _get_rec_or_404(rec_id: int, session: AsyncSession) -> LiveRecording:
    rec = await session.get(LiveRecording, rec_id)
    if not rec:
        raise HTTPException(404, detail="Recording not found")
    return rec


@router.post("/api/recordings/{recording_id}/retry")
async def retry_recording(
    recording_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    rec = await _get_rec_or_404(recording_id, session)
    if rec.status != "interrupted":
        raise HTTPException(409, detail=f"Cannot retry recording in status '{rec.status}'")
    new = await begin_recording(rec.room_url, rec.platform, rec.creator, origin=rec.origin)
    return {"retried_from": rec.id, **{
        "id": new.id,
        "status": new.status,
        "started_at": new.started_at.isoformat() if new.started_at else None,
    }}


@router.post("/api/recordings/{recording_id}/stop")
async def stop_recording(
    recording_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    rec = await _get_rec_or_404(recording_id, session)
    if rec.status != "recording":
        raise HTTPException(409, detail=f"Cannot stop recording in status '{rec.status}'")
    ok = await get_recorder().stop(recording_id)
    if not ok:
        raise HTTPException(409, detail="Recording has no active engine process")
    await session.refresh(rec)
    return {"id": rec.id, "status": rec.status}
