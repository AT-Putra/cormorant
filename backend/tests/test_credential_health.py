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
