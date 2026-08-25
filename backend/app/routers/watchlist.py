"""Watchlist CRUD (plan step 14): add/remove creators by profile or room URL.

Creator identity is resolved with a threaded yt-dlp probe of the submitted
URL (Decision C); the poller later synthesizes probe URLs from
platform+creator_id (see services/poller.py).
"""

import asyncio
import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import CreatorWatch
from app.services import live_rooms, ytdlp
from app.util.platform import creator_id_from_url, detect_platform

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class WatchCreate(BaseModel):
    url: str
    scope: str = Field(default="both", pattern="^(lives|posts|both)$")
    live_url: str | None = None


class WatchPatch(BaseModel):
    scope: str | None = Field(default=None, pattern="^(lives|posts|both)$")
    enabled: bool | None = None
    # Renameable because a walled-off listing leaves the creator named after
    # its bare profile id (see add_watch).
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    # "" clears the room and puts the live check back on the profile probe.
    live_url: str | None = None


class WatchOut(BaseModel):
    id: int
    platform: str
    creator_id: str
    display_name: str
    scope: str
    live_url: str | None
    enabled: bool
    last_seen_post_id: str | None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


def _slug(name: str) -> str:
    s = re.sub(r"[^0-9A-Za-z_.-]+", "_", name).strip("_")
    return s or "creator"


def _clean_live_url(url: str | None, platform: str) -> str | None:
    """A room URL is polled verbatim, so it must at least belong to the same
    platform as the creator it hangs off."""
    url = (url or "").strip()
    if not url:
        return None
    if detect_platform(url) != platform:
        raise HTTPException(
            400, detail=f"Live room URL must be a {platform} URL"
        )
    return url


def _resolve_identity(info: dict, entry: dict, display: str) -> str:
    """Best creator id from a probe result. info['id'] counts only for
    playlist-shaped results (channel tabs); a single video's id would group
    the wrong things. `entry` is the newest-post probe used when the listing
    itself is anonymous (see _probe_creator)."""
    for src in (info, entry):
        cid = src.get("uploader_id") or src.get("channel_id")
        if cid:
            return str(cid)
    if info.get("_type") == "playlist" or ":tab" in str(info.get("extractor_key", "")):
        if info.get("id"):
            return str(info["id"])
    return _slug(display)


def _display_name(info: dict) -> str:
    return (
        info.get("channel") or info.get("uploader") or info.get("title") or ""
    ).strip()


def _first_entry_url(info: dict) -> str | None:
    for e in info.get("entries") or []:
        url = e.get("url") or e.get("webpage_url")
        if url:
            return str(url)
    return None


# Two different walls, two different fixes. bilibili answers space listings
# with 412 whenever the egress IP is rate-limited — signed in or out, cookies
# do not lift it (yt-dlp#12013), it just has to expire. Auth walls do want
# cookies. Either way the URL, not the listing, is what identifies a creator.
_RATE_LIMIT_RE = re.compile(
    r"\b(412|429)\b|blocked by server|rejected by server|rate.?limit|too many requests",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"\b(401|403)\b|log in|login|sign in|cookies|private|members?.only",
    re.IGNORECASE,
)


def _is_blocked(exc: Exception) -> bool:
    """A wall we can wait out or authenticate past — as opposed to a URL that
    simply does not resolve."""
    msg = str(exc)
    return bool(_RATE_LIMIT_RE.search(msg) or _AUTH_RE.search(msg))


def _probe_detail(platform: str, exc: Exception, had_cookies: bool) -> str:
    msg = (str(exc).strip().splitlines() or [""])[-1].removeprefix("ERROR: ").strip()
    msg = msg or exc.__class__.__name__
    if _RATE_LIMIT_RE.search(msg):
        return (
            f"{platform} is rate-limiting this machine ({msg}). It clears on its own — "
            "wait a few minutes and try again, or paste the creator's profile URL, "
            "which needs no listing lookup."
        )
    if _AUTH_RE.search(msg):
        fix = (
            f"the stored {platform} cookies may be expired — re-export them"
            if had_cookies
            else f"add {platform} cookies"
        )
        return (
            f"{platform} would not serve this signed out ({msg}). "
            f"Open Settings → Credentials and {fix}, then try again."
        )
    return f"Could not resolve creator from URL: {msg}"


async def _probe_creator(url: str, cookie_path: str | None) -> tuple[dict, dict]:
    """Resolve probe: newest page of the listing only, plus one full probe of
    its newest entry when the listing carries no creator name (bilibili space
    playlists are anonymous — id and nothing else). Raises the yt-dlp error."""
    info = await asyncio.to_thread(
        ytdlp.probe, url, cookie_path, extract_flat=True, playlist_items="1"
    )
    if _display_name(info):
        return info, {}
    entry_url = _first_entry_url(info)
    if not entry_url:
        return info, {}
    try:
        return info, await asyncio.to_thread(ytdlp.probe, entry_url, cookie_path)
    except Exception:
        log.warning("naming probe of %s failed; falling back to URL id", entry_url)
        return info, {}


@router.post("", response_model=WatchOut, status_code=201)
async def add_watch(
    body: WatchCreate, session: AsyncSession = Depends(get_session)
) -> CreatorWatch:
    platform = detect_platform(body.url)
    if not platform:
        raise HTTPException(400, detail="Unsupported URL")

    # Stored cookies decide what a probe can even see (private/members-only
    # profiles); the resolve probe was running signed out regardless of them.
    # Local import: credentials imports app.services, so a module-level import
    # here would be circular.
    from app.routers.credentials import aget_cookiefile

    cookiefile = await aget_cookiefile(platform)
    url_id = creator_id_from_url(body.url)
    try:
        info, entry = await _probe_creator(
            body.url, str(cookiefile) if cookiefile else None
        )
    except Exception as exc:
        # bilibili answers space listings with 412 whenever the egress IP is
        # rate-limited (minutes to hours, cookies do not lift it). A profile
        # URL already names the creator to poll, so record the watch and let
        # the sweep retry rather than making the user wait out the block.
        if not (url_id and _is_blocked(exc)):
            raise HTTPException(
                400, detail=_probe_detail(platform, exc, bool(cookiefile))
            ) from exc
        log.warning("%s listing blocked; watching %s from its URL: %s", platform, url_id, exc)
        info, entry = {}, {}
    finally:
        if cookiefile:
            cookiefile.unlink(missing_ok=True)

    display = _display_name(info) or _display_name(entry) or url_id or ""
    if not display:
        raise HTTPException(400, detail="Probe returned no creator identity")

    # A profile URL states the id the poller must poll back; trust it over the
    # probe, whose uploader_id can be a different key space (numeric vs handle).
    creator_id = url_id or _resolve_identity(info, entry, display)

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

    live_url = _clean_live_url(body.live_url, platform)
    if not live_url:
        # bilibili hides the live room behind a second id no probe reports;
        # fill it once here so the watch has somewhere to point its live
        # check. Best-effort by design — see services/live_rooms.
        live_url = await live_rooms.resolve_room_url(platform, creator_id)

    watch = CreatorWatch(
        platform=platform,
        creator_id=creator_id,
        display_name=display,
        scope=body.scope,
        live_url=live_url,
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
    patch = body.model_dump(exclude_unset=True)
    if "live_url" in patch:
        patch["live_url"] = _clean_live_url(patch["live_url"], watch.platform)
    if "display_name" in patch:
        patch["display_name"] = str(patch["display_name"]).strip()
        if not patch["display_name"]:
            raise HTTPException(400, detail="display_name cannot be blank")
    for k, v in patch.items():
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
