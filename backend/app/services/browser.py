"""Headless Chrome, for the one page yt-dlp cannot read.

TikTok serves www.tiktok.com HTML behind a JS WAF challenge (SlardarWAF: 200
OK, ~1.1 KB, `_wafchallengeid`, "Please wait..."). A browser clears it by
running the script; yt-dlp cannot, and no amount of header, cookie or
curl_cffi work gets past it -- measured from the deploy host, every
impersonation target it offers plus a valid cookie jar, all challenged.

That page is the ONLY place the HEVC ladder lives. webcast/room/info carries
the H.264 ladder and nothing else (measured: zero `hevc` mentions, and codec
capability params change the response by 8 bytes of timestamp); room/enter
answers 403 without request signing. So 1080p60 is reachable only through a
browser, and this module is that browser.

Deliberately NOT on the polling path. Liveness and room ids come from
api-live/user/room, which is plain JSON and not challenged, so the poller
never pays for this. Only a starting capture does -- one Chrome run per
recording rather than one per creator per sweep -- which is why the lane is
opt-in through VD_BROWSER_HEVC rather than on by default.

Cookies go in over CDP rather than through a seeded profile: TikTok's session
cookies are httpOnly, so `document.cookie` cannot set them, and Chrome's own
cookie store is encrypted at rest. Network.setCookies takes them directly.

Every failure here returns None. The caller's fallback is the ordinary yt-dlp
fetch, which still yields the H.264 ladder -- a capture at 720p, never no
capture at all.
"""

import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from http.cookiejar import MozillaCookieJar
from pathlib import Path

log = logging.getLogger(__name__)

# "1" turns the lane on. The recorder sets it for the capture subprocess only.
ENABLE_ENV = "VD_BROWSER_HEVC"

# google-chrome is what the image installs; the others let a developer run the
# backend on a laptop that already has a browser under a different name.
_BINARIES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")

# The challenge clears in a couple of seconds when it clears at all; the rest
# of the budget is TikTok's own render time on a slow room.
DEFAULT_TIMEOUT = 45.0

# What the challenge stub contains and a real page never does.
_CHALLENGE_MARKER = "_wafchallengeid"

# The blob this whole module exists to reach.
_LADDER_MARKER = "hevcStreamData"

# How long to keep looking for the ladder after the page is otherwise real.
# Not optional: the first paint of a live room routinely carries roomId while
# the ladder is still rendering, so returning on "looks real" brought back a
# room WITHOUT its ladder that the very same page served a second later. Rooms
# that genuinely publish no HEVC must not hang for it, hence a short bound.
_LADDER_GRACE = 10.0

# How often the DOM is re-read while waiting. Must stay well under the grace
# above, or the grace expires after a single look and buys nothing.
_POLL_INTERVAL = 0.5


def enabled() -> bool:
    """Whether the browser lane is switched on for this process."""
    return os.environ.get(ENABLE_ENV) == "1"


def chrome_path() -> str | None:
    """Path to a usable browser binary, or None when the image has none."""
    for name in _BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


def cdp_cookies(cookiefile: str | Path | None) -> list[dict]:
    """Netscape cookies.txt -> Network.setCookies payload, [] for anything odd.

    httpOnly is not represented in the Netscape format and is not reconstructed
    here: the flag only governs whether scripts can read a cookie, never
    whether it is sent, so a cookie set without it still authenticates the
    request. That is the whole reason these are being injected.
    """
    if not cookiefile:
        return []
    jar = MozillaCookieJar()
    try:
        # Stored jars routinely carry session cookies and stale expiries; both
        # still authenticate, so neither is a reason to drop one.
        jar.load(str(cookiefile), ignore_discard=True, ignore_expires=True)
    except Exception as exc:
        log.warning("cookie jar %s is unreadable: %s", cookiefile, exc)
        return []

    out = []
    for c in jar:
        item = {
            "name": c.name,
            "value": c.value or "",
            "domain": c.domain,
            "path": c.path or "/",
            "secure": bool(c.secure),
        }
        if c.expires:
            item["expires"] = float(c.expires)
        out.append(item)
    return out


def _free_port() -> int:
    """A port the debugging server can have. Racy in principle; the window is
    one process spawn wide and the alternative is parsing Chrome's stderr."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _page_target(port: int, deadline: float) -> str | None:
    """The websocket URL of Chrome's first page target, once it has one."""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=2
            ) as r:
                for t in json.loads(r.read().decode("utf-8")):
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
        except Exception:
            pass  # not up yet
        time.sleep(0.25)
    return None


async def _drive(ws_url: str, url: str, cookies: list[dict], deadline: float) -> str | None:
    """Set cookies, navigate, and hand back the DOM once it is the real page."""
    import websockets

    async with websockets.connect(ws_url, max_size=None) as ws:
        counter = 0

        async def cmd(method: str, params: dict | None = None) -> dict:
            nonlocal counter
            counter += 1
            mine = counter
            await ws.send(json.dumps({"id": mine, "method": method,
                                      "params": params or {}}))
            while True:
                # CDP interleaves events with replies; skip anything not ours.
                msg = json.loads(await ws.recv())
                if msg.get("id") == mine:
                    return msg

        await cmd("Network.enable")
        if cookies:
            await cmd("Network.setCookies", {"cookies": cookies})
        await cmd("Page.enable")
        await cmd("Page.navigate", {"url": url})

        # Polling the DOM beats waiting on loadEventFired: the challenge page
        # fires load too, then REPLACES itself once the script clears. What
        # matters is which document is there now, not that one finished.
        html = None
        settled_at = None
        while time.monotonic() < deadline:
            got = await cmd("Runtime.evaluate", {
                "expression": "document.documentElement.outerHTML",
                "returnByValue": True,
            })
            html = (got.get("result", {}).get("result", {}) or {}).get("value")
            if html and _CHALLENGE_MARKER not in html and (
                "SIGI_STATE" in html or "roomId" in html
            ):
                if _LADDER_MARKER in html:
                    return html  # what we came for
                settled_at = settled_at or time.monotonic()
                if time.monotonic() - settled_at >= _LADDER_GRACE:
                    return html  # a room that simply has no HEVC ladder
            await asyncio.sleep(_POLL_INTERVAL)
        return html


def _run(coro, deadline: float):
    """asyncio.run(), but survive being called from a thread already running a
    loop.

    The capture subprocess is plain synchronous yt-dlp, and the in-process
    probes arrive on an asyncio.to_thread worker, so both reach here with no
    loop and take the fast path. A caller that awaits the extractor directly
    does not, and bare asyncio.run() raises there -- which this module would
    then swallow into None, silently costing the ladder rather than reporting
    anything. Handing the coroutine its own thread costs one thread and makes
    the lane behave the same either way.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict = {}

    def target():
        try:
            box["value"] = asyncio.run(coro)
        except Exception as exc:  # mirrored out, never raised in the worker
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout=max(1.0, deadline - time.monotonic()))
    if "error" in box:
        raise box["error"]
    return box.get("value")


def fetch_page(
    url: str, cookiefile: str | Path | None = None, timeout: float = DEFAULT_TIMEOUT
) -> str | None:
    """The page as a browser sees it, or None for every possible failure.

    Synchronous, like everything in services/ytdlp.py: it is called from inside
    a yt-dlp extractor, which has no event loop of its own.
    """
    exe = chrome_path()
    if not exe:
        log.warning("no chrome binary on PATH; keeping the H.264 ladder")
        return None

    profile = tempfile.mkdtemp(prefix="vd-chrome-")
    port = _free_port()
    proc = subprocess.Popen(
        [
            exe,
            "--headless=new",
            # No sandbox: the app runs unprivileged in a container, where
            # Chrome's setuid helper is unavailable. Kept narrow by what this
            # browser does -- it loads one TikTok URL and is killed.
            "--no-sandbox",
            "--disable-gpu",
            # Containers get a 64 MB /dev/shm, which Chrome outgrows and then
            # crashes on rather than degrading.
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
            "--mute-audio",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout
    try:
        ws_url = _page_target(port, deadline)
        if not ws_url:
            log.warning("chrome devtools never came up within %.0fs", timeout)
            return None
        html = _run(_drive(ws_url, url, cdp_cookies(cookiefile), deadline), deadline)
        log.info(
            "browser fetch of %s: %s bytes, ladder=%s",
            url, len(html or ""), _LADDER_MARKER in (html or ""),
        )
        return html
    except Exception as exc:
        log.warning("browser fetch of %s failed: %s", url, exc)
        return None
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
