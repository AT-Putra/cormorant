"""Live-room lookup for platforms that hide the room behind a second id.

The one place the backend talks to a platform API directly, and a deliberate
exception to Principle 1 (poller.py: probes only, no hand-rolled clients).
The reason: bilibili keeps live rooms in their own id space —
live.bilibili.com/5265 belongs to space.bilibili.com/4549624 — and nothing
yt-dlp extracts from a space page names the room, so a watch could not point
its live check anywhere without the user looking the number up by hand.

Scope is deliberately narrow, and stays that way:
  * add-time convenience only. The answer is stored on the watch, so polling,
    recording and every other path keep running on yt-dlp alone.
  * every failure is None. A dead or changed endpoint costs auto-fill, never
    an add — the field stays editable by hand.
"""

import logging

import httpx

log = logging.getLogger(__name__)

_TIMEOUT = 8.0
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


async def _bilibili_room(mid: str) -> str | None:
    """Room URL for a bilibili uid, or None when they have never opened one.

    Unauthenticated and unsigned: this endpoint sits on api.live.bilibili.com,
    which does not share the WBI signing or the rate-limit wall that makes
    space listings answer 412.
    """
    if not mid.isdigit():
        return None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(
            "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld",
            params={"mid": mid},
            headers={"User-Agent": _UA, "Referer": f"https://space.bilibili.com/{mid}"},
        )
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        log.info("bilibili room lookup for %s answered code %s", mid, body.get("code"))
        return None
    data = body.get("data") or {}
    if not data.get("roomStatus"):  # 0 == this creator has no live room
        return None
    url = data.get("url") or (
        f"https://live.bilibili.com/{data['roomid']}" if data.get("roomid") else None
    )
    return str(url).split("?")[0] if url else None


_RESOLVERS = {"bilibili": _bilibili_room}


async def resolve_room_url(platform: str, creator_id: str) -> str | None:
    """Best-effort room URL for a creator. Never raises."""
    resolver = _RESOLVERS.get(platform)
    if not resolver:
        return None
    try:
        return await resolver(str(creator_id))
    except Exception as exc:
        log.warning("%s room lookup for %s failed: %s", platform, creator_id, exc)
        return None
