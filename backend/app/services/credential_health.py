"""Is the stored TikTok cookie jar still worth having?

A capture that quietly falls back to anonymous is not a visible failure: the
recording still happens, at whatever quality an anonymous client is offered,
and nothing in the UI says the login stopped working. This module makes that
state loud enough to act on, because the fix -- re-export the cookies -- takes
a minute and only the user can do it.

Three ways a jar stops working, and they need three different detectors:

- **missing**  no credential row at all, or one carrying no session cookie.
  Read straight off the database; no network.
- **expired**  the session cookie's own expiry has passed (or is about to).
  Netscape cookies.txt states it in field 5; again no network.
- **rejected** present, unexpired, and the site still serves a logged-out
  page. Only the site can tell us this, so it is observed rather than
  computed: the tiktok:live plugin already fetches /@handle/live for the HEVC
  ladder, and that page carries `webapp.app-context.user` when the session is
  good and omits the key entirely when it is not. Measured 2026-08-30 on one
  room within the same minute: anonymous 241,224 B with no `user` key, the
  same fetch with cookies 247,079 B carrying uid and nickName. The signal is
  free -- no request exists for the sake of this check.

Bilibili needs a fourth detector, because its **rejected** state is the
common one and nothing observes it for free. SESSDATA is rotated whenever
bilibili refreshes the session, which revokes every copy exported earlier
while the expiry in the file still reads years away -- so the offline check
says "ok" about a jar the site no longer honours. And yt-dlp treats the mere
presence of SESSDATA as logged in, so the capture does not fall back to
anonymous, it runs at whatever tier a logged-out client is offered: a 年度大会员
credential the save-time check had passed eight days earlier recorded at 超清
(qn 250) while nav answered 账号未登录 (measured 2026-09-19, prod). The only
way to know is to ask: one GET of the nav endpoint with the jar, the same
question the credentials router asks at save time. That is a probe, not an
observation, so sweep() makes it itself -- only for a jar the offline check
passed, only for platforms named in PROBES.

Two structural constraints shape the rest:

The plugin runs inside `asyncio.to_thread`, and services/activity's mirror
drops any event published off the loop (it needs a running loop to schedule
the write). So the plugin cannot publish; it calls note_session(), which only
records, and the poller drains that on the loop in sweep(). The plugin also
runs in the capture subprocess, where an event would reach nothing at all --
that observation is simply lost, and the next poll sweep of a live room makes
it again.

Reporting is transition-only. Four idle creators on a 300s sweep is 288
chances a day to say the same thing, which is exactly how watch.poll_error
buried the activity feed; see poller._OFFLINE_RE for that lesson. One event
when the jar goes bad, one when it comes back, silence in between.
"""

import asyncio
import logging
import threading
import time

from app.services import events

log = logging.getLogger(__name__)

# Adding a platform means naming its session cookies here; the offline half of
# the module is platform-agnostic. A platform whose site says nothing about the
# session for free also wants an entry in PROBES below.
SESSION_COOKIES = {
    "tiktok": ("sessionid", "sessionid_ss", "sid_tt", "sid_guard"),
    "bilibili": ("SESSDATA",),
}

# How long before expiry to start warning. A week is enough notice to re-export
# without racing a live stream.
EXPIRY_WARNING_S = 7 * 24 * 3600

_NAMES = {"tiktok": "TikTok", "bilibili": "bilibili"}

_ADVICE = {
    "missing": "no {name} session cookie is stored -- captures are anonymous",
    "expired": "the stored {name} session cookie has expired",
    "rejected": "{name} says the stored session is logged out -- re-export the cookies",
    "expiring": "the stored {name} session cookie expires soon",
}


def _advice(state: str, platform: str) -> str:
    return _ADVICE[state].format(name=_NAMES.get(platform, platform))


# ---- active probes -----------------------------------------------------------

BILIBILI_NAV = "https://api.bilibili.com/x/web-interface/nav"


def bilibili_nav(cookiefile: str) -> dict:
    """The nav payload for a jar, through yt-dlp's own networking so the
    answer describes the session the extractor will present. Raises on a
    network failure; the caller decides what silence means."""
    from app.services import ytdlp

    return ytdlp.fetch_json(
        BILIBILI_NAV, cookiefile, headers={"Referer": "https://www.bilibili.com/"}
    )


def _bilibili_logged_in(cookiefile: str) -> bool | None:
    """Whether bilibili still honours the jar. None is "no verdict" -- a
    network error or an answer without isLogin -- and is never read as
    logged out, since a blip must not announce a dead credential."""
    try:
        payload = bilibili_nav(cookiefile)
    except Exception as exc:
        log.warning("bilibili session probe failed: %s", exc)
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or "isLogin" not in data:
        return None
    return bool(data["isLogin"])


# platform -> sync callable(cookiefile path) -> logged_in | None. Run off the
# loop by sweep(); its answer outranks a recorded observation, being newer.
PROBES = {"bilibili": _bilibili_logged_in}


async def _probe(platform: str) -> bool | None:
    probe = PROBES.get(platform)
    if probe is None:
        return None
    # Lazy: the router imports this package, so a top-level import is a cycle.
    from app.routers.credentials import aget_cookiefile

    cookiefile = await aget_cookiefile(platform)
    if cookiefile is None:
        return None
    try:
        return await asyncio.to_thread(probe, str(cookiefile))
    finally:
        cookiefile.unlink(missing_ok=True)

_lock = threading.Lock()
_observed: dict[str, bool] = {}   # platform -> logged_in, written off-loop
_reported: dict[str, str] = {}    # platform -> last state published


def note_session(platform: str, logged_in: bool) -> None:
    """Record what a fetched page said about the session. Thread-safe, silent.

    Called from inside the extractor, which may be on a worker thread or in the
    capture subprocess -- neither can publish, so this only stores.
    """
    with _lock:
        _observed[platform] = logged_in


def _take_observation(platform: str) -> bool | None:
    """Consume the last observation, so a stale one cannot outlive its fetch."""
    with _lock:
        return _observed.pop(platform, None)


def parse_expiry(text: str, platform: str) -> float | None:
    """Latest expiry among the platform's session cookies, or None when the jar
    carries none of them.

    A 0 in field 5 marks a browser-session cookie with no stated end; skipped
    rather than read as expired, since such an export can still work.
    """
    names = SESSION_COOKIES.get(platform) or ()
    latest: float | None = None
    for line in (text or "").splitlines():
        line = line.strip()
        # "#HttpOnly_" prefixes a real data line, not a comment -- and TikTok's
        # session cookies are exactly the httpOnly ones.
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue
        parts = line.split("\t")
        if len(parts) < 7 or parts[5] not in names:
            continue
        try:
            expiry = float(parts[4])
        except ValueError:
            continue
        if expiry <= 0:
            continue
        latest = expiry if latest is None else max(latest, expiry)
    return latest


def inspect(
    text: str | None, platform: str = "tiktok", now: float | None = None
) -> tuple[str, str]:
    """Offline verdict on a jar's text: (state, human detail)."""
    now = time.time() if now is None else now
    if not text or not text.strip():
        return "missing", _advice("missing", platform)
    expiry = parse_expiry(text, platform)
    if expiry is None:
        return "missing", _advice("missing", platform)
    left = expiry - now
    if left <= 0:
        return "expired", f"{_advice('expired', platform)} ({int(-left // 86400)}d ago)"
    if left <= EXPIRY_WARNING_S:
        return "expiring", f"{_advice('expiring', platform)} (in {int(left // 3600)}h)"
    return "ok", ""


def combine(
    offline: tuple[str, str], logged_in: bool | None, platform: str = "tiktok"
) -> tuple[str, str]:
    """Fold an observed session state into the offline verdict.

    Only "ok" can become "rejected": when the jar is already missing or expired
    the user has a better thing to be told, and a logged-out page is the
    expected consequence rather than news of its own.
    """
    state, detail = offline
    if logged_in is False and state == "ok":
        return "rejected", _advice("rejected", platform)
    if logged_in is True and state == "rejected":
        return "ok", ""
    return state, detail


def report(platform: str, state: str, detail: str) -> bool:
    """Publish only when the state actually changed. True if something went out.

    The memory is per-process, so a restart re-announces a still-broken jar
    once. That is the right side to err on: a warning repeated after a restart
    is noise the user can act on, one never repeated is a warning they can miss.
    """
    previous = _reported.get(platform)
    if previous == state:
        return False
    _reported[platform] = state
    if state == "ok":
        if previous is None:
            return False  # healthy at boot is not news
        events.publish({"type": "credentials.ok", "platform": platform})
        return True
    events.publish(
        {
            "type": "credentials.stale",
            "platform": platform,
            "state": state,
            "detail": detail,
        }
    )
    return True


async def _jar_text(platform: str) -> str | None:
    """The decrypted jar, without materialising the temp file aget_cookiefile
    would write -- nothing here hands it to an engine."""
    from sqlalchemy import select

    from app import crypto, models
    from app.db import async_session

    async with async_session() as s:
        row = (
            await s.execute(
                select(models.PlatformCredential).where(
                    models.PlatformCredential.platform == platform
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return None
    try:
        return crypto.decrypt_cookie_blob(row.encrypted_blob)
    except Exception:
        log.exception("could not decrypt the %s cookie blob", platform)
        return None


async def sweep(platform: str = "tiktok") -> str:
    """One health check: offline verdict, folded with what the site said --
    an observation a fetch left behind, or the probe's answer when the
    platform has one and the jar is worth the round trip."""
    offline = inspect(await _jar_text(platform), platform)
    observed = _take_observation(platform)
    if offline[0] == "ok":
        # A missing or expired jar already has its verdict; asking the site
        # about it would only spend a request to be told the same thing.
        probed = await _probe(platform)
        if probed is not None:
            observed = probed
    state, detail = combine(offline, observed, platform)
    report(platform, state, detail)
    return state


def reset() -> None:
    """Forget both memories (tests)."""
    with _lock:
        _observed.clear()
    _reported.clear()
