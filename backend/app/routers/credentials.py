"""Per-platform cookie credential storage: paste or cookies.txt upload.

Validation runs a structural check on what was pasted, then a threaded
yt-dlp probe for the platforms that have a usable target, BEFORE storing.
Blobs are Fernet-encrypted at rest; decrypted only in-process for engine
calls (get_cookiefile), never returned by any API.
"""

import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crypto
from app.db import get_session
from app.models import PlatformCredential, utcnow
from app.services import ytdlp

router = APIRouter(prefix="/api/credentials", tags=["credentials"])

PLATFORMS = {"bilibili", "instagram", "tiktok", "douyin", "xhs"}

# Cheap authenticated probe target per platform (popular public content;
# auth-required errors are the signal we care about, not the content itself)
_PROBE_URLS = {
    # BV1xx411c7mD = bilibili video av2, uploaded 2009, the site's oldest
    # surviving upload. Chosen over a trending video because probe targets
    # only break when they are deleted.
    "bilibili": "https://www.bilibili.com/video/BV1xx411c7mD",
    "tiktok": "https://www.tiktok.com/@tiktok",
    # instagram, douyin and xhs are deliberately absent.
    #
    # instagram: a profile URL routes to yt-dlp's instagram:user extractor,
    # which ships with _WORKING = False. It still scrapes the `sharedData`
    # blob Instagram dropped from its HTML years ago, so every probe died on
    # "Unable to extract data" and no cookie could ever have passed. The
    # alternatives are no better: a post URL breaks the day that post is
    # deleted, and /stories/<user>/ answers "You need to log in" whenever the
    # account simply has no story up right now, which would reject good
    # cookies. instagram gets _REQUIRED_COOKIES below instead.
    #
    # douyin and xhs: yt-dlp matches only individual posts on them —
    # douyin.com/video/<id> and xiaohongshu.com/explore/<id>, with no profile
    # pattern at all — so the durable public target this table relies on does
    # not exist. Pinning one post would work until the day it is deleted, at
    # which point saving cookies breaks with no way to tell why.
}

# Cookie domain each platform's credentials must actually come from. Suffix
# matched, so www.bilibili.com and .bilibili.com both count.
_COOKIE_DOMAINS = {
    "bilibili": "bilibili.com",
    "instagram": "instagram.com",
    "tiktok": "tiktok.com",
    "douyin": "douyin.com",
    "xhs": "xiaohongshu.com",
}

# Cookie that has to be present for an export to be a logged-in session, for
# the platforms where yt-dlp names one. instagram's extractors define
# _AUTH_COOKIE_NAME = "sessionid" and read its presence as _is_logged_in, so
# an export without it authenticates nothing however many other cookies rode
# along. With no probe target left, this is instagram's only real check.
_REQUIRED_COOKIES = {
    "instagram": "sessionid",
}


def _cookie_entries(cookie_text: str) -> list[tuple[str, str]]:
    """(domain, name) for every cookie in a Netscape cookie file.

    "#HttpOnly_" is a domain-field prefix some exporters emit, not a comment,
    so it is stripped rather than skipped along with the real comments.
    """
    out: list[tuple[str, str]] = []
    for raw in cookie_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_") :]
        elif line.startswith("#"):
            continue
        parts = line.split("	")
        if len(parts) >= 7 and parts[0]:
            out.append((parts[0].lstrip(".").lower(), parts[5]))
    return out


def _check_cookie_shape(platform: str, cookie_text: str) -> None:
    """Reject a paste that cannot be this platform's logged-in cookies.

    Runs for every platform, before any network call. The probe targets are
    public content, so a probe passing has never proved the cookies
    authenticate — it only proves the site answered. This does check
    something real about what was pasted, and it is the whole of validation
    for instagram, douyin and xhs (see _PROBE_URLS).
    """
    entries = _cookie_entries(cookie_text)
    if not entries:
        raise HTTPException(
            status_code=400,
            detail=(
                "No cookies found. Paste a cookies.txt export in Netscape "
                "format — one cookie per line, fields separated by tabs."
            ),
        )
    want = _COOKIE_DOMAINS[platform]
    on_platform = [
        name for domain, name in entries
        if domain == want or domain.endswith("." + want)
    ]
    if not on_platform:
        seen = ", ".join(sorted({d for d, _ in entries})[:3])
        raise HTTPException(
            status_code=400,
            detail=f"These cookies are for {seen}; {platform} needs {want}.",
        )
    required = _REQUIRED_COOKIES.get(platform)
    if required and required not in on_platform:
        raise HTTPException(
            status_code=400,
            detail=(
                f"These {want} cookies have no {required}, so they are a "
                f"logged-out session. Log in to {want} in the browser you "
                "export from, then export again."
            ),
        )


class CookieTextIn(BaseModel):
    cookie_text: str


class CredentialOut(BaseModel):
    platform: str
    validated_at: str | None
    updated_at: str | None

    model_config = {"from_attributes": True}


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    markers = (
        "login", "log in", "sign in", "cookie", "cookies",
        "auth", "account", "members-only", "private", "rate limit is exceeded",
        "requested content is not available", "nsig", "visitor",
    )
    return any(m in msg for m in markers)


async def _validate_cookie_text(platform: str, cookie_text: str) -> None:
    """Structural check, then a yt-dlp probe where one is possible.

    Raises HTTPException on failure. Both early exits happen before the
    temp file exists: it holds the credentials in plaintext, so it is only
    written when something is actually going to read it, and always
    removed afterwards.
    """
    import asyncio

    _check_cookie_shape(platform, cookie_text)
    probe_url = _PROBE_URLS.get(platform)
    if probe_url is None:
        return  # structural check is all this platform can have

    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", prefix=f"vd_cookies_{platform}_", delete=False, encoding="utf-8"
    )
    tmp.write(cookie_text if cookie_text.endswith("\n") else cookie_text + "\n")
    tmp.close()

    try:
        try:
            # Flat + capped, never a full extraction. Several probe targets
            # are profiles, and extracting one meant pulling every video on
            # them: TikTok's @tiktok took 61s and then died on whichever clip
            # happened to sit at the top of the feed ("Unexpected response
            # from webpage request"), failing a cookie save for a reason that
            # had nothing to do with the cookies. Flat alone still paged 1454
            # entries in 58s, so the item cap is load-bearing, not a nicety.
            await asyncio.to_thread(
                ytdlp.probe,
                probe_url,
                tmp.name,
                extract_flat=True,
                playlist_items="1-3",
            )
        except Exception as exc:
            if _is_auth_error(exc):
                raise HTTPException(status_code=400, detail=f"Cookie validation failed: {exc}")
            raise HTTPException(status_code=502, detail=f"Probe network error: {exc}")
    finally:
        Path(tmp.name).unlink(missing_ok=True)


@router.post("/{platform}")
async def save_credential(
    platform: str,
    request: Request,
    cookies_file: UploadFile | None = File(None),
    session: AsyncSession = Depends(get_session),
):
    if platform not in PLATFORMS:
        raise HTTPException(status_code=404, detail=f"Unknown platform: {platform}")

    if cookies_file is not None and cookies_file.filename:
        raw = await cookies_file.read()
        text = raw.decode("utf-8", errors="replace")
    else:
        # Manual JSON parse — avoids FastAPI Body/File content-type conflicts
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if isinstance(payload, dict) and payload.get("cookie_text"):
            text = payload["cookie_text"]
        else:
            raise HTTPException(status_code=422, detail="Provide cookie_text JSON body or cookies_file upload")

    # Netscape header line optional but tolerated by yt-dlp either way
    await _validate_cookie_text(platform, text)

    blob = crypto.encrypt_cookie_text(text)
    row = (
        await session.execute(
            select(PlatformCredential).where(PlatformCredential.platform == platform)
        )
    ).scalar_one_or_none()
    if row is None:
        row = PlatformCredential(platform=platform, encrypted_blob=blob)
        session.add(row)
    else:
        row.encrypted_blob = blob
        row.updated_at = utcnow()
    row.validated_at = utcnow()
    await session.commit()
    return {"platform": platform, "validated": True}


# crypto.CONFIG_DIR is read lazily via cfg_mod; tests monkeypatch app.crypto.CONFIG_DIR


@router.get("")
async def list_credentials(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(select(PlatformCredential).order_by(PlatformCredential.platform))
    ).scalars().all()
    return [
        {
            "platform": r.platform,
            "validated_at": r.validated_at.isoformat() if r.validated_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in rows
    ]


@router.delete("/{platform}")
async def delete_credential(platform: str, session: AsyncSession = Depends(get_session)):
    row = (
        await session.execute(
            select(PlatformCredential).where(PlatformCredential.platform == platform)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="No credential stored")
    await session.delete(row)
    await session.commit()
    return {"deleted": platform}


def get_cookiefile(platform: str) -> Path | None:
    """Decrypt stored credential to a temp file for yt-dlp cookiefile usage (sync contexts).

    Returns None when no credential exists. Caller owns cleanup of the path.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        raise RuntimeError("Use aget_cookiefile() in async contexts")

    from app.db import async_session

    async def _load() -> str | None:
        async with async_session() as s:
            row = (
                await s.execute(
                    select(PlatformCredential).where(PlatformCredential.platform == platform)
                )
            ).scalar_one_or_none()
            return row.encrypted_blob if row else None

    blob = asyncio.run(_load())
    if blob is None:
        return None
    text = crypto.decrypt_cookie_blob(blob)
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", prefix=f"vd_auth_{platform}_", delete=False, encoding="utf-8"
    )
    tmp.write(text if text.endswith("\n") else text + "\n")
    tmp.close()
    return Path(tmp.name)


async def aget_cookiefile(platform: str) -> Path | None:
    """Async variant: decrypt blob into a NamedTemporaryFile for engine calls."""
    from app.db import async_session

    async with async_session() as s:
        row = (
            await s.execute(
                select(PlatformCredential).where(PlatformCredential.platform == platform)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        text = crypto.decrypt_cookie_blob(row.encrypted_blob)
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", prefix=f"vd_auth_{platform}_", delete=False, encoding="utf-8"
    )
    tmp.write(text if text.endswith("\n") else text + "\n")
    tmp.close()
    return Path(tmp.name)
