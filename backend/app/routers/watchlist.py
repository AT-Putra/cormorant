"""Watchlist CRUD (plan step 14): add/remove creators by profile or room URL.

Creator identity is resolved with a threaded yt-dlp probe of the submitted
URL (Decision C); the poller later synthesizes probe URLs from
platform+creator_id (see services/poller.py).
"""

import asyncio
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import CreatorWatch
from app.services import ytdlp
from app.util.platform import detect_platform

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class WatchCreate(BaseModel):
    url: str
    scope: str = Field(default="both", pattern="^(lives|posts|both)$")


class WatchPatch(BaseModel):
    scope: str | None = Field(default=None, pattern="^(lives|posts|both)$")
    enabled: bool | None = None


class WatchOut(BaseModel):
    id: int
    platform: str
    creator_id: str
    display_name: str
    scope: str
    enabled: bool
    last_seen_post_id: str | None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


def _slug(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z_.-]+", "_", name).strip("_")
    return s or "creator"


def _resolve_identity(info: dict, display: str) -> str:
    """Best creator id from a probe result. info['id'] counts only for
    playlist-shaped results (channel tabs); a single video's id would group
    the wrong things."""
    cid = info.get("uploader_id") or info.get("channel_id")
    if cid:
        return str(cid)
    if info.get("_type") == "playlist" or ":tab" in str(info.get("extractor_key", "")):
        if info.get("id"):
            return str(info["id"])
    return _slug(display)


@router.post("", response_model=WatchOut, status_code=201)
async def add_watch(
    body: WatchCreate, session: AsyncSession = Depends(get_session)
) -> CreatorWatch:
    platform = detect_platform(body.url)
    if not platform:
        raise HTTPException(400, detail="Unsupported URL")

    try:
        info = await asyncio.to_thread(ytdlp.probe, body.url)
    except Exception as exc:
        raise HTTPException(
            400, detail=f"Could not resolve creator from URL: {exc}"
        ) from exc

    display = (
        info.get("channel") or info.get("uploader") or info.get("title") or ""
    ).strip()
    if not display:
        raise HTTPException(400, detail="Probe returned no creator identity")

    creator_id = _resolve_identity(info, display)

    dup = (
        await session.execute(
            select(CreatorWatch).where(
                CreatorWatch.platform == platform,
                CreatorWatch.creator_id == creator_id,
            )
        )
    ).scalar_one_or_none()
    if dup:
        raise HTTPException(409, detail="Already watching this creator")

    watch = CreatorWatch(
        platform=platform,
        creator_id=creator_id,
        display_name=display,
        scope=body.scope,
    )
    session.add(watch)
    await session.commit()
    await session.refresh(watch)
    return watch


@router.get("", response_model=list[WatchOut])
async def list_watches(
    session: AsyncSession = Depends(get_session),
) -> list[CreatorWatch]:
    rows = (
        (
            await session.execute(
                select(CreatorWatch).order_by(
                    CreatorWatch.created_at.desc(), CreatorWatch.id.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def _get_watch_or_404(watch_id: int, session: AsyncSession) -> CreatorWatch:
    watch = await session.get(CreatorWatch, watch_id)
    if not watch:
        raise HTTPException(404, detail="Watchlist entry not found")
    return watch


@router.patch("/{watch_id}", response_model=WatchOut)
async def update_watch(
    watch_id: int,
    body: WatchPatch,
    session: AsyncSession = Depends(get_session),
) -> CreatorWatch:
    watch = await _get_watch_or_404(watch_id, session)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(watch, k, v)
    await session.commit()
    await session.refresh(watch)
    return watch


@router.delete("/{watch_id}", status_code=204)
async def delete_watch(
    watch_id: int, session: AsyncSession = Depends(get_session)
) -> None:
    watch = await _get_watch_or_404(watch_id, session)
    await session.delete(watch)
    await session.commit()
