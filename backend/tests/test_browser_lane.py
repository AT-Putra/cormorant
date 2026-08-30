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


# ---- Chrome is the escalation, not the default ---------------------------
#
# The lane used to ask the browser first. Measured 2026-08-30 with the WAF
# challenge down, a plain anonymous fetch returned the whole ladder including
# uhd_60, so that ordering spent up to 45s of Chrome -- 45s of a live stream
# not yet being recorded -- for a page the ordinary fetch already had. The
# plain fetch goes first now and Chrome answers only the three cases it cannot.


def _ie_with_pages(monkeypatch, plain, browsed="<html>browser</html>"):
    """Extractor whose two page sources are stubbed; records Chrome's budget."""
    from yt_dlp import YoutubeDL

    ie = _live_ie_class()(YoutubeDL({"quiet": True, "no_warnings": True}))
    seen = {"browser": False, "timeout": None}

    def fake_browser(self, url, timeout=None):
        seen["browser"] = True
        seen["timeout"] = timeout
        return browsed

    monkeypatch.setattr(type(ie), "_browser_webpage", fake_browser)
    monkeypatch.setattr(type(ie), "_download_webpage", lambda *a, **k: plain)
    return ie, seen


def test_a_plain_page_with_the_ladder_never_spawns_chrome(monkeypatch):
    """The green row: WAF down, room ungated. Chrome buys nothing here."""
    page = '<html>{"hevcStreamData":{}}SIGI_STATE</html>'
    ie, seen = _ie_with_pages(monkeypatch, plain=page)
    ie._hevc_formats("https://www.tiktok.com/@x/live", "room-1")
    assert not seen["browser"], "the plain fetch already had the ladder"


def test_the_waf_stub_escalates_to_chrome_on_the_full_budget(monkeypatch):
    stub = '<html>_wafchallengeid Please wait...</html>'
    ie, seen = _ie_with_pages(monkeypatch, plain=stub)
    ie._hevc_formats("https://www.tiktok.com/@x/live", "room-1")
    assert seen["browser"], "only a browser can run the challenge script"
    assert seen["timeout"] is None, "a challenge needs the default budget"


def test_the_maintenance_stub_escalates_on_the_full_budget(monkeypatch):
    """TikTok's other refusal: ~537 B, "Site Maintenance", 200 OK. Caught in
    the wild 2026-08-30 from a laptop while the deploy host was being served
    the real 241 KB page in the same minute.

    It must never be filed as 'no-ladder'. That label means "a real page that
    withheld the ladder", which is the age-gate signal -- a block wearing it
    would take the short budget and corrupt the record meant to settle whether
    the gate needs a browser."""
    stub = (
        "<!doctype html><html><head><title>Site Maintenance</title></head>"
        "<body><h1>Oops! Something went wrong</h1></body></html>"
    )
    ie, seen = _ie_with_pages(monkeypatch, plain=stub)
    ie._hevc_formats("https://www.tiktok.com/@x/live", "room-1")
    assert seen["browser"]
    assert seen["timeout"] is None, "a block needs the full budget, not the gate's"


def test_a_big_page_mentioning_maintenance_is_still_a_real_page(monkeypatch):
    """The marker only counts in a stub-sized body, or a room whose title says
    "Site Maintenance" would be misread as a block."""
    from app.ytdlp_plugins.vd.yt_dlp_plugins.extractor import tiktok_live

    page = "<html>SIGI_STATE Site Maintenance" + "x" * 6000 + "</html>"
    assert tiktok_live._blocked_as(page) is None
    ie, seen = _ie_with_pages(monkeypatch, plain=page)
    ie._hevc_formats("https://www.tiktok.com/@x/live", "room-1")
    assert seen["timeout"] == tiktok_live._NO_LADDER_TIMEOUT


def test_a_refusal_stub_never_judges_the_cookie_jar():
    """A block says nothing about the session; reading it as "logged out"
    would warn about good cookies every time TikTok blocked us."""
    from yt_dlp import YoutubeDL

    ie = _live_ie_class()(YoutubeDL({"quiet": True, "no_warnings": True}))
    for stub in (
        "<html>_wafchallengeid Please wait...</html>",
        "<html><title>Site Maintenance</title>Oops!</html>",
    ):
        assert ie._session_state(stub) is None


def test_an_unreadable_page_escalates_to_chrome(monkeypatch):
    ie, seen = _ie_with_pages(monkeypatch, plain=None)
    ie._hevc_formats("https://www.tiktok.com/@x/live", "room-1")
    assert seen["browser"]
    assert seen["timeout"] is None


def test_a_real_page_without_the_ladder_escalates_on_a_short_budget(monkeypatch):
    """The age-gate row. Chrome gets a shorter budget because the page already
    rendered, and because a room with no HEVC at all lands here too."""
    from app.ytdlp_plugins.vd.yt_dlp_plugins.extractor import tiktok_live

    ie, seen = _ie_with_pages(monkeypatch, plain="<html>SIGI_STATE real</html>")
    ie._hevc_formats("https://www.tiktok.com/@x/live", "room-1")
    assert seen["browser"]
    assert seen["timeout"] == tiktok_live._NO_LADDER_TIMEOUT


def test_a_browser_failure_falls_back_to_the_plain_fetch(monkeypatch):
    """Chrome returning nothing must never cost the page we already had."""
    from yt_dlp import YoutubeDL

    ie = _live_ie_class()(YoutubeDL({"quiet": True, "no_warnings": True}))
    monkeypatch.setattr(
        type(ie), "_browser_webpage", lambda self, url, timeout=None: None)
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
