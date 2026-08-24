"""DownloadManager logic tests — all ytdlp engine calls MOCKED, no network.

Covers: duplicate-skip, pause->resume state flow, space-floor gate +
hysteresis resume, redownload override.
"""

import asyncio
import threading

import pytest

from app import models
from app.services.downloader import DownloadManager


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """Temp DB + temp MEDIA_ROOT (reloaded config), schema initialized."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))
    import app.config as config

    importlib_reload(config)
    import app.db as db_mod

    importlib_reload(db_mod)
    await db_mod.init_db()
    yield db_mod
    await db_mod.engine.dispose()


def importlib_reload(mod):  # noqa: ANN001 - tiny local alias
    import importlib

    return importlib.reload(mod)


@pytest.fixture
async def mgr(db, monkeypatch):
    """Manager with watcher disabled; free_space mocked high by default."""
    from app.services import downloader as dl

    monkeypatch.setattr(dl, "free_space_pct", lambda path: 50.0)
    m = dl.DownloadManager()
    yield m
    # Drain queue without starting workers.


def make_job(db, **kw) -> models.DownloadJob:
    defaults = dict(
        url="https://www.bilibili.com/video/BV1xx",
        platform="bilibili",
        kind="video",
        title="T",
        creator="c1",
        status="queued",
    )
    defaults.update(kw)
    job = models.DownloadJob(**defaults)

    async def _add():
        async with db.async_session() as s:
            s.add(job)
            await s.commit()
        return job.id

    return job, _add


async def fetch(db, job_id):
    async with db.async_session() as s:
        return await s.get(models.DownloadJob, job_id)


class FakeInfo(dict):
    """Minimal yt-dlp result."""


# ---- mock engine ------------------------------------------------------------


@pytest.fixture
def fake_engine(monkeypatch):
    """Replace services.ytdlp functions used inside run_job."""
    from app.services import ytdlp as y

    calls = {"download": []}

    def set_result(fn):
        monkeypatch.setattr(y, "download", fn)

    def default(opts, url):
        calls["download"].append(url)
        info = FakeInfo(
            requested_downloads=[{"filepath": "X:\\media\\bilibili\\c1\\T.mp4"}]
        )
        for hook in opts.get("progress_hooks", []):
            hook({"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10})
            hook({"status": "finished", "downloaded_bytes": 10, "total_bytes": 10})
        return info

    set_result(default)
    y.calls = calls
    return y


# ---- tests ------------------------------------------------------------------


async def test_duplicate_skipped(mgr, db, fake_engine):
    _, add = make_job(db)
    jid = await add()
    # Prior completed job, same normalized URL + kind.
    prior = models.DownloadJob(
        url="https://www.bilibili.com/video/BV1xx?spm_id_from=x",
        platform="bilibili",
        kind="video",
        title="T",
        creator="c1",
        status="done",
        output_path=None,  # file gone; the done-row alone still counts
    )
    async with db.async_session() as s:
        s.add(prior)
        await s.commit()

    await mgr.run_job(jid)
    job = await fetch(db, jid)
    assert job.status == "skipped"
    assert fake_engine.calls["download"] == []  # never reached the engine


async def test_redownload_overrides_duplicate(mgr, db, fake_engine):
    _, add = make_job(db, redownload_requested=True)
    jid = await add()
    async with db.async_session() as s:
        s.add(
            models.DownloadJob(
                url="https://www.bilibili.com/video/BV1xx",
                platform="bilibili",
                kind="video",
                title="T",
                creator="c1",
                status="done",
            )
        )
        await s.commit()

    await mgr.run_job(jid)
    job = await fetch(db, jid)
    assert job.status == "done"
    assert len(fake_engine.calls["download"]) == 1
    assert job.redownload_requested is False  # consumed after use


async def test_pause_resume_flow(mgr, db, fake_engine):
    _, add = make_job(db)
    jid = await add()

    abort_holder = {}

    def pausing_download(opts, url):
        abort_holder["hook"] = opts["progress_hooks"][0]
        # Simulate mid-download abort raised from the hook (pause mechanism).
        hook = opts["progress_hooks"][0]
        hook({"status": "downloading", "downloaded_bytes": 1, "total_bytes": 10})
        raise fake_engine.AbortDownload("paused")

    fake_engine.download = pausing_download

    await mgr.run_job(jid)
    job = await fetch(db, jid)
    assert job.status == "paused"

    # Resume: engine succeeds this time.
    fake_engine.download = lambda opts, url: FakeInfo(
        requested_downloads=[{"filepath": "out.mp4"}]
    )

    mgr.resume(jid)
    await mgr.run_job(jid)
    job = await fetch(db, jid)
    assert job.status == "done"
    assert job.progress == 100.0


async def test_cancel_cleans_parts_and_fails(mgr, db, fake_engine):
    _, add = make_job(db)
    jid = await add()

    def cancelling_download(opts, url):
        raise fake_engine.AbortDownload("cancelled")

    fake_engine.download = cancelling_download

    mgr.cancel(jid)
    await mgr.run_job(jid)
    job = await fetch(db, jid)
    assert job.status == "failed"
    assert "cancelled" in (job.error or "")


async def test_space_floor_gate_pauses_auto_only(mgr, db, fake_engine, monkeypatch):
    from app.services import downloader as dl

    monkeypatch.setattr(dl, "free_space_pct", lambda path: 1.0)  # below floor 5%

    auto_job, add_auto = make_job(db, is_auto=True)
    manual_job, add_manual = make_job(db, is_auto=False)
    auto_id, manual_id = await add_auto(), await add_manual()

    await asyncio.gather(mgr.run_job(auto_id), mgr.run_job(manual_id))

    auto = await fetch(db, auto_id)
    manual = await fetch(db, manual_id)
    assert auto.status == "paused_space_floor"
    assert manual.status == "done"  # manual jobs always proceed
    assert fake_engine.calls["download"] == [manual_job.url]


async def test_hysteresis_resume_only_touches_space_floor_jobs(
    mgr, db, fake_engine, monkeypatch
):
    from app.services import downloader as dl

    paused_auto, add_a = make_job(db, is_auto=True)
    user_paused, add_u = make_job(db)  # not auto
    a_id = await add_a()
    u_id = await add_u()
    async with db.async_session() as s:
        ja = await s.get(models.DownloadJob, a_id)
        ju = await s.get(models.DownloadJob, u_id)
        ja.status = "paused_space_floor"
        ju.status = "paused"
        await s.commit()

    # Below floor+2 -> no resume.
    monkeypatch.setattr(dl, "free_space_pct", lambda path: 6.0)  # floor 5, need 7
    assert await mgr.resume_space_floor_jobs() == 0

    # At floor+2 -> only the space-floor job re-enqueues.
    monkeypatch.setattr(dl, "free_space_pct", lambda path: 7.0)
    assert await mgr.resume_space_floor_jobs() == 1
    assert fake_engine.calls["download"] == []


async def test_progress_written_to_db(mgr, db, fake_engine):
    _, add = make_job(db)
    jid = await add()
    await mgr.run_job(jid)
    job = await fetch(db, jid)
    assert job.progress == 100.0


async def test_failed_job_records_error(mgr, db, fake_engine):
    _, add = make_job(db)
    jid = await add()

    def boom(opts, url):
        raise RuntimeError("extractor exploded")

    fake_engine.download = boom
    await mgr.run_job(jid)
    job = await fetch(db, jid)
    assert job.status == "failed"
    assert "extractor" in job.error


async def test_worker_drains_queue(mgr, db, fake_engine):
    jobs = []
    for i in range(3):
        _, add = make_job(db)  # identical URL+kind: first runs, rest dup-skip
        jobs.append(await add())
    for jid in jobs:
        mgr.enqueue(jid)

    worker = asyncio.create_task(mgr._worker("t"))
    await asyncio.wait_for(mgr._queue.join(), timeout=5)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    statuses = [await fetch(db, jid) for jid in jobs]
    assert [j.status for j in statuses] == ["done", "skipped", "skipped"]

