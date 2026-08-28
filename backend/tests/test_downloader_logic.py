"""DownloadManager logic tests — all ytdlp engine calls MOCKED, no network.

Covers: duplicate-skip, pause->resume state flow, space-floor gate +
hysteresis resume, redownload override.
"""

import asyncio
import threading
from datetime import timedelta

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


async def test_a_part_from_before_the_run_belongs_to_someone_else(mgr, db):
    """The output dir is per creator, and the live recorder writes there too.

    An auto-record and a queued download of the same room share a directory,
    so "the largest .part here" could be a capture ffmpeg was still writing --
    which this module would then rename out from under the recorder and file
    as the job's own output, or delete outright on cancel.
    """
    from app.services import downloader as dl, ytdlp as y

    job, add = make_job(db)
    await add()
    out = y.output_dir(job)
    out.mkdir(parents=True, exist_ok=True)
    recorder_capture = out / "live_20260828_100718.flv.part"
    recorder_capture.write_bytes(b"0" * 999)

    baseline = dl.part_snapshot(job)
    assert str(recorder_capture) in baseline
    # Not ours, however much bigger it is than anything we write.
    assert mgr._captured_part(job, baseline) is None
    mgr._cleanup_parts(job, baseline)
    assert recorder_capture.is_file()

    # What our own engine writes after the baseline is ours.
    mine = out / "T.mp4.part"
    mine.write_bytes(b"0" * 10)
    assert mgr._captured_part(job, baseline) == mine


async def test_engine_with_no_progress_hooks_still_leaves_probing(
    mgr, db, fake_engine, monkeypatch
):
    """An external downloader reports nothing until the capture is over.

    yt-dlp hands a LIVE m3u8 to FFmpegFD, and ExternalFD fires exactly one
    progress hook -- 'finished', after the stream ends. So a tiktok live sat
    in 'probing' for hours, no error and no progress, while ffmpeg was in fact
    writing. Bytes on disk are the signal the engine will not give us.
    """
    import time

    from app.services import downloader as dl, ytdlp as y
    import app.services.events as events

    monkeypatch.setattr(dl, "PART_PROBE_S", 0.01)
    job, add = make_job(db)
    jid = await add()

    def silent_live(opts, url):
        out = y.output_dir(job)
        out.mkdir(parents=True, exist_ok=True)
        (out / "T.mp4.part").write_bytes(b"0" * 32)
        time.sleep(0.3)  # capture underway; not one hook fired
        return FakeInfo(requested_downloads=[{"filepath": str(out / "T.mp4")}])

    fake_engine.download = silent_live

    seen = []
    events.subscribe(seen.append)
    try:
        await mgr.run_job(jid)
    finally:
        events.unsubscribe(seen.append)

    assert [e for e in seen if e.get("type") == "job.downloading"], (
        "job never left 'probing': " + str([e.get("type") for e in seen])
    )
    assert (await fetch(db, jid)).status == "done"


async def test_terminate_engine_children_stops_only_the_named_capture(tmp_path):
    """Several captures can be in flight at once, in the same directory.

    The temp filename is the only thing that tells them apart, so cancelling
    one job must not reach into another job's engine -- or the live recorder's.
    """
    import subprocess
    import sys

    from app.services import downloader as dl

    mine = tmp_path / "mine.mp4.part"
    theirs = tmp_path / "theirs.mp4.part"
    procs = [
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(f)])
        for f in (mine, theirs)
    ]
    try:
        assert dl.terminate_engine_children(mine) == 1
        procs[0].wait(timeout=10)
        assert procs[1].poll() is None  # the other capture is untouched
    finally:
        for proc in procs:
            proc.kill()


async def test_cancel_stops_an_engine_that_reports_no_progress(
    mgr, db, fake_engine, monkeypatch
):
    """Cancel used to be delivered only through a progress hook.

    An external downloader fires none while it runs, so cancelling a live HLS
    capture marked the row and left ffmpeg writing until the broadcast ended
    by itself -- and the bytes it kept writing were in a directory the live
    recorder shares.
    """
    import threading as _threading

    from app.services import downloader as dl, ytdlp as y
    import app.services.events as events

    monkeypatch.setattr(dl, "PART_PROBE_S", 0.01)
    job, add = make_job(db)
    jid = await add()

    stopped = _threading.Event()
    killed: list = []

    def fake_terminate(part):
        killed.append(part)
        stopped.set()
        return 1

    monkeypatch.setattr(dl, "terminate_engine_children", fake_terminate)

    def silent_live(opts, url):
        out = y.output_dir(job)
        out.mkdir(parents=True, exist_ok=True)
        (out / "T.mp4.part").write_bytes(b"0" * 32)
        if not stopped.wait(5):  # no hook to poll: only a kill ends this
            raise AssertionError("engine was never stopped")
        raise RuntimeError("ffmpeg exited with code 255")

    fake_engine.download = silent_live

    seen = []
    events.subscribe(seen.append)
    try:
        task = asyncio.create_task(mgr.run_job(jid))
        for _ in range(300):  # wait for the watchdog to name this job's .part
            await asyncio.sleep(0.01)
            if jid in mgr._job_parts:
                break
        assert jid in mgr._job_parts, "watchdog never found the engine's output"
        mgr.cancel(jid)
        await asyncio.wait_for(task, timeout=10)
    finally:
        events.unsubscribe(seen.append)

    assert killed == [y.output_dir(job) / "T.mp4.part"]
    job_row = await fetch(db, jid)
    assert job_row.status == "failed"
    # The engine's dying words must not surface as the reason.
    assert job_row.error == "cancelled"
    assert [e for e in seen if e.get("type") == "job.cancelled"]
    assert mgr._job_parts == {}


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


async def test_run_stamps_started_and_finished(mgr, db, fake_engine):
    """A completed run records both run timestamps."""
    _, add = make_job(db)
    jid = await add()
    await mgr.run_job(jid)
    job = await fetch(db, jid)
    assert job.started_at is not None
    assert job.finished_at is not None
    assert job.finished_at >= job.started_at


async def test_retry_refreshes_run_timestamps(mgr, db, fake_engine):
    """Pause clears finished_at; the next run re-stamps both."""
    _, add = make_job(db, status="paused")  # parked job, resume path
    jid = await add()
    await mgr.run_job(jid)  # first (resumed) run completes
    done1 = await fetch(db, jid)
    assert done1.finished_at is not None

    # Simulate a retry: back to paused, then run again.
    async with db.async_session() as s:
        job = await s.get(models.DownloadJob, jid)
        job.status = "paused"
        job.finished_at = models.utcnow() - timedelta(hours=1)
        await s.commit()
    await mgr.run_job(jid)
    job2 = await fetch(db, jid)
    assert job2.status == "done"
    assert job2.finished_at > done1.finished_at  # re-stamped, not stale


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



async def test_cookiefile_is_removed_when_build_opts_raises(mgr, db, fake_engine, monkeypatch):
    """The plaintext cookie file must not outlive a failed job.

    build_opts renders folder_template, so an unsupported placeholder raises
    between decrypting the cookies to /tmp and arming the cleanup. It used to
    sit above the try/finally, which stranded the file once per attempt with
    nobody left who knew the path.
    """
    import tempfile
    from pathlib import Path

    from app.routers import credentials as cred
    from app.services import ytdlp as y

    tmp_root = Path(tempfile.gettempdir())
    before = {p for pre in cred._COOKIEFILE_PREFIXES for p in tmp_root.glob(f"{pre}*.txt")}

    async def _fake_cookiefile(platform):
        f = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", prefix=f"{cred._ENGINE_PREFIX}{platform}_",
            delete=False, encoding="utf-8",
        )
        f.write("# Netscape HTTP Cookie File\n")
        f.close()
        return Path(f.name)

    monkeypatch.setattr(cred, "aget_cookiefile", _fake_cookiefile)

    def _boom(job, settings, *, extra=None):
        raise KeyError("title")

    monkeypatch.setattr(y, "build_opts", _boom)

    _, add = make_job(db)
    jid = await add()
    await mgr.run_job(jid)

    assert (await fetch(db, jid)).status == "failed"
    after = {p for pre in cred._COOKIEFILE_PREFIXES for p in tmp_root.glob(f"{pre}*.txt")}
    assert after == before, f"stranded plaintext cookie file(s): {after - before}"


async def test_cookiefile_is_removed_when_the_download_raises(mgr, db, fake_engine, monkeypatch):
    """Same guarantee on the path that already worked, so a refactor that
    re-hoists build_opts cannot pass by shrinking the try instead."""
    import tempfile
    from pathlib import Path

    from app.routers import credentials as cred
    from app.services import ytdlp as y

    tmp_root = Path(tempfile.gettempdir())
    before = {p for pre in cred._COOKIEFILE_PREFIXES for p in tmp_root.glob(f"{pre}*.txt")}

    async def _fake_cookiefile(platform):
        f = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", prefix=f"{cred._ENGINE_PREFIX}{platform}_",
            delete=False, encoding="utf-8",
        )
        f.write("# Netscape HTTP Cookie File\n")
        f.close()
        return Path(f.name)

    monkeypatch.setattr(cred, "aget_cookiefile", _fake_cookiefile)
    monkeypatch.setattr(y, "download", lambda opts, url: (_ for _ in ()).throw(RuntimeError("engine died")))

    _, add = make_job(db)
    jid = await add()
    await mgr.run_job(jid)

    assert (await fetch(db, jid)).status == "failed"
    after = {p for pre in cred._COOKIEFILE_PREFIXES for p in tmp_root.glob(f"{pre}*.txt")}
    assert after == before, f"stranded plaintext cookie file(s): {after - before}"
