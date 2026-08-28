"""DownloadManager logic tests — all ytdlp engine calls MOCKED, no network.

Covers: duplicate-skip, pause->resume state flow, space-floor gate +
hysteresis resume, redownload override.
"""

import asyncio
import threading
import time
from datetime import timedelta

import pytest

from app import models
from app.services import downloader as dl
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
    """Match ffmpeg's real argument form, and only the named capture.

    yt-dlp does not hand ffmpeg a bare path: _ffmpeg_filename_argument turns
    /media/x/live.mp4.part into file:/media/x/live.mp4.part, because a path
    containing ':' would otherwise read as a protocol. Comparing raw argv
    against the path matched nothing, so cancel was inert against the exact
    engine it exists to stop. Several captures also share a directory, so the
    match still has to be exact -- cancelling one job must never reach into
    another job's engine, or the live recorder's.
    """
    import subprocess
    import sys

    from app.services import downloader as dl

    mine = tmp_path / "mine.mp4.part"
    theirs = tmp_path / "theirs.mp4.part"
    sleeper = "import time; time.sleep(30)"
    # As ffmpeg is really invoked, plus a bare-path engine for good measure.
    ffmpeg_style = subprocess.Popen([sys.executable, "-c", sleeper, f"file:{mine}"])
    bare_style = subprocess.Popen([sys.executable, "-c", sleeper, str(mine)])
    other_job = subprocess.Popen([sys.executable, "-c", sleeper, f"file:{theirs}"])
    procs = [ffmpeg_style, bare_style, other_job]
    try:
        assert dl.terminate_engine_children(mine) == 2
        ffmpeg_style.wait(timeout=10)
        bare_style.wait(timeout=10)
        assert other_job.poll() is None  # the other capture is untouched
    finally:
        for proc in procs:
            proc.kill()


def test_arg_path_unwraps_only_ffmpegs_url_form(tmp_path):
    """A prefix strip must not corrupt paths that never had one."""
    from app.services.downloader import _arg_path

    assert _arg_path("file:/media/x/live.mp4.part") == "/media/x/live.mp4.part"
    assert _arg_path("/media/x/live.mp4.part") == "/media/x/live.mp4.part"
    assert _arg_path("-i") == "-i"
    # A path that merely contains the word is left alone.
    assert _arg_path("/media/profile:1/x.part") == "/media/profile:1/x.part"


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


async def test_a_resumed_run_still_owns_the_part_it_wrote(mgr, db):
    """part_snapshot is taken at the start of EVERY run.

    So on a resumed or reconnected attempt the job's own .part from the last
    attempt is already on disk, and a raw baseline disowned it -- which cut
    the live reconnect chain to one retry and left a capture that ended after
    a drop sitting on disk unfiled.
    """
    from app.services import downloader as dl, ytdlp as y

    job, add = make_job(db)
    await add()
    out = y.output_dir(job)
    out.mkdir(parents=True, exist_ok=True)
    mine = out / "T.mp4.part"
    mine.write_bytes(b"0" * 64)  # left by the previous attempt
    recorder_capture = out / "live_20260828_100718.flv.part"
    recorder_capture.write_bytes(b"0" * 999)

    # The resumed run sees BOTH files as pre-existing.
    baseline = dl.part_snapshot(job)
    assert {str(mine), str(recorder_capture)} <= baseline

    # Without a claim it owns neither -- this is the state that regressed.
    assert mgr._captured_part(job, baseline) is None

    # With the claim carried over from the previous attempt, it owns its own
    # file again, and still not the recorder's larger one.
    mgr._job_parts[job.id] = mine
    assert mgr._captured_part(job, baseline) == mine

    # Cancelling a resumed job discards its own bytes and nobody else's.
    mgr._cleanup_parts(job, baseline)
    assert not mine.exists()
    assert recorder_capture.is_file()


async def test_the_claim_survives_a_requeue_but_not_a_finished_run(
    mgr, db, fake_engine, monkeypatch
):
    """A re-queued retry resumes onto the same .part, so the claim has to
    outlive that run; a settled job must start the next one fresh."""
    from app.services import downloader as dl, ytdlp as y

    monkeypatch.setattr(dl, "PART_PROBE_S", 0.01)
    job, add = make_job(db)
    jid = await add()
    out = y.output_dir(job)

    def drops_midway(opts, url):
        out.mkdir(parents=True, exist_ok=True)
        (out / "T.mp4.part").write_bytes(b"0" * 32)
        time.sleep(0.15)
        raise RuntimeError("Connection reset by peer")

    fake_engine.download = drops_midway
    await mgr.run_job(jid)

    # Re-queued for another attempt, and still owns what it wrote.
    assert (await fetch(db, jid)).status == "queued"
    assert mgr._job_parts.get(jid) == out / "T.mp4.part"

    # Now let it finish; the claim is released.
    def finishes(opts, url):
        for hook in opts.get("progress_hooks", []):
            hook({"status": "downloading", "downloaded_bytes": 9, "total_bytes": 10})
        return FakeInfo(requested_downloads=[{"filepath": str(out / "T.mp4")}])

    fake_engine.download = finishes
    await mgr.run_job(jid)
    assert (await fetch(db, jid)).status == "done"
    assert jid not in mgr._job_parts


async def test_progress_comes_from_disk_when_the_engine_reports_none(
    mgr, db, fake_engine, monkeypatch
):
    """Size and rate for an engine that publishes neither.

    Everything the Queue shows about a running job comes from progress hooks,
    and FFmpegFD -- which is what yt-dlp hands a LIVE m3u8 -- fires exactly
    one, after the capture ends. A tiktok live therefore sat at 0 MB with no
    rate for its whole run while ffmpeg wrote gigabytes to disk.
    """
    from app.services import downloader as dl, ytdlp as y
    import app.services.events as events

    monkeypatch.setattr(dl, "PART_PROBE_S", 0.01)
    job, add = make_job(db)
    jid = await add()

    def silent_live(opts, url):
        out = y.output_dir(job)
        out.mkdir(parents=True, exist_ok=True)
        part = out / "T.mp4.part"
        for n in range(1, 6):  # a capture growing on disk, silent to the app
            part.write_bytes(b"0" * (n * 50_000))
            time.sleep(0.05)
        return FakeInfo(requested_downloads=[{"filepath": str(out / "T.mp4")}])

    fake_engine.download = silent_live

    seen = []
    events.subscribe(seen.append)
    try:
        await mgr.run_job(jid)
    finally:
        events.unsubscribe(seen.append)

    prog = [e for e in seen if e.get("type") == "job.progress"]
    assert prog, "nothing reported for a hook-silent engine"
    assert max(e["downloaded_bytes"] for e in prog) >= 50_000
    assert any(e.get("speed") for e in prog), "size but no rate"


async def test_a_reporting_engine_keeps_its_own_numbers(
    mgr, db, fake_engine, monkeypatch
):
    """Two sources of truth would fight, so the disk reader stands down.

    yt-dlp's rate is instantaneous where the disk one is an average over the
    poll window; interleaving them would make the panel jitter between two
    different measurements of the same download.
    """
    from app.services import downloader as dl, ytdlp as y
    import app.services.events as events

    monkeypatch.setattr(dl, "PART_PROBE_S", 0.01)
    job, add = make_job(db)
    jid = await add()

    def hooked(opts, url):
        out = y.output_dir(job)
        out.mkdir(parents=True, exist_ok=True)
        (out / "T.mp4.part").write_bytes(b"0" * 5000)
        for hook in opts.get("progress_hooks", []):
            hook({"status": "downloading", "downloaded_bytes": 5000,
                  "total_bytes": 10000})
        time.sleep(0.25)  # ~25 poll windows for a reader that did not stop
        return FakeInfo(requested_downloads=[{"filepath": str(out / "T.mp4")}])

    fake_engine.download = hooked

    seen = []
    events.subscribe(seen.append)
    try:
        await mgr.run_job(jid)
    finally:
        events.unsubscribe(seen.append)

    # Hook payloads carry total_bytes; the disk reader has no total to give.
    from_disk = [
        e for e in seen
        if e.get("type") == "job.progress" and "total_bytes" not in e
    ]
    # At most one: the reader can sample once before the consumer thread has
    # drained the first hook and recorded that this engine reports for itself.
    assert len(from_disk) <= 1, f"disk reader kept talking over the engine: {len(from_disk)}"
    assert jid not in mgr._hook_seen  # released when the run settles


async def test_part_helpers_follow_a_custom_folder_template(mgr, db):
    """The engine writes under folder_template; the helpers must look there.

    build_opts renders the setting, but part_snapshot, _captured_part and
    _cleanup_parts all called output_dir(job) with no settings and so read the
    default {platform}/{creator}. With a custom template every one of them
    looked at an empty directory: no size reported, no output_path claimed --
    which is what shields a paused job's capture from the sweep -- no
    stream-over finalize, no reconnect, and cancel deleting nothing.
    """
    from app.services import ytdlp as y

    settings = {"folder_template": "archive/{creator}"}
    job, add = make_job(db)
    await add()

    real_dir = y.output_dir(job, settings)
    default_dir = y.output_dir(job)
    assert real_dir != default_dir  # the template actually moves it
    real_dir.mkdir(parents=True, exist_ok=True)
    part = real_dir / "T.mp4.part"
    part.write_bytes(b"0" * 4096)

    # Blind to the template, all three of these miss the file entirely.
    assert dl.part_snapshot(job, settings) == {str(part)}
    assert mgr._captured_part(job, set(), settings) == part

    mgr._cleanup_parts(job, set(), settings)
    assert not part.exists(), "cancel left the capture behind"


async def test_the_first_rate_reading_has_nothing_to_subtract_from(
    mgr, db, fake_engine, monkeypatch
):
    """Seeding prev_size at 0 made the first rate the whole file over one poll
    window, so a capture already well underway reported a wildly overstated
    speed for one tick.

    A file that never grows is the clean way to see it: every reading has a
    zero delta, so a correct implementation reports a size and no rate at all.
    Before the fix the first reading alone claimed 5 MB per window.
    """
    from app.services import ytdlp as y
    import app.services.events as events

    monkeypatch.setattr(dl, "PART_PROBE_S", 0.01)
    job, add = make_job(db)
    jid = await add()

    def already_large(opts, url):
        out = y.output_dir(job)
        out.mkdir(parents=True, exist_ok=True)
        (out / "T.mp4.part").write_bytes(b"0" * 5_000_000)  # written once
        time.sleep(0.12)  # several poll windows, no growth
        return FakeInfo(requested_downloads=[{"filepath": str(out / "T.mp4")}])

    fake_engine.download = already_large

    seen = []
    events.subscribe(seen.append)
    try:
        await mgr.run_job(jid)
    finally:
        events.unsubscribe(seen.append)

    prog = [e for e in seen if e.get("type") == "job.progress"]
    assert prog, "no progress reported"
    assert all(e["downloaded_bytes"] == 5_000_000 for e in prog)
    assert all(e["speed"] is None for e in prog), "invented a rate from no growth"


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
