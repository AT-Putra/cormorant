"""Library API (US-013 / plan step 19): list, range-stream, thumbnail, delete.

Filesystem paths never leave the server — items are addressed by DB id and
served via /stream and /thumbnail routes.
"""

import os
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import LibraryItem

CHUNK = 256 * 1024  # sync read size per await — Decision C: no big reads on the loop

router = APIRouter(tags=["library"])

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _serialize(item: LibraryItem) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "platform": item.platform,
        "creator": item.creator,
        "media_type": item.media_type,
        "size_bytes": item.size_bytes,
        "duration_seconds": item.duration_seconds,
        "has_thumbnail": bool(item.thumbnail_path),
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


async def _get_item_or_404(item_id: int, session: AsyncSession) -> LibraryItem:
    item = await session.get(LibraryItem, item_id)
    if not item:
        raise HTTPException(404, detail="Library item not found")
    return item


@router.get("/api/library")
async def list_library(
    platform: str | None = None,
    creator: str | None = None,
    media_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    q = select(LibraryItem).order_by(
        LibraryItem.created_at.desc(), LibraryItem.id.desc()
    )
    if platform:
        q = q.where(LibraryItem.platform == platform)
    if creator:
        q = q.where(LibraryItem.creator == creator)
    if media_type:
        q = q.where(LibraryItem.media_type == media_type)
    rows = (
        (await session.execute(q.limit(limit).offset(offset))).scalars().all()
    )
    return [_serialize(i) for i in rows]


@router.get("/api/library/{item_id}/thumbnail")
async def get_thumbnail(
    item_id: int, session: AsyncSession = Depends(get_session)
) -> Response:
    item = await _get_item_or_404(item_id, session)
    if not item.thumbnail_path or not Path(item.thumbnail_path).is_file():
        raise HTTPException(404, detail="Thumbnail not found")
    data = await _read_file_range(Path(item.thumbnail_path), None)
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(Path(item.thumbnail_path).suffix.lower(), "application/octet-stream")
    return Response(content=data, media_type=media_type)


@router.get("/api/library/{item_id}/stream")
async def stream_item(
    item_id: int,
    request: Request,
    download: bool = False,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Manual range-capable file server so <video> can seek.

    download=1 adds Content-Disposition: attachment so the same bytes save to
    disk instead of playing inline — the only way to retrieve media when the
    app runs on a remote VM.
    """
    item = await _get_item_or_404(item_id, session)
    path = Path(item.file_path)
    if not path.is_file():
        raise HTTPException(404, detail="File missing from disk")
    total = path.stat().st_size

    start = 0
    end = total - 1
    status = 200
    range_header = request.headers.get("range")
    if range_header:
        m = _RANGE_RE.match(range_header.strip())
        if not m or (not m.group(1) and not m.group(2)):
            raise HTTPException(
                416, detail="Invalid Range", headers={"Content-Range": f"bytes */{total}"}
            )
        if m.group(1):  # bytes=start-end | bytes=start-
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else total - 1
        else:  # suffix: bytes=-N (last N bytes)
            start = max(0, total - int(m.group(2)))
            end = total - 1
        if start > end or start >= total:
            raise HTTPException(
                416, detail="Range Not Satisfiable",
                headers={"Content-Range": f"bytes */{total}"},
            )
        end = min(end, total - 1)
        status = 206

    length = end - start + 1
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Type": "video/mp4" if path.suffix.lower() in (".mp4", ".mkv") else "application/octet-stream",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    if download:
        # Titles are routinely non-ASCII (CJK), which a bare filename= header
        # cannot carry; RFC 5987 filename* is the portable form.
        headers["Content-Disposition"] = (
            f"attachment; filename*=UTF-8''{quote(path.name, safe='')}"
        )

    body = await _read_file_range(path, (start, end))
    return Response(content=body, status_code=status, headers=headers)


async def _read_file_range(path: Path, span: tuple[int, int] | None) -> bytes:
    """Read [start,end] off the event loop via to_thread chunks."""
    import anyio

    def _read() -> bytes:
        parts: list[bytes] = []
        with open(path, "rb") as f:
            if span is None:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    parts.append(chunk)
            else:
                f.seek(span[0])
                remaining = span[1] - span[0] + 1
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    parts.append(chunk)
                    remaining -= len(chunk)
        return b"".join(parts)

    return await anyio.to_thread.run_sync(_read)


@router.delete("/api/library/{item_id}", status_code=204)
async def delete_item(
    item_id: int, session: AsyncSession = Depends(get_session)
) -> Response:
    item = await _get_item_or_404(item_id, session)
    for p in (item.file_path, item.thumbnail_path):
        if p:
            Path(p).unlink(missing_ok=True)  # ponytail: no empty-dir sweep; add when the tree gets messy
    await session.execute(sa_delete(LibraryItem).where(LibraryItem.id == item.id))
    await session.commit()
    return Response(status_code=204)
