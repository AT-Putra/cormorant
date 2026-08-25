"""WebSocket event stream (US-011): /api/ws forwards the in-process pub/sub.

AuthMiddleware is BaseHTTPMiddleware — it never sees WebSocket connects, so
the cookie check is done explicitly here (same token-hash lookup as HTTP).
"""

import asyncio
import json
import logging
from collections import deque

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.auth import SESSION_COOKIE, _live_session_token
from app.services import events

log = logging.getLogger(__name__)

router = APIRouter(tags=["ws"])

# Poll cadence for the push-only stream. Events come from other threads
# (worker threads publish), so a plain asyncio.Queue wakeup isn't reliable;
# a bounded deque + short poll is simple and starvation-free.
POLL_S = 0.1
# How often an open socket re-validates its session cookie, so password
# rotation / logout actually signs live sockets out.
REAUTH_S = 30


def _db():
    """Module-reload-safe DB handle (same pattern as services/downloader.py)."""
    import app.db

    return app.db.async_session()


@router.websocket("/api/ws")
async def ws_events(ws: WebSocket) -> None:
    token = ws.cookies.get(SESSION_COOKIE)
    async with _db() as session:
        live = await _live_session_token(session, token)
    if live is None:
        await ws.close(code=4401)  # 4000-range: app-specific "unauthenticated"
        return

    buf: deque[dict] = deque(maxlen=256)

    def _forward(event: dict) -> None:
        buf.append(event)  # GIL-atomic; maxlen silently drops oldest

    events.subscribe(_forward)
    await ws.accept()
    try:
        last_auth_check = asyncio.get_event_loop().time()
        while True:
            now = asyncio.get_event_loop().time()
            if now - last_auth_check >= REAUTH_S:
                last_auth_check = now
                async with _db() as session:
                    live = await _live_session_token(session, token)
                if live is None:
                    await ws.close(code=4401)  # signed out / rotated mid-stream
                    break
            try:
                event = buf.popleft()
            except IndexError:
                recv = asyncio.create_task(ws.receive_text())
                await asyncio.wait({recv}, timeout=POLL_S)
                if recv.done():
                    break  # any client frame or disconnect ends it (push-only)
                recv.cancel()
                continue
            await ws.send_text(json.dumps(event))
            # ponytail: no ping/pong keepalive yet; add if a proxy idles
            # sockets out before the frontend's reconnect kicks in.
    except (WebSocketDisconnect, RuntimeError):
        pass  # client left, or socket torn down mid-send
    finally:
        events.unsubscribe(_forward)
