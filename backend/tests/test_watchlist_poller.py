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

    def fake_probe(url, cookiefile=None, *, extract_flat=False):
        state["calls"].append({"url": url, "extract_flat": extract_flat})
        if url in state["probe_results"]:
            return state["probe_results"][url]
        return state["default_info"](url)

    monkeypatch.setattr(wl.ytdlp, "probe", fake_probe)
    monkeypatch.setattr(pl.ytdlp, "probe", fake_probe)

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
    def boom(url, cookiefile=None, *, extract_flat=False):
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

    def flaky_probe(url, cookiefile=None, *, extract_flat=False):
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
