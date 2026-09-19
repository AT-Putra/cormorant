"""Cookie-jar health: the three bad states, and saying so exactly once.

The dedupe half matters as much as the detection half. Four creators on a 300s
sweep is 288 chances a day to repeat the same sentence, which is precisely how
watch.poll_error buried the activity feed (see poller._OFFLINE_RE). A warning
that arrives every five minutes is a warning nobody reads.
"""

import time

import pytest

from app.services import credential_health as ch


@pytest.fixture(autouse=True)
def _clean():
    ch.reset()
    yield
    ch.reset()


def _jar(expiry, name="sessionid"):
    """One Netscape line, the way a browser export writes an httpOnly cookie."""
    return f"#HttpOnly_.tiktok.com\tTRUE\t/\tTRUE\t{int(expiry)}\t{name}\tabc123\n"


# ---- offline detection ---------------------------------------------------


def test_no_jar_reads_as_missing():
    assert ch.inspect(None)[0] == "missing"
    assert ch.inspect("")[0] == "missing"
    assert ch.inspect("   \n")[0] == "missing"


def test_a_jar_without_a_session_cookie_reads_as_missing():
    """Cookies that carry no login are the same problem wearing a file."""
    jar = _jar(time.time() + 999999, name="tt_csrf_token")
    assert ch.inspect(jar)[0] == "missing"


def test_a_past_expiry_reads_as_expired():
    state, detail = ch.inspect(_jar(time.time() - 3 * 86400))
    assert state == "expired"
    assert "3d ago" in detail


def test_expiry_inside_the_warning_window_reads_as_expiring():
    state, detail = ch.inspect(_jar(time.time() + 2 * 86400))
    assert state == "expiring"
    assert "h)" in detail


def test_a_healthy_jar_reads_as_ok():
    assert ch.inspect(_jar(time.time() + 90 * 86400))[0] == "ok"


def test_the_latest_session_cookie_wins():
    """Exports carry several; one stale line must not condemn a live session."""
    now = time.time()
    jar = _jar(now - 86400, "sid_tt") + _jar(now + 60 * 86400, "sessionid")
    assert ch.inspect(jar)[0] == "ok"


def test_comment_lines_are_skipped_but_httponly_is_not():
    now = time.time() + 90 * 86400
    jar = "# Netscape HTTP Cookie File\n# comment\n" + _jar(now)
    assert ch.inspect(jar)[0] == "ok"


def test_a_browser_session_cookie_is_not_read_as_expired():
    """Expiry 0 means 'until the browser closes', not 'expired in 1970'."""
    assert ch.inspect(_jar(0))[0] == "missing"


# ---- the observed (online) half ------------------------------------------


def test_a_logged_out_page_turns_a_healthy_jar_into_rejected():
    ok = ("ok", "")
    state, detail = ch.combine(ok, logged_in=False)
    assert state == "rejected"
    assert detail


def test_a_logged_in_page_clears_a_rejection():
    assert ch.combine(("rejected", "x"), logged_in=True)[0] == "ok"


def test_a_logged_out_page_does_not_mask_a_worse_state():
    """Expired is the cause; logged-out is only its symptom. Report the cause."""
    assert ch.combine(("expired", "x"), logged_in=False)[0] == "expired"
    assert ch.combine(("missing", "x"), logged_in=False)[0] == "missing"


def test_an_observation_is_consumed_once():
    """A stale observation must not outlive the fetch that made it, or a jar
    stays 'rejected' long after it started working again."""
    ch.note_session("tiktok", False)
    assert ch._take_observation("tiktok") is False
    assert ch._take_observation("tiktok") is None


# ---- reporting: transitions only -----------------------------------------


def _capture():
    from app.services import events

    seen = []
    events.subscribe(seen.append)
    return seen, lambda: events.unsubscribe(seen.append)


def test_a_new_problem_is_announced_once():
    seen, done = _capture()
    try:
        assert ch.report("tiktok", "expired", "gone") is True
        assert ch.report("tiktok", "expired", "gone") is False
        assert ch.report("tiktok", "expired", "gone") is False
    finally:
        done()
    stale = [e for e in seen if e.get("type") == "credentials.stale"]
    assert len(stale) == 1
    assert stale[0]["state"] == "expired"


def test_a_changed_problem_is_announced_again():
    """expiring -> expired is new information, not a repeat."""
    seen, done = _capture()
    try:
        ch.report("tiktok", "expiring", "soon")
        ch.report("tiktok", "expired", "gone")
    finally:
        done()
    assert [e["state"] for e in seen if e.get("type") == "credentials.stale"] == [
        "expiring",
        "expired",
    ]


def test_recovery_is_announced():
    seen, done = _capture()
    try:
        ch.report("tiktok", "expired", "gone")
        assert ch.report("tiktok", "ok", "") is True
    finally:
        done()
    assert [e.get("type") for e in seen] == ["credentials.stale", "credentials.ok"]


def test_healthy_at_boot_says_nothing():
    seen, done = _capture()
    try:
        assert ch.report("tiktok", "ok", "") is False
    finally:
        done()
    assert seen == []


# ---- bilibili: the probe -----------------------------------------------------


def _bili_jar(expiry):
    return f"#HttpOnly_.bilibili.com\tTRUE\t/\tTRUE\t{int(expiry)}\tSESSDATA\tabc%2Cdef\n"


def test_bilibili_session_cookie_is_sessdata():
    assert ch.inspect(_bili_jar(time.time() + 90 * 86400), "bilibili")[0] == "ok"
    state, detail = ch.inspect(_jar(time.time() + 90 * 86400), "bilibili")
    assert state == "missing"
    assert "bilibili" in detail


def test_bilibili_logged_in_reads_islogin_and_nothing_else(monkeypatch):
    answers = iter([
        {"code": 0, "data": {"isLogin": True, "uname": "x"}},
        {"code": -101, "message": "账号未登录", "data": {"isLogin": False}},
        {"code": 0, "data": {}},          # no verdict: the key is not there
        "not even a dict",                # no verdict
    ])
    monkeypatch.setattr(ch, "bilibili_nav", lambda cookiefile: next(answers))
    assert ch._bilibili_logged_in("jar") is True
    assert ch._bilibili_logged_in("jar") is False
    assert ch._bilibili_logged_in("jar") is None
    assert ch._bilibili_logged_in("jar") is None


def test_a_network_error_is_no_verdict(monkeypatch):
    def boom(cookiefile):
        raise OSError("connection reset")

    monkeypatch.setattr(ch, "bilibili_nav", boom)
    assert ch._bilibili_logged_in("jar") is None


def _probe_env(monkeypatch, jar, verdict):
    """A sweep whose jar and probe answer are scripted; records the events."""
    seen = []
    monkeypatch.setattr(ch.events, "publish", seen.append)

    async def jar_text(platform):
        return jar

    monkeypatch.setattr(ch, "_jar_text", jar_text)

    probed = []

    async def probe(platform):
        probed.append(platform)
        return verdict

    monkeypatch.setattr(ch, "_probe", probe)
    return seen, probed


async def test_a_rotated_sessdata_is_reported_as_rejected(monkeypatch):
    """Unexpired in the file, refused by the site: the state the offline check
    cannot see and the one that actually happens."""
    seen, probed = _probe_env(monkeypatch, _bili_jar(time.time() + 300 * 86400), False)
    assert await ch.sweep("bilibili") == "rejected"
    assert probed == ["bilibili"]
    assert [e["type"] for e in seen] == ["credentials.stale"]
    assert seen[0]["platform"] == "bilibili"
    assert "logged out" in seen[0]["detail"]


async def test_a_probe_with_no_verdict_leaves_the_offline_answer(monkeypatch):
    seen, _ = _probe_env(monkeypatch, _bili_jar(time.time() + 300 * 86400), None)
    assert await ch.sweep("bilibili") == "ok"
    assert seen == []


async def test_an_expired_jar_is_not_probed(monkeypatch):
    """The site would only repeat what the file already says."""
    seen, probed = _probe_env(monkeypatch, _bili_jar(time.time() - 86400), False)
    assert await ch.sweep("bilibili") == "expired"
    assert probed == []


async def test_a_fresh_export_clears_the_rejection(monkeypatch):
    seen, _ = _probe_env(monkeypatch, _bili_jar(time.time() + 300 * 86400), False)
    await ch.sweep("bilibili")
    _probe_env(monkeypatch, _bili_jar(time.time() + 300 * 86400), True)[0]
    # events.publish is re-patched by the second env; read the state instead
    assert await ch.sweep("bilibili") == "ok"


async def test_tiktok_has_no_probe_and_keeps_its_observation(monkeypatch):
    seen = []
    monkeypatch.setattr(ch.events, "publish", seen.append)

    async def jar_text(platform):
        return _jar(time.time() + 90 * 86400)

    monkeypatch.setattr(ch, "_jar_text", jar_text)
    ch.note_session("tiktok", False)
    assert await ch.sweep("tiktok") == "rejected"
    assert "TikTok" in seen[0]["detail"]
