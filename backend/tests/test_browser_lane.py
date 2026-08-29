"""The browser lane must stay off everywhere except a running capture.

Chrome is the only client that clears TikTok's WAF challenge, and the challenged
page is the only place the HEVC ladder lives -- so without this lane every
capture is capped at the H.264 720p tier. But a browser launch is expensive
enough that spawning one per creator per poll sweep would be worse than the
ladder is worth, so the lane is opt-in and exactly one caller opts in.

These pin the three ways that bargain silently breaks: the lane defaulting on,
the recorder forgetting to switch it on for the capture subprocess, and a
browser failure turning into a failed extraction instead of the H.264 result.
"""

import asyncio
import json
import os
import time

import pytest

from app.services import browser, recorder


def _live_ie_class():
    from yt_dlp.globals import extractors

    for cls in extractors.value.values():
        if getattr(cls, "IE_NAME", None) == "tiktok:live":
            return cls
    pytest.fail("tiktok:live extractor missing from the registry")


# ---- the switch ----------------------------------------------------------


def test_lane_is_off_by_default(monkeypatch):
    monkeypatch.delenv(browser.ENABLE_ENV, raising=False)
    assert browser.enabled() is False


def test_lane_is_on_only_for_the_exact_value(monkeypatch):
    monkeypatch.setenv(browser.ENABLE_ENV, "1")
    assert browser.enabled() is True
    # anything else is a typo, not a request
    monkeypatch.setenv(browser.ENABLE_ENV, "true")
    assert browser.enabled() is False


def test_capture_subprocess_switches_the_lane_on(monkeypatch):
    """The recorder is the one caller allowed to turn this on."""
    seen = {}

    async def fake_exec(*cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return object()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(recorder._spawn_proc(["echo", "hi"]))
    assert seen["env"][browser.ENABLE_ENV] == "1"
    # and it must not throw away the rest of the environment
    assert "PATH" in seen["env"] or os.name != "posix"


# ---- cookies -------------------------------------------------------------


NETSCAPE = (
    "# Netscape HTTP Cookie File\n"
    ".tiktok.com\tTRUE\t/\tTRUE\t1900000000\tsessionid\tsecret-value\n"
    ".tiktok.com\tTRUE\t/\tFALSE\t0\tttwid\tplain\n"
)


def test_cookies_become_a_cdp_payload(tmp_path):
    f = tmp_path / "cookies.txt"
    f.write_text(NETSCAPE, encoding="utf-8")
    got = {c["name"]: c for c in browser.cdp_cookies(f)}
    assert got["sessionid"]["value"] == "secret-value"
    assert got["sessionid"]["domain"] == ".tiktok.com"
    assert got["sessionid"]["secure"] is True
    assert got["sessionid"]["expires"] == 1900000000.0
    # a session cookie carries no expiry and must still survive the mapping
    assert got["ttwid"]["secure"] is False
    assert "expires" not in got["ttwid"]


def test_no_cookiefile_is_not_an_error():
    assert browser.cdp_cookies(None) == []


def test_an_unreadable_jar_costs_cookies_not_the_capture(tmp_path):
    f = tmp_path / "junk.txt"
    f.write_text("this is not a cookie file", encoding="utf-8")
    assert browser.cdp_cookies(f) == []


# ---- failure is always the H.264 ladder, never an exception --------------


def test_a_missing_browser_returns_none(monkeypatch):
    monkeypatch.setattr(browser, "chrome_path", lambda: None)
    assert browser.fetch_page("https://www.tiktok.com/@x/live") is None


def test_browser_off_means_the_plugin_does_not_reach_for_chrome(monkeypatch):
    monkeypatch.delenv(browser.ENABLE_ENV, raising=False)
    monkeypatch.setattr(browser, "fetch_page", lambda *a, **k: pytest.fail(
        "the lane is off; nothing may spawn a browser"))
    ie = _live_ie_class()(__import__("yt_dlp").YoutubeDL(
        {"quiet": True, "no_warnings": True}))
    assert ie._browser_webpage("https://www.tiktok.com/@x/live") is None


def test_the_browser_page_is_preferred_when_the_lane_is_on(monkeypatch):
    """A browser page must be used INSTEAD of the challenged yt-dlp fetch."""
    from yt_dlp import YoutubeDL

    page = '<html>{"hevcStreamData":{}}SIGI_STATE</html>'
    ie = _live_ie_class()(YoutubeDL({"quiet": True, "no_warnings": True}))
    monkeypatch.setattr(type(ie), "_browser_webpage", lambda self, url: page)
    monkeypatch.setattr(type(ie), "_download_webpage", lambda *a, **k: pytest.fail(
        "the browser answered; the challenged fetch must not run"))
    # no ladder in that stub, so [] -- the point is which fetch was used
    assert ie._hevc_formats("https://www.tiktok.com/@x/live", "room-1") == []


def test_a_browser_failure_falls_back_to_the_plain_fetch(monkeypatch):
    from yt_dlp import YoutubeDL

    ie = _live_ie_class()(YoutubeDL({"quiet": True, "no_warnings": True}))
    monkeypatch.setattr(type(ie), "_browser_webpage", lambda self, url: None)
    used = {}

    def fake_download(self, url, *a, **k):
        used["yes"] = True
        return ""

    monkeypatch.setattr(type(ie), "_download_webpage", fake_download)
    assert ie._hevc_formats("https://www.tiktok.com/@x/live", "room-1") == []
    assert used.get("yes"), "a browserless run must still try the ordinary fetch"


# ---- the lane must not care whether a loop is already turning -------------
#
# The capture subprocess is synchronous yt-dlp and probes arrive on a
# to_thread worker, so both reach the driver with no running loop. A caller
# that awaits the extractor directly does not, and a bare asyncio.run() raises
# there -- which this module swallows into None, costing the ladder while
# reporting nothing. That is the failure mode this whole feature exists to
# stop, so it gets a test rather than a comment.


async def _answer():
    return "the page"


def test_driver_runs_without_a_loop():
    assert browser._run(_answer(), deadline=_soon()) == "the page"


@pytest.mark.asyncio
async def test_driver_survives_a_running_loop():
    assert browser._run(_answer(), deadline=_soon()) == "the page"


@pytest.mark.asyncio
async def test_a_driver_failure_is_still_raised_from_a_running_loop():
    async def boom():
        raise RuntimeError("devtools went away")

    with pytest.raises(RuntimeError, match="devtools went away"):
        browser._run(boom(), deadline=_soon())


def _soon() -> float:
    import time

    return time.monotonic() + 30


# ---- the ladder, not merely the first paint ------------------------------
#
# A live room's first paint routinely carries roomId while hevcStreamData is
# still rendering. Returning on "looks like a real page" therefore brought back
# a room WITHOUT its ladder that the same page served a second later -- caught
# on prod, where one room came back with 12 HEVC formats and another with none
# in 3s. So the driver keeps looking, bounded, and rooms that genuinely publish
# no HEVC still come back rather than hanging.


class _FakeWS:
    """A websocket that replays a scripted sequence of DOM snapshots."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.methods = []
        self._replies = []
        self._last = ""

    async def send(self, raw):
        msg = json.loads(raw)
        self.methods.append(msg["method"])
        if msg["method"] == "Runtime.evaluate":
            if self.pages:
                self._last = self.pages.pop(0)
            self._replies.append(
                {"id": msg["id"], "result": {"result": {"value": self._last}}})
        else:
            self._replies.append({"id": msg["id"], "result": {}})

    async def recv(self):
        return json.dumps(self._replies.pop(0))


def _fake_connect(ws):
    class _CM:
        async def __aenter__(self_inner):
            return ws

        async def __aexit__(self_inner, *exc):
            return False

    return lambda *a, **k: _CM()


def _drive(pages, monkeypatch, grace=0.05):
    import websockets

    ws = _FakeWS(pages)
    monkeypatch.setattr(websockets, "connect", _fake_connect(ws))
    monkeypatch.setattr(browser, "_LADDER_GRACE", grace)
    html = asyncio.run(browser._drive(
        "ws://x", "https://www.tiktok.com/@x/live", [], time.monotonic() + 20))
    return html, ws


FIRST_PAINT = '<html>SIGI_STATE "roomId":"1"</html>'
WITH_LADDER = '<html>SIGI_STATE "roomId":"1" hevcStreamData uhd_60</html>'


def test_the_first_paint_does_not_win_over_the_ladder(monkeypatch):
    # The grace has to outlast several polls to be a grace at all; production
    # pairs a 10s window with a 0.5s poll, and the ratio is what matters here.
    html, _ = _drive([FIRST_PAINT, FIRST_PAINT, WITH_LADDER], monkeypatch, grace=5.0)
    assert "hevcStreamData" in html


def test_the_grace_must_outlast_the_poll_interval():
    """A grace shorter than one poll would return the first paint every time,
    which is the bug this pair of tests exists to keep fixed."""
    assert browser._LADDER_GRACE >= 10 * browser._POLL_INTERVAL


def test_a_room_without_a_ladder_still_comes_back(monkeypatch):
    html, _ = _drive([FIRST_PAINT], monkeypatch)
    assert html == FIRST_PAINT


def test_cookies_are_set_before_the_page_is_asked_for(monkeypatch):
    """Order matters: cookies after navigate would authenticate nothing."""
    import websockets

    ws = _FakeWS([WITH_LADDER])
    monkeypatch.setattr(websockets, "connect", _fake_connect(ws))
    asyncio.run(browser._drive(
        "ws://x", "https://www.tiktok.com/@x/live",
        [{"name": "sessionid", "value": "v", "domain": ".tiktok.com"}],
        time.monotonic() + 20))
    assert ws.methods.index("Network.setCookies") < ws.methods.index("Page.navigate")
