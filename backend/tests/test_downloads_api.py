"""API tests for /api/downloads — no network; yt-dlp.probe and the download
manager are stubbed (see tests/conftest.py for the DB + auth conventions).
"""

import asyncio

import pytest


@pytest.fixture
def client(authed_client):
    import app.routers.downloads as dl

    c, stub = authed_client
    yield c, stub, dl


def _probe_info():
    return {
        "title": "Sample Video",
        "duration": 61.5,
        # yt-dlp runs format selection even under skip_download, and reports
        # the merged pair it intends to mux.
        "format_id": "137+140",
        "formats": [
            {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none",
             "tbr": 50},
            {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
             "tbr": 130},
            {"format_id": "137", "ext": "mp4", "resolution": "1080x1920", "fps": 30,
             "vcodec": "avc1.640028", "acodec": "none", "filesize_approx": 10_485_760,
             "tbr": 2000},
            {"format_id": "137+140", "ext": "mp4", "resolution": "1080x1920", "fps": 30,
             "vcodec": "avc1.640028", "acodec": "mp4a.40.2", "tbr": 4500},
            {"format_id": "160", "ext": "mp4", "resolution": "144x256", "fps": 30,
             "vcodec": "avc1", "acodec": "none", "tbr": 100},
        ],
    }


def test_probe_filters_and_sorts_formats(client, monkeypatch):
    c, _stub, dl = client
    monkeypatch.setattr(
        dl.ytdlp, "probe", lambda url, cookiefile=None, **kw: _probe_info()
    )

    r = c.post("/api/downloads/probe", json={"url": "https://www.bilibili.com/video/BV1xx411c7XX"})
    assert r.status_code == 200
    data = r.json()
    assert data["platform"] == "bilibili"
    assert data["title"] == "Sample Video"
    assert data["duration"] == 61.5
    ids = [f["format_id"] for f in data["formats"]]
    # Storyboard dropped (no streams), sub-200kbit ladders dropped; sorted
    # descending by tbr.
    assert ids == ["137+140", "137"]
    f137 = next(f for f in data["formats"] if f["format_id"] == "137")
    assert f137["filesize_approx"] == 10_485_760
    assert data["formats"][0]["tbr"] == 4500
    # Codec/fps/bitrate reach the client: the quality dropdown labels entries
    # with them, so two same-resolution formats are told apart by codec.
    assert (f137["vcodec"], f137["acodec"], f137["fps"]) == ("avc1.640028", "none", 30)


def test_probe_reports_what_best_available_resolves_to(client, monkeypatch):
    """The dropdown marks a row as best; the backend decides which one."""
    c, _stub, dl = client
    monkeypatch.setattr(dl.ytdlp, "probe", lambda url, cookiefile=None, **kw: _probe_info())

    r = c.post("/api/downloads/probe", json={"url": "https://www.bilibili.com/video/BV1xx411c7XX"})

    assert r.status_code == 200
    assert r.json()["best_format_id"] == "137+140"


def test_probe_selection_runs_under_the_configured_cap(client, monkeypatch):
    """'Best available' is the uncapped-probe answer only when no cap is set.

    build_opts caps a no-format_id download with default_quality, so a probe
    that skipped the cap would mark a 4K row as best on an account capped to
    1080p -- and the download would then quietly fetch something else.
    """
    c, _stub, dl = client
    seen: dict = {}

    def _capture(url, cookiefile=None, **kw):
        seen.update(kw)
        return _probe_info()

    monkeypatch.setattr(dl.ytdlp, "probe", _capture)
    assert c.put("/api/settings", json={"default_quality": "1080p"}).status_code == 200

    r = c.post("/api/downloads/probe", json={"url": "https://www.bilibili.com/video/BV1xx411c7XX"})

    assert r.status_code == 200
    assert seen["format_sort"] == dl.ytdlp.quality_sort("1080p")
    assert seen["format_sort"] is not None


def test_probe_without_a_cap_passes_no_sort(client, monkeypatch):
    """The default is 'best', which quality_sort answers with None -- yt-dlp's
    own ordering, not a sort string that happens to mean the same thing."""
    c, _stub, dl = client
    seen: dict = {}

    def _capture(url, cookiefile=None, **kw):
        seen.update(kw)
        return _probe_info()

    monkeypatch.setattr(dl.ytdlp, "probe", _capture)

    c.post("/api/downloads/probe", json={"url": "https://www.bilibili.com/video/BV1xx411c7XX"})

    assert seen["format_sort"] is None


def _thin_probe_info():
    """Every stream under the 200kbit floor, plus a storyboard. Some short or
    heavily-compressed clips look like this."""
    return {
        "title": "Thin Video",
        "formats": [
            {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none",
             "tbr": 50},
            {"format_id": "low1", "ext": "mp4", "resolution": "256x144", "fps": 15,
             "vcodec": "avc1", "acodec": "mp4a.40.2", "tbr": 120},
            {"format_id": "low2", "ext": "mp4", "resolution": "426x240", "fps": 15,
             "vcodec": "avc1", "acodec": "mp4a.40.2", "tbr": 180},
        ],
    }


def test_thin_formats_resurface_when_nothing_clears_the_floor(client, monkeypatch):
    """An all-low-bitrate video must not return [] — Queue.tsx hides the
    picker behind formats.length > 0, so that reads as a dropdown that
    silently isn't there."""
    c, _stub, dl = client
    monkeypatch.setattr(dl.ytdlp, "probe", lambda url, cookiefile=None, **kw: _thin_probe_info())

    r = c.post("/api/downloads/probe", json={"url": "https://www.bilibili.com/video/BV1xx411c7XX"})
    assert r.status_code == 200
    ids = [f["format_id"] for f in r.json()["formats"]]
    # Sorted by tbr descending, and the storyboard stays dropped even here.
    assert ids == ["low2", "low1"]


def test_thin_formats_stay_demoted_when_better_exist(client, monkeypatch):
    """The floor is still a preference: nothing thin leaks in alongside real
    ladders."""
    c, _stub, dl = client
    monkeypatch.setattr(dl.ytdlp, "probe", lambda url, cookiefile=None, **kw: _probe_info())

    r = c.post("/api/downloads/probe", json={"url": "https://www.bilibili.com/video/BV1xx411c7XX"})
    ids = [f["format_id"] for f in r.json()["formats"]]
    assert "140" not in ids and "160" not in ids


def test_storyboard_only_still_yields_nothing(client, monkeypatch):
    """Resurfacing thin formats must not resurface manifests/storyboards."""
    c, _stub, dl = client
    monkeypatch.setattr(
        dl.ytdlp,
        "probe",
        lambda url, cookiefile=None, **kw: {
            "title": "SB only",
            "formats": [
                {"format_id": "sb0", "ext": "mhtml", "vcodec": "none",
                 "acodec": "none", "tbr": 50},
            ],
        },
    )
    r = c.post("/api/downloads/probe", json={"url": "https://www.bilibili.com/video/BV1xx411c7XX"})
    assert r.json()["formats"] == []


def _live_probe_info():
    """Bilibili live: no resolution, fps or tbr — the tier note and protocol
    are the only things separating four otherwise identical entries."""
    return {
        "title": "Live Room",
        "formats": [
            {"format_id": "source-0", "ext": "fmp4", "vcodec": "avc",
             "protocol": "m3u8_native", "format_note": "原画"},
            {"format_id": "source-2", "ext": "flv", "vcodec": "avc",
             "protocol": "https", "format_note": "原画"},
        ],
    }


def test_probe_keeps_live_formats_with_tier_and_protocol(client, monkeypatch):
    c, _stub, dl = client
    monkeypatch.setattr(dl.ytdlp, "probe", lambda url, cookiefile=None, **kw: _live_probe_info())

    r = c.post("/api/downloads/probe", json={"url": "https://live.bilibili.com/23630605"})
    assert r.status_code == 200
    fmts = r.json()["formats"]
    # No tbr means the sub-200kbit filter must not swallow them.
    assert [f["format_id"] for f in fmts] == ["source-0", "source-2"]
    assert all(f["format_note"] == "原画" for f in fmts)
    assert [f["protocol"] for f in fmts] == ["m3u8_native", "https"]


def test_probe_invalid_url_400(client):
    c, _stub, _dl = client
    r = c.post("/api/downloads/probe", json={"url": "not a url at all"})
    assert r.status_code == 400


def test_probe_extractor_failure_400(client, monkeypatch):
    c, _stub, dl = client

    def boom(url, cookiefile=None):
        raise RuntimeError("Unsupported URL")

    monkeypatch.setattr(dl.ytdlp, "probe", boom)
    r = c.post("/api/downloads/probe", json={"url": "https://www.tiktok.com/@u/video/1"})
    assert r.status_code == 400
    assert "Probe failed" in r.json()["detail"]


def test_create_job_queues_and_lists(client, monkeypatch):
    c, stub, dl = client
    monkeypatch.setattr(
        dl.ytdlp,
        "probe",
        lambda url, cookiefile=None, **kw: {"title": "My Post", "uploader": "creatorA"},
    )

    r = c.post("/api/downloads", json={"url": "https://www.tiktok.com/@someuser/video/123"})
    assert r.status_code == 201
    job = r.json()
    assert job["platform"] == "tiktok"
    assert job["status"] == "queued"
    assert job["title"] == "My Post"
    assert job["creator"] == "creatorA"
    assert job["is_auto"] is False
    assert stub.enqueued == [job["id"]]

    # Metadata probe failure still queues with fallback title/creator.
    def boom(url, cookiefile=None):
        raise RuntimeError("down")

    monkeypatch.setattr(dl.ytdlp, "probe", boom)
    r = c.post("/api/downloads", json={"url": "https://www.tiktok.com/@x/video/456"})
    assert r.status_code == 201
    job2 = r.json()
    assert job2["creator"] == "tiktok"
    assert sorted(stub.enqueued) == sorted([job["id"], job2["id"]])

    r = c.get("/api/downloads")
    jobs = r.json()
    assert {j["id"] for j in jobs} == {job["id"], job2["id"]}
    # Newest first.
    assert jobs[0]["id"] > jobs[-1]["id"]

    r = c.get(f"/api/downloads/{job['id']}")
    assert r.status_code == 200 and r.json()["id"] == job["id"]

    r = c.get("/api/downloads", params={"status": "queued"})
    assert len(r.json()) == 2


async def _set_job_status(jid, status):
    import app.db as db_mod

    async with db_mod.async_session() as s:
        j = await s.get(db_mod.models.DownloadJob, jid)
        j.status = status
        await s.commit()


def test_pause_resume_cancel_retry_transitions(client, monkeypatch):
    c, stub, dl = client
    monkeypatch.setattr(dl.ytdlp, "probe", lambda url, cookiefile=None, **kw: {"title": "X"})

    r = c.post("/api/downloads", json={"url": "https://www.instagram.com/reel/Cabc123/"})
    jid = r.json()["id"]

    # pause while queued -> 409 (pause only when probing/downloading)
    r = c.post(f"/api/downloads/{jid}/pause")
    assert r.status_code == 409

    asyncio.run(_set_job_status(jid, "downloading"))
    r = c.post(f"/api/downloads/{jid}/pause")
    assert r.status_code == 200
    assert r.json()["status"] == "paused"
    assert stub.paused == [jid]

    # resume a paused job -> back to queued + re-enqueued
    r = c.post(f"/api/downloads/{jid}/resume")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert stub.enqueued.count(jid) == 2  # create + resume

    # cancel a queued job -> abort event + failed/cancelled marker
    r = c.post(f"/api/downloads/{jid}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed" and body["error"] == "cancelled"
    assert stub.cancelled == [jid]

    # retry resets failed -> queued, clears error
    r = c.post(f"/api/downloads/{jid}/retry")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued" and body["error"] is None

    # done jobs cannot be retried
    asyncio.run(_set_job_status(jid, "done"))
    r = c.post(f"/api/downloads/{jid}/retry")
    assert r.status_code == 409


def test_unknown_action_and_missing_job(client):
    c, _stub, _dl = client
    assert c.post("/api/downloads/999/frobnicate").status_code == 404
    assert c.get("/api/downloads/999").status_code == 404
    assert c.post("/api/downloads/999/pause").status_code == 404


def test_delete_job_removes_row_and_cancels_active(client, monkeypatch):
    c, stub, dl = client
    monkeypatch.setattr(dl.ytdlp, "probe", lambda url, cookiefile=None, **kw: {"title": "X"})

    r = c.post("/api/downloads", json={"url": "https://www.instagram.com/reel/Cabc123/"})
    jid = r.json()["id"]

    # active job -> aborted before the row goes away
    asyncio.run(_set_job_status(jid, "downloading"))
    assert c.delete(f"/api/downloads/{jid}").status_code == 204
    assert stub.cancelled == [jid]
    assert c.get(f"/api/downloads/{jid}").status_code == 404

    # terminal job -> deleted without touching the manager
    r = c.post("/api/downloads", json={"url": "https://www.instagram.com/reel/Cxyz789/"})
    jid2 = r.json()["id"]
    asyncio.run(_set_job_status(jid2, "done"))
    assert c.delete(f"/api/downloads/{jid2}").status_code == 204
    assert stub.cancelled == [jid]  # unchanged

    assert c.delete("/api/downloads/999").status_code == 404
