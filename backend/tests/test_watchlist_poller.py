"""Watchlist CRUD + poller tests (US-009) — no network: every yt-dlp probe
is monkeypatched; recorder and manager are in-test stubs.
"""

import pytest
from sqlalchemy import select


class StubRecorder:
    """Matches RecorderSupervisor's public surface used by the poller."""

    def __init__(self):
        self.started = []

    def start_recording(self, recording_id):
        self.started.append(recording_id)
        return None


@pytest.fixture
def watch_env(authed_client, monkeypatch):
    """authed client + stubbed recorder + probe injection helpers."""
    import app.routers.watchlist as wl
    import app.services.poller as pl

    c, stub_manager = authed_client
    rec = StubRecorder()
    monkeypatch.setattr(pl, "get_recorder", lambda: rec)
    monkeypatch.setattr(pl, "get_manager", lambda: stub_manager)

    state = {
        # keyed by URL; falls back to default_info(url) when absent.
        # Default resolves to a valid offline creator so _add_creator works
        # without per-URL registration.
        "probe_results": {},
        "default_info": lambda url: {
            "_type": "playlist",
            "extractor_key": "BiliBili",
            "id": "12345",
            "uploader_id": "12345",
            "channel": "Test Creator",
            "title": "Test Creator",
            "entries": [],
        },
        "calls": [],
        "recorder": rec,
    }

    def fake_probe(url, cookiefile=None, *, extract_flat=False, playlist_items=None):
        state["calls"].append(
            {
                "url": url,
                "extract_flat": extract_flat,
                "playlist_items": playlist_items,
                "cookiefile": cookiefile,
            }
        )
        if url in state["probe_results"]:
            return state["probe_results"][url]
        return state["default_info"](url)

    monkeypatch.setattr(wl.ytdlp, "probe", fake_probe)
    monkeypatch.setattr(pl.ytdlp, "probe", fake_probe)

    # Room lookup is a real HTTP call in production; off unless a test opts in.
    state["rooms"] = {}
    state["room_lookups"] = []

    async def fake_resolve(platform, creator_id):
        state["room_lookups"].append((platform, str(creator_id)))
        return state["rooms"].get(str(creator_id))

    monkeypatch.setattr(wl.live_rooms, "resolve_room_url", fake_resolve)

    def set_probe(url, info):
        state["probe_results"][url] = info

    async def sweep():
        await pl.poller.poll_once()

    state.update(
        client=c, set_probe=set_probe, sweep=sweep, manager=stub_manager
    )
    return state


def _add_creator(c, url="https://space.bilibili.com/12345", scope="both"):
    r = c.post("/api/watchlist", json={"url": url, "scope": scope})
    assert r.status_code == 201, r.text
    return r.json()


CHANNEL_INFO = {
    "_type": "playlist",
    "extractor_key": "BiliBili",
    "id": "12345",
    "uploader_id": "12345",
    "channel": "Test Creator",
    "title": "Test Creator",
}


# ---- CRUD API -------------------------------------------------------------


def test_add_list_patch_delete_flow(watch_env):
    c = watch_env["client"]
    watch_env["set_probe"]("https://space.bilibili.com/12345", dict(CHANNEL_INFO))

    created = _add_creator(c)
    assert created["platform"] == "bilibili"
    assert created["display_name"] == "Test Creator"
    assert created["creator_id"] == "12345"
    assert created["scope"] == "both" and created["enabled"] is True

    rows = c.get("/api/watchlist").json()
    assert len(rows) == 1 and rows[0]["id"] == created["id"]
    assert rows[0]["last_seen_post_id"] is None

    # duplicate platform+creator -> 409 (a single video by the same creator
    # resolves to the same uploader_id)
    watch_env["set_probe"](
        "https://www.bilibili.com/video/BV1xx411c7XX",
        {"id": "BV1xx411c7XX", "uploader_id": "12345", "channel": "Test Creator"},
    )
    r = c.post(
        "/api/watchlist", json={"url": "https://www.bilibili.com/video/BV1xx411c7XX"}
    )
    assert r.status_code == 409

    # PATCH persists scope + enabled
    r = c.patch(
        f"/api/watchlist/{created['id']}", json={"scope": "lives", "enabled": False}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["scope"] == "lives" and body["enabled"] is False

    # disabled rows vanish from the poller's sweep — covered in
    # test_disabled_watch_not_polled

    # DELETE removes
    r = c.delete(f"/api/watchlist/{created['id']}")
    assert r.status_code == 204
    assert c.get("/api/watchlist").json() == []


def test_add_unsupported_url_400(watch_env):
    r = watch_env["client"].post(
        "/api/watchlist", json={"url": "https://example.com/x"}
    )
    assert r.status_code == 400


def test_add_probe_failure_400(watch_env):
    def boom(url, cookiefile=None, *, extract_flat=False, playlist_items=None):
        raise RuntimeError("nope")

    import app.routers.watchlist as wl

    orig = wl.ytdlp.probe
    wl.ytdlp.probe = boom  # not monkeypatched via fixture: must raise, not fall back
    try:
        r = watch_env["client"].post(
            "/api/watchlist", json={"url": "https://www.tiktok.com/@u"}
        )
    finally:
        wl.ytdlp.probe = orig
    assert r.status_code == 400
    assert "resolve creator" in r.json()["detail"]

    # PATCH unknown id / DELETE unknown id -> 404
    c = watch_env["client"]
    assert c.patch("/api/watchlist/999", json={"enabled": False}).status_code == 404
    assert c.delete("/api/watchlist/999").status_code == 404


def test_add_resolves_anonymous_channel_via_newest_entry(watch_env):
    """bilibili space listings carry an id and no name at all; the newest
    entry does, and the URL states the id the poller must poll back."""
    c = watch_env["client"]
    space = "https://space.bilibili.com/4549624"
    watch_env["set_probe"](
        space,
        {
            "_type": "playlist",
            "extractor_key": "BilibiliSpaceVideo",
            "id": "4549624",
            "entries": [{"id": "BV1x", "url": "https://www.bilibili.com/video/BV1x"}],
        },
    )
    watch_env["set_probe"](
        "https://www.bilibili.com/video/BV1x",
        {"id": "BV1x", "uploader_id": "4549624", "uploader": "Yuel"},
    )

    created = _add_creator(c, url=space)
    assert created["display_name"] == "Yuel"
    assert created["creator_id"] == "4549624"

    # resolve probe walks one page only, then names the creator from entry 1
    calls = [x for x in watch_env["calls"] if x["url"] == space]
    assert calls and calls[0]["extract_flat"] is True
    assert calls[0]["playlist_items"] == "1"


def test_add_falls_back_to_url_id_when_listing_is_nameless(watch_env):
    """Empty channel: nothing to name it after, but the URL still identifies
    the creator — better a bare id than a 400."""
    c = watch_env["client"]
    space = "https://space.bilibili.com/4549624"
    watch_env["set_probe"](
        space, {"_type": "playlist", "id": "4549624", "entries": []}
    )
    created = _add_creator(c, url=space)
    assert created["display_name"] == "4549624"
    assert created["creator_id"] == "4549624"


def test_add_blocked_listing_still_watches_a_profile_url(watch_env, monkeypatch):
    """bilibili's 412 is an egress-IP rate limit that clears on its own; the
    profile URL already says who to poll, so the add must not be lost to it."""
    import app.routers.watchlist as wl

    def blocked(url, cookiefile=None, *, extract_flat=False, playlist_items=None):
        raise RuntimeError(
            "ERROR: Request is blocked by server (412), please wait and try later."
        )

    monkeypatch.setattr(wl.ytdlp, "probe", blocked)
    c = watch_env["client"]
    r = c.post("/api/watchlist", json={"url": "https://space.bilibili.com/4549624"})
    assert r.status_code == 201, r.text
    assert r.json()["creator_id"] == "4549624"
    assert r.json()["display_name"] == "4549624"

    # a placeholder name is renameable
    r2 = c.patch(f"/api/watchlist/{r.json()['id']}", json={"display_name": "Liyuu_"})
    assert r2.status_code == 200 and r2.json()["display_name"] == "Liyuu_"
    assert c.patch(
        f"/api/watchlist/{r.json()['id']}", json={"display_name": "  "}
    ).status_code == 400


def test_add_blocked_post_url_points_at_credentials(watch_env, monkeypatch):
    """No profile id to fall back on: explain the wall instead of echoing
    yt-dlp's line."""
    import app.routers.watchlist as wl

    def blocked(url, cookiefile=None, *, extract_flat=False, playlist_items=None):
        raise RuntimeError(
            "ERROR: Request is blocked by server (412), please wait and try later."
        )

    monkeypatch.setattr(wl.ytdlp, "probe", blocked)
    r = watch_env["client"].post(
        "/api/watchlist", json={"url": "https://www.bilibili.com/video/BV1xx411c7XX"}
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    # 412 is a rate limit, not an auth wall: cookies do not lift it, so the
    # message must not send the user off to Credentials.
    assert "412" in detail and "rate-limiting" in detail
    assert "Credentials" not in detail


def test_add_auth_walled_url_points_at_credentials(watch_env, monkeypatch):
    """403/login walls are the case cookies actually fix."""
    import app.routers.watchlist as wl

    def walled(url, cookiefile=None, *, extract_flat=False, playlist_items=None):
        raise RuntimeError("ERROR: HTTP Error 403: Forbidden")

    monkeypatch.setattr(wl.ytdlp, "probe", walled)
    r = watch_env["client"].post(
        "/api/watchlist", json={"url": "https://www.instagram.com/p/Cabc123/"}
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "Credentials" in detail and "instagram cookies" in detail


def test_add_broken_url_is_never_swallowed(watch_env, monkeypatch):
    """Only anti-bot walls are survivable — a dead profile still 400s."""
    import app.routers.watchlist as wl

    def missing(url, cookiefile=None, *, extract_flat=False, playlist_items=None):
        raise RuntimeError("ERROR: Unable to extract user id; account not found")

    monkeypatch.setattr(wl.ytdlp, "probe", missing)
    r = watch_env["client"].post(
        "/api/watchlist", json={"url": "https://space.bilibili.com/4549624"}
    )
    assert r.status_code == 400
    assert "resolve creator" in r.json()["detail"]


async def test_probes_use_stored_cookies(watch_env, monkeypatch, tmp_path):
    """Watchlist resolve and poller sweep both run signed-in when a credential
    exists — the signed-out probe is exactly what bilibili blocks."""
    import app.routers.credentials as creds

    jar = tmp_path / "bilibili_cookies.txt"

    def arm():
        jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    async def fake_cookiefile(platform):
        return jar if platform == "bilibili" else None

    monkeypatch.setattr(creds, "aget_cookiefile", fake_cookiefile)

    c = watch_env["client"]
    space = "https://space.bilibili.com/12345"
    watch_env["set_probe"](space, dict(CHANNEL_INFO))

    arm()
    _add_creator(c, url=space)
    assert watch_env["calls"][-1]["cookiefile"] == str(jar)
    # decrypted jar is a temp file: it must not outlive the probe
    assert not jar.exists()

    arm()
    watch_env["calls"].clear()
    await watch_env["sweep"]()
    assert watch_env["calls"][-1]["cookiefile"] == str(jar)
    assert watch_env["calls"][-1]["playlist_items"] == "1-5"
    assert not jar.exists()


# ---- poller decisions -----------------------------------------------------


async def test_poller_lives_transition_creates_recording(watch_env):
    c, st = watch_env["client"], watch_env
    row = _add_creator(c, scope="lives")

    profile = f"https://space.bilibili.com/{row['creator_id']}"
    st["set_probe"](
        profile,
        {
            "_type": "playlist",
            "id": "room-1",
            "is_live": True,
            "entries": [{"id": "v1", "title": "stream"}],
        },
    )

    await st["sweep"]()

    assert st["recorder"].started, "poller should have started a recording"
    rid = st["recorder"].started[0]

    from sqlalchemy import select

    import app.db as db_mod
    from app.models import LiveRecording

    async with db_mod.async_session() as s:
        rec = await s.get(LiveRecording, rid)
        assert rec is not None
        assert rec.origin == "watchlist"
        assert rec.status == "recording"
        assert rec.platform == "bilibili"

    # Second sweep while still live: no duplicate recording (active guard).
    await st["sweep"]()
    assert st["recorder"].started == [rid]


async def test_live_room_url_is_polled_instead_of_the_listing(watch_env):
    """bilibili keeps rooms in their own id space, so a lives-only watch polls
    the room and never touches the (rate-limited) space listing."""
    c, st = watch_env["client"], watch_env
    room = "https://live.bilibili.com/7983646"
    r = c.post(
        "/api/watchlist",
        json={
            "url": "https://space.bilibili.com/5500585",
            "scope": "lives",
            "live_url": room,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["live_url"] == room

    st["set_probe"](room, {"id": "7983646", "is_live": True, "title": "live now"})
    st["calls"].clear()
    await st["sweep"]()

    probed = [x["url"] for x in st["calls"]]
    assert probed == [room], "lives-only watch must not probe the space listing"
    assert st["recorder"].started

    from app.models import LiveRecording
    import app.db as db_mod

    async with db_mod.async_session() as s:
        rec = await s.get(LiveRecording, st["recorder"].started[0])
        # the capture points at the room, not the profile page
        assert rec.room_url == room


async def test_offline_room_is_not_a_poll_error(watch_env, monkeypatch):
    """yt-dlp raises 'Streamer is not live' for an idle room — the normal
    state, not a broken sweep."""
    c, st = watch_env["client"], watch_env
    room = "https://live.bilibili.com/7983646"
    c.post(
        "/api/watchlist",
        json={
            "url": "https://space.bilibili.com/5500585",
            "scope": "lives",
            "live_url": room,
        },
    )

    import app.services.poller as pl

    def offline(url, cookiefile=None, *, extract_flat=False, playlist_items=None):
        raise RuntimeError("ERROR: [BiliLive] 7983646: Streamer is not live")

    monkeypatch.setattr(pl.ytdlp, "probe", offline)

    import app.services.events as events

    errors = []
    events.subscribe(errors.append)
    try:
        await st["sweep"]()
    finally:
        events.unsubscribe(errors.append)

    assert not st["recorder"].started
    assert [e for e in errors if e.get("type") == "watch.poll_error"] == []


async def test_room_probe_failure_still_reports(watch_env, monkeypatch):
    """A dead room URL is a real error and must not hide behind 'offline'."""
    c, st = watch_env["client"], watch_env
    c.post(
        "/api/watchlist",
        json={
            "url": "https://space.bilibili.com/5500585",
            "scope": "lives",
            "live_url": "https://live.bilibili.com/7983646",
        },
    )

    import app.services.poller as pl

    def boom(url, cookiefile=None, *, extract_flat=False, playlist_items=None):
        raise RuntimeError("ERROR: Unable to download JSON metadata")

    monkeypatch.setattr(pl.ytdlp, "probe", boom)

    import app.services.events as events

    errors = []
    events.subscribe(errors.append)
    try:
        await st["sweep"]()
    finally:
        events.unsubscribe(errors.append)

    assert [e for e in errors if e.get("type") == "watch.poll_error"]


def test_room_url_is_filled_in_on_add(watch_env):
    """Nothing a probe returns names a bilibili room, so the add resolves it
    once and stores it — polling never repeats the lookup."""
    c, st = watch_env["client"], watch_env
    st["rooms"]["4549624"] = "https://live.bilibili.com/5265"

    row = _add_creator(c, url="https://space.bilibili.com/4549624")
    assert row["live_url"] == "https://live.bilibili.com/5265"
    assert st["room_lookups"] == [("bilibili", "4549624")]


def test_supplied_room_url_skips_the_lookup(watch_env):
    c, st = watch_env["client"], watch_env
    r = c.post(
        "/api/watchlist",
        json={
            "url": "https://space.bilibili.com/4549624",
            "live_url": "https://live.bilibili.com/999",
        },
    )
    assert r.status_code == 201
    assert r.json()["live_url"] == "https://live.bilibili.com/999"
    assert st["room_lookups"] == []


def test_room_lookup_miss_never_blocks_the_add(watch_env):
    """No room, dead endpoint, timeout — all the same: the watch is still
    created and the field stays fillable by hand."""
    c, st = watch_env["client"], watch_env
    row = _add_creator(c, url="https://space.bilibili.com/4549624")
    assert row["live_url"] is None
    assert st["room_lookups"] == [("bilibili", "4549624")]


def test_live_url_is_validated_and_clearable(watch_env):
    c = watch_env["client"]
    r = c.post(
        "/api/watchlist",
        json={
            "url": "https://space.bilibili.com/5500585",
            "live_url": "https://www.tiktok.com/@someone/live",
        },
    )
    assert r.status_code == 400 and "bilibili" in r.json()["detail"]

    row = _add_creator(c, url="https://space.bilibili.com/5500585")
    assert row["live_url"] is None
    r = c.patch(
        f"/api/watchlist/{row['id']}",
        json={"live_url": "https://live.bilibili.com/7983646"},
    )
    assert r.status_code == 200
    assert r.json()["live_url"] == "https://live.bilibili.com/7983646"
    # blanking it puts the live check back on the profile probe
    r = c.patch(f"/api/watchlist/{row['id']}", json={"live_url": ""})
    assert r.status_code == 200 and r.json()["live_url"] is None


async def test_poller_offline_then_no_action(watch_env):
    c, st = watch_env["client"], watch_env
    _add_creator(c, scope="both")
    # default_info: offline, no entries
    await st["sweep"]()
    assert st["recorder"].started == []


async def test_poller_posts_enqueue_and_cursor(watch_env):
    c, st = watch_env["client"], watch_env
    row = _add_creator(c, scope="posts")

    profile = f"https://space.bilibili.com/{row['creator_id']}"
    st["set_probe"](
        profile,
        {
            "_type": "playlist",
            "entries": [
                {"id": "post2", "title": "Newest post", "url": "https://b23.tv/post2"},
                {"id": "post1", "title": "Older post", "url": "https://b23.tv/post1"},
            ],
        },
    )

    await st["sweep"]()

    # Job enqueued with is_auto + cursor advanced to newest id
    import app.db as db_mod
    from app.models import DownloadJob, CreatorWatch

    async with db_mod.async_session() as s:
        job = (
            await s.execute(select(DownloadJob))
        ).scalars().first()
        watch = await s.get(CreatorWatch, row["id"])
    assert job is not None and job.is_auto is True
    assert job.status == "queued"
    assert job.kind == "video"
    assert job.url == "https://b23.tv/post2"
    assert job.selected_quality == "best"  # settings default_quality
    assert st["manager"].enqueued == [job.id]  # enqueued AFTER commit
    assert watch.last_seen_post_id == "post2"

    # Same newest id again -> no re-enqueue
    async with db_mod.async_session() as s:
        count_before = len((await s.execute(select(DownloadJob))).scalars().all())
    await st["sweep"]()
    async with db_mod.async_session() as s:
        count_after = len((await s.execute(select(DownloadJob))).scalars().all())
    assert count_after == count_before


async def test_scope_lives_suppresses_post_downloads(watch_env):
    c, st = watch_env["client"], watch_env
    row = _add_creator(c, scope="lives")

    profile = f"https://space.bilibili.com/{row['creator_id']}"
    st["set_probe"](
        profile,
        {
            "_type": "playlist",
            "is_live": False,
            "entries": [{"id": "p9", "title": "A post", "url": "https://b23.tv/p9"}],
        },
    )
    await st["sweep"]()

    import app.db as db_mod
    from app.models import DownloadJob

    async with db_mod.async_session() as s:
        jobs = (await s.execute(select(DownloadJob))).scalars().all()
    assert jobs == []
    # cursor untouched too
    from app.models import CreatorWatch

    async with db_mod.async_session() as s:
        watch = await s.get(CreatorWatch, row["id"])
    assert watch.last_seen_post_id is None


async def test_per_creator_error_isolation(watch_env, monkeypatch):
    c, st = watch_env["client"], watch_env
    w1 = _add_creator(c, url="https://space.bilibili.com/111")
    w2 = _add_creator(c, url="https://www.tiktok.com/@other")

    import app.services.poller as pl

    calls = {"n": 0}

    def flaky_probe(url, cookiefile=None, *, extract_flat=False, playlist_items=None):
        calls["n"] += 1
        if "bilibili" in url:  # w1's resolved profile host
            raise RuntimeError("extractor exploded")
        return {"_type": "playlist", "entries": []}

    monkeypatch.setattr(pl.ytdlp, "probe", flaky_probe)

    errors = []
    import app.services.events as events

    def cap(e):
        errors.append(e)

    events.subscribe(cap)
    try:
        await st["sweep"]()  # must not raise despite creator 111 failing
    finally:
        events.unsubscribe(cap)

    assert calls["n"] == 2, "second creator still probed after first failed"
    assert any(
        e.get("type") == "watch.poll_error" and e.get("platform") == "bilibili"
        for e in errors
    )


async def test_poll_interval_from_settings(watch_env, monkeypatch):
    import app.services.poller as pl
    from app.services.settings_store import SettingsModel

    async def fake_aget_settings(session):
        return SettingsModel(poll_interval_seconds=90)

    monkeypatch.setattr(pl, "aget_settings", fake_aget_settings)
    interval = await pl.poller.current_interval()
    assert interval == 90

    # floor at 60s even if someone sets something silly
    async def tiny(session):
        return SettingsModel(poll_interval_seconds=5)

    monkeypatch.setattr(pl, "aget_settings", tiny)
    assert await pl.poller.current_interval() == 60


async def test_disabled_watch_not_polled(watch_env, monkeypatch):
    c, st = watch_env["client"], watch_env
    row = _add_creator(c, scope="both")
    r = c.patch(f"/api/watchlist/{row['id']}", json={"enabled": False})
    assert r.status_code == 200

    called = []

    def spy(url, cookiefile=None, *, extract_flat=False):
        called.append(url)
        return {"_type": "playlist", "entries": []}

    import app.services.poller as pl

    monkeypatch.setattr(pl.ytdlp, "probe", spy)
    await st["sweep"]()
    assert called == []
