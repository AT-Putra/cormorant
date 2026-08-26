"""Per-platform cookie credential storage: paste or cookies.txt upload.

Validation runs a structural check on what was pasted, then — BEFORE storing
— either a real session check against the platform's own auth endpoint
(_AUTH_CHECKS, which also names the account) or, failing that, a threaded
yt-dlp probe for the platforms that have a usable target.
Blobs are Fernet-encrypted at rest; decrypted only in-process for engine
calls (get_cookiefile), never returned by any API.
"""

import logging
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

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/credentials", tags=["credentials"])

PLATFORMS = {"bilibili", "instagram", "tiktok", "douyin", "xhs"}

# Both prefixes name a file holding DECRYPTED cookies. Kept as constants
# because sweep_stale_cookiefiles() has to match every one of them: a prefix
# that drifts out of that tuple is plaintext credentials nothing will collect.
_ENGINE_PREFIX = "vd_auth_"      # handed to an engine call
_VALIDATE_PREFIX = "vd_cookies_"  # handed to the save-time validator
_COOKIEFILE_PREFIXES = (_ENGINE_PREFIX, _VALIDATE_PREFIX)

# Cheap authenticated probe target per platform (popular public content;
# auth-required errors are the signal we care about, not the content itself)
_PROBE_URLS = {
    "tiktok": "https://www.tiktok.com/@tiktok",
    # bilibili, instagram, douyin and xhs are deliberately absent.
    #
    # bilibili: it has something strictly better — see _AUTH_CHECKS. Its old
    # target (BV1xx411c7mD, av2, the site's oldest surviving upload) was
    # public content, so a logged-out session probed it perfectly happily and
    # got stored as valid. That is not a hypothetical: a revoked SESSDATA sat
    # in this table reporting "validated" while every bilibili download
    # quietly lost its top two quality tiers.
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
    account_label: str | None = None

    model_config = {"from_attributes": True}


def _is_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    markers = (
        "login", "log in", "sign in", "cookie", "cookies",
        "auth", "account", "members-only", "private", "rate limit is exceeded",
        "requested content is not available", "nsig", "visitor",
    )
    return any(m in msg for m in markers)


# bilibili's own "who am I" endpoint. Answers the question a probe of public
# content structurally cannot: not "did the site respond" but "is this session
# logged in, and as whom".
_BILIBILI_NAV = "https://api.bilibili.com/x/web-interface/nav"


def _check_bilibili_auth(cookiefile: str) -> str:
    """Verify a bilibili session server-side; return a label for the account.

    Why this exists rather than a probe: yt-dlp's bilibili extractor treats
    the mere PRESENCE of a SESSDATA cookie as being logged in (its
    `is_logged_in` property), and on that basis trusts the quality ladder
    embedded in the watch page instead of calling the playurl API at all.
    When SESSDATA is present but revoked, the page it reads was served
    logged-out — so the ladder is the stunted one, and the API call that
    would have returned the full set never happens.

    Measured on a 4K video with a revoked cookie: 480p ceiling, versus 1080p
    with no cookie whatsoever. A dead credential is not merely useless here,
    it is worse than none, and nothing downstream says a word about it. So it
    has to be caught at save time, which is the only moment a human is
    watching.
    """
    try:
        payload = ytdlp.fetch_json(
            _BILIBILI_NAV, cookiefile, headers={"Referer": "https://www.bilibili.com/"}
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Probe network error: {exc}") from exc

    data = payload.get("data") or {}
    if not data.get("isLogin"):
        # Logged out answers code -101 / 账号未登录. Carry bilibili's own
        # message through rather than flattening an unexpected code into a
        # guess about what went wrong.
        reason = payload.get("message") or f"code {payload.get('code')}"
        raise HTTPException(
            status_code=400,
            detail=(
                f"bilibili says this session is not logged in ({reason}). "
                "SESSDATA is rotated whenever bilibili refreshes it, which "
                "revokes any copy exported earlier even though its expiry "
                "date still looks years away. Log in at bilibili.com and "
                "export again."
            ),
        )
    name = data.get("uname") or str(data.get("mid") or "?")
    # Labels, never rejects: an account without 大会员 is a perfectly good
    # credential — it still unlocks 1080p and members-only posts. Recording
    # the tier is what lets the UI answer "why is there still no 4K", which
    # a bare "validated" cannot.
    if data.get("vipStatus") == 1:
        tier = (data.get("vip_label") or {}).get("text") or "premium"
    else:
        tier = "no premium"
    return f"{name} · {tier}"


# Platforms with a real authentication check. Where one exists it replaces the
# probe entirely: it is faster (one JSON GET, no extraction) and it tests the
# thing that actually matters.
_AUTH_CHECKS = {
    "bilibili": _check_bilibili_auth,
}


async def _validate_cookie_text(platform: str, cookie_text: str) -> str | None:
    """Structural check, then a real auth check or a yt-dlp probe.

    Returns a human-readable account label when the platform can identify the
    session, else None. Raises HTTPException on failure. The early exit
    happens before the temp file exists: it holds the credentials in
    plaintext, so it is only written when something is actually going to read
    it, and always removed afterwards.
    """
    import asyncio

    _check_cookie_shape(platform, cookie_text)
    auth_check = _AUTH_CHECKS.get(platform)
    probe_url = _PROBE_URLS.get(platform)
    if auth_check is None and probe_url is None:
        return None  # structural check is all this platform can have

    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", prefix=f"{_VALIDATE_PREFIX}{platform}_", delete=False, encoding="utf-8"
    )
    tmp.write(cookie_text if cookie_text.endswith("\n") else cookie_text + "\n")
    tmp.close()

    try:
        if auth_check is not None:
            return await asyncio.to_thread(auth_check, tmp.name)
        if probe_url is not None:
            # Flat + capped, never a full extraction. Several probe targets
            # are profiles, and extracting one meant pulling every video on
            # them: TikTok's @tiktok took 61s and then died on whichever clip
            # happened to sit at the top of the feed ("Unexpected response
            # from webpage request"), failing a cookie save for a reason that
            # had nothing to do with the cookies. Flat alone still paged 1454
            # entries in 58s, so the item cap is load-bearing, not a nicety.
            try:
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
        return None
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
    label = await _validate_cookie_text(platform, text)

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
    row.account_label = label
    await session.commit()
    return {"platform": platform, "validated": True, "account": label}


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
            "account_label": r.account_label,
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


def sweep_stale_cookiefiles() -> int:
    """Delete decrypted cookie files stranded by a previous process. Boot only.

    Every caller unlinks its own file in a `finally`, which covers errors and
    cancellation but not death: SIGKILL, OOM, `podman restart`. A live capture
    holds its file for the whole chain — hours, by design — so a restart
    mid-recording leaves plaintext credentials in /tmp with nobody left who
    knows the path.

    Safe to delete everything matching at BOOT specifically, and only there:
    the process tree that owned those files died with the previous container,
    so none of them can still be in use. Calling this from the periodic sweep
    instead would pull the file out from under a running capture.
    """
    tmp_root = Path(tempfile.gettempdir())
    removed = 0
    for prefix in _COOKIEFILE_PREFIXES:
        for stale in tmp_root.glob(f"{prefix}*.txt"):
            try:
                stale.unlink()
                removed += 1
            except OSError:  # pragma: no cover - racing another unlink
                pass
    if removed:
        log.warning(
            "removed %d cookie file(s) stranded by a previous run; a capture or "
            "download was killed mid-flight", removed
        )
    return removed


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
        "w", suffix=".txt", prefix=f"{_ENGINE_PREFIX}{platform}_", delete=False, encoding="utf-8"
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
        "w", suffix=".txt", prefix=f"{_ENGINE_PREFIX}{platform}_", delete=False, encoding="utf-8"
    )
    tmp.write(text if text.endswith("\n") else text + "\n")
    tmp.close()
    return Path(tmp.name)
