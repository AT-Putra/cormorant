"""Recorder supervisor tests (US-010) — ALL subprocess spawning mocked.

No real yt-dlp/streamlink invocations: engine processes are fakes whose
wait()/returncode are scripted; the boot live-probe is monkeypatched.
"""

import asyncio
import importlib
import signal
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app import models
from app.services import recorder as rec_mod
from app.services.recorder import (
    RecorderSupervisor,
    _signal_group as _real_signal_group,
    engine_chain,
    output_filename,
    recording_output_path,
)


# ---- fixtures ----------------------------------------------------------------


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """Temp DATA_DIR + MEDIA_ROOT via config reload (mirrors test_downloader_logic)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))
    import app.config as config

    importlib.reload(config)
    import app.db as db_mod

    importlib.reload(db_mod)
    await db_mod.init_db()
    yield db_mod
    await db_mod.engine.dispose()


class FakeProc:
    """Scripted asyncio.subprocess.Process stand-in."""

    def __init__(self, pid: int = 4242, exit_code: int = 0, delay: float = 0.0):
        self.pid = pid
        self._exit_code = exit_code
        self._delay = delay
        self.returncode = None
        self.signals: list[int] = []
        # Optional hook: called when SIGTERM arrives (real engines die on it).
        self.on_signal = None

    def send_signal(self, sig):
        self.signals.append(sig)
        if self.on_signal:
            self.on_signal(sig)

    async def wait(self):
        # Poll _delay so on_signal / simulated kills can shorten the run
        # mid-flight (a plain long sleep would be uninterruptible).
        while self._delay > 0:
            await asyncio.sleep(min(self._delay, 0.05))
        self.returncode = self._exit_code
        return self.returncode


@pytest.fixture(autouse=True)
def no_real_spawn(monkeypatch):
    """Belt-and-braces: any unmocked create_subprocess_exec fails loudly."""

    async def boom(*a, **k):
        raise AssertionError("real subprocess spawn attempted in tests")

    monkeypatch.setattr(rec_mod.asyncio, "create_subprocess_exec", boom)


@pytest.fixture(autouse=True)
def no_real_signals(monkeypatch):
    """The other half of no_real_spawn: FakeProc is not a process.

    _signal_group's POSIX branch hands proc.pid to os.killpg, so under Linux
    every stop test aimed a real syscall at a pid that had never existed --
    ProcessLookupError, the fake never saw its SIGTERM, and its scripted delay
    ran on. That is a stop path tested only on Windows, and on Linux (which is
    what production runs) a hang rather than a failure. Deliver to the
    stand-in instead; the real group semantics are covered by
    test_signal_group_signals_the_whole_group_on_posix.
    """

    monkeypatch.setattr(
        rec_mod, "_signal_group", lambda proc, sig: proc.send_signal(sig)
    )


@pytest.fixture(autouse=True)
def fake_remux(monkeypatch):
    """No ffmpeg in tests, same rule as no_real_spawn.

    Copies the bytes rather than returning a bare True, so a test can tell a
    finalized capture from a file that was never written. The failure path has
    its own test that overrides this.
    """

    async def _remux(src, dst):
        if not src.exists():
            return False
        dst.write_bytes(src.read_bytes())
        return True

    monkeypatch.setattr(rec_mod, "remux_to_mp4", _remux)


@pytest.fixture
async def sup():
    s = RecorderSupervisor()
    yield s
    # shutdown() gathers the supervision tasks, and a failed assertion can
    # leave a stand-in "running" for its full scripted delay -- which is how
    # one broken stop test cost a CI runner five minutes instead of failing in
    # five seconds. The test has already failed by then; do not also hang.
    try:
        await asyncio.wait_for(s.shutdown(), timeout=10)
    except asyncio.TimeoutError:
        pass


def make_recording(db, **kw):
    defaults = dict(
        room_url="https://live.bilibili.com/123",
        platform="bilibili",
        creator="c1",
        origin="manual",
        status="recording",
    )
    defaults.update(kw)

    async def _add() -> int:
        async with db.async_session() as s:
            r = models.LiveRecording(**defaults)
            s.add(r)
            await s.commit()
            return r.id

    return _add


async def fetch(db, rec_id):
    async with db.async_session() as s:
        return await s.get(models.LiveRecording, rec_id)


def script_spawns(monkeypatch, procs):
    """Queue FakeProcs returned per spawn call; record spawn argvs."""
    spawned: list[list[str]] = []

    async def fake_exec(*cmd, **kwargs):
        spawned.append(list(cmd))
        return procs.pop(0)

    monkeypatch.setattr(rec_mod.asyncio, "create_subprocess_exec", fake_exec)
    return spawned


def _media_root() -> Path:
    from app.config import MEDIA_ROOT

    return Path(MEDIA_ROOT)


def pin_out(monkeypatch, name: str) -> Path:
    """Pin the supervisor's output path into tmp MEDIA_ROOT."""
    out = _media_root() / name
    monkeypatch.setattr(rec_mod, "recording_output_path", lambda *a: out)
    return out


# ---- engine chain + filename ---------------------------------------------------


def test_engine_chain_ytdlp_first_streamlink_fallback():
    chain = engine_chain("https://live.bilibili.com/1", "/tmp/out.mp4")
    assert len(chain) == 2
    ytdlp_cmd, sl_cmd = chain
    assert "yt_dlp" in ytdlp_cmd and "--quiet" in ytdlp_cmd
    assert "-o" in ytdlp_cmd and "/tmp/out.mp4" in ytdlp_cmd
    assert sl_cmd[0] == "streamlink"
    assert sl_cmd[-3:] == ["best", "-o", "/tmp/out.mp4"]
    # Join-point capture only: never DVR backfill.
    all_args = [a for cmd in chain for a in cmd]
    assert "--live-from-start" not in all_args


def test_engine_chain_without_cookies_passes_no_cookie_flags():
    all_args = [a for cmd in engine_chain("https://live.bilibili.com/1", "/tmp/o.mp4") for a in cmd]
    assert "--cookies" not in all_args
    assert "--http-cookies-file" not in all_args


def test_engine_chain_hands_cookies_to_both_engines():
    """Anonymous capture can silently land on a lower tier for gated rooms,
    so a stored credential must reach the fallback engine too."""
    ytdlp_cmd, sl_cmd = engine_chain(
        "https://live.bilibili.com/1", "/tmp/o.mp4", "/tmp/ck.txt"
    )
    assert ytdlp_cmd[ytdlp_cmd.index("--cookies") + 1] == "/tmp/ck.txt"
    assert sl_cmd[sl_cmd.index("--http-cookies-file") + 1] == "/tmp/ck.txt"
    # Cookie flags must not displace the trailing url/quality/output shape.
    assert ytdlp_cmd[-3:] == ["https://live.bilibili.com/1", "-o", "/tmp/o.mp4"]
    assert sl_cmd[-3:] == ["best", "-o", "/tmp/o.mp4"]


def test_filename_embeds_started_at_no_collision():
    t1 = datetime(2026, 8, 24, 9, 30, 0)
    p1 = recording_output_path("bilibili", "c1", t1)
    p2 = recording_output_path("bilibili", "c1", t1 + timedelta(minutes=5))
    assert p1 != p2
    assert output_filename(t1) == "live_20260824_093000.mp4"
    assert p1.parts[-3:] == ("bilibili", "c1", "live_20260824_093000.mp4")


async def test_two_recordings_same_room_different_paths(sup, db, monkeypatch):
    """Re-records of one room never collide (started_at in filename)."""
    outs = []

    async def fake_exec(*cmd, **kwargs):
        argv = list(cmd)
        out = Path(argv[argv.index("-o") + 1])
        outs.append(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 32)  # engine produced output
        proc = FakeProc(exit_code=0)
        proc.returncode = 0
        return proc

    monkeypatch.setattr(rec_mod.asyncio, "create_subprocess_exec", fake_exec)

    stamps = (datetime(2026, 8, 24, 9, 30, 0), datetime(2026, 8, 24, 9, 35, 0))
    for url, ts in zip(
        ("https://live.bilibili.com/1", "https://live.bilibili.com/1"), stamps
    ):
        rid = await make_recording(db, room_url=url, started_at=ts)()
        await asyncio.wait_for(sup.start_recording(rid), timeout=5)

    assert len(outs) == 2
    assert outs[0] != outs[1]
    # Each filename carries its row's own started_at stamp.
    async with db.async_session() as s:
        rows = (await s.execute(select(models.LiveRecording))).scalars().all()
    assert {Path(r.output_path).name for r in rows} == {
        output_filename(r.started_at) for r in rows
    }


async def test_capture_lands_in_flv_and_finalizes_to_mp4(sup, db, monkeypatch):
    """The engine is handed .flv; the library gets .mp4.

    A cut MP4 has no moov atom and will not open, so the bytes must land in
    FLV -- but the engines write the raw stream, so shipping that file as
    .mp4 produced FLV bytes with an .mp4 name and duration 0.
    """
    out = pin_out(monkeypatch, "cap.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    targets: list[str] = []
    script_spawns(monkeypatch, [FakeProc(exit_code=0)])
    # After script_spawns, or this binds the no_real_spawn guard instead.
    real_spawn = rec_mod.asyncio.create_subprocess_exec

    async def spawn_touch(*cmd, **kwargs):
        argv = list(cmd)
        targets.append(argv[argv.index("-o") + 1])
        out.with_suffix(".flv").write_bytes(b"0" * 64)
        return await real_spawn(*cmd, **kwargs)

    monkeypatch.setattr(rec_mod.asyncio, "create_subprocess_exec", spawn_touch)

    rid = await make_recording(db)()
    await asyncio.wait_for(sup.start_recording(rid), timeout=5)

    assert all(t.endswith(".flv") for t in targets)
    assert out.is_file()  # finalized
    assert not out.with_suffix(".flv").exists()  # capture dropped after remux
    rec = await fetch(db, rid)
    assert rec.output_path == str(out)


async def test_failed_remux_keeps_and_registers_the_capture(sup, db, monkeypatch):
    """Losing the container is not a reason to lose the recording."""
    out = pin_out(monkeypatch, "keep.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)

    async def _fails(src, dst):
        return False

    monkeypatch.setattr(rec_mod, "remux_to_mp4", _fails)
    script_spawns(monkeypatch, [FakeProc(exit_code=0)])
    real_spawn = rec_mod.asyncio.create_subprocess_exec

    async def spawn_touch(*cmd, **kwargs):
        out.with_suffix(".flv").write_bytes(b"0" * 64)
        return await real_spawn(*cmd, **kwargs)

    monkeypatch.setattr(rec_mod.asyncio, "create_subprocess_exec", spawn_touch)

    rid = await make_recording(db)()
    await asyncio.wait_for(sup.start_recording(rid), timeout=5)

    rec = await fetch(db, rid)
    assert rec.status == "finished"
    assert rec.output_path == str(out.with_suffix(".flv"))
    assert out.with_suffix(".flv").is_file()


async def test_capture_path_is_claimed_before_the_first_byte(sup, db, monkeypatch):
    """recovery reads active recordings' output_path to decide what is an
    orphan. While that stayed NULL until finalize, a running capture claimed
    nothing and the sweep remuxed the file out from under its own engine."""
    out = pin_out(monkeypatch, "claim.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    seen: list[str | None] = []
    script_spawns(monkeypatch, [FakeProc(exit_code=0)])
    real_spawn = rec_mod.asyncio.create_subprocess_exec

    async def spawn_touch(*cmd, **kwargs):
        seen.append((await fetch(db, rid)).output_path)
        out.with_suffix(".flv").write_bytes(b"0" * 32)
        return await real_spawn(*cmd, **kwargs)

    monkeypatch.setattr(rec_mod.asyncio, "create_subprocess_exec", spawn_touch)

    rid = await make_recording(db)()
    await asyncio.wait_for(sup.start_recording(rid), timeout=5)

    # Claimed as the FLV the engine actually writes, not the finalized name.
    assert seen == [str(out.with_suffix(".flv"))]
    # ...and replaced by the finalized path once the capture is done.
    assert (await fetch(db, rid)).output_path == str(out)


def test_mp4_copy_args_tags_hevc_and_leaves_h264_alone(monkeypatch, tmp_path):
    """hev1 is the mp4 muxer's default and the reason a fine HEVC file plays
    as a black frame; hvc1 on an H.264 stream would break one that worked."""
    src = tmp_path / "c.flv"
    src.write_bytes(b"x")

    monkeypatch.setattr(rec_mod, "_video_codec", lambda p: "hevc")
    assert rec_mod.mp4_copy_args(src)[-2:] == ["-tag:v", "hvc1"]

    monkeypatch.setattr(rec_mod, "_video_codec", lambda p: "h264")
    assert "-tag:v" not in rec_mod.mp4_copy_args(src)

    # ffprobe missing or silent: copy anyway rather than refusing to finalize.
    monkeypatch.setattr(rec_mod, "_video_codec", lambda p: "")
    assert "-tag:v" not in rec_mod.mp4_copy_args(src)


async def _all_rec_ids(db):
    async with db.async_session() as s:
        rows = (await s.execute(select(models.LiveRecording))).scalars().all()
    return [r.id for r in rows]


# ---- supervision flow ------------------------------------------------------------


async def test_supervise_success_marks_finished_writes_library(sup, db, monkeypatch):
    out = pin_out(monkeypatch, "x.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    spawned = script_spawns(monkeypatch, [FakeProc(exit_code=0)])

    rid = await make_recording(db)()

    # Materialize the "capture" when the engine spawns (before finalize checks).
    real_spawn = rec_mod.asyncio.create_subprocess_exec

    async def spawn_touch(*cmd, **kwargs):
        out.with_suffix(".flv").write_bytes(b"0" * 128)
        return await real_spawn(*cmd, **kwargs)

    monkeypatch.setattr(rec_mod.asyncio, "create_subprocess_exec", spawn_touch)

    await asyncio.wait_for(sup.start_recording(rid), timeout=5)

    rec = await fetch(db, rid)
    assert rec.status == "finished"
    assert rec.output_path == str(out)
    assert rec.ended_at is not None
    assert len(spawned) == 1
    async with db.async_session() as s:
        items = (await s.execute(select(models.LibraryItem))).scalars().all()
    assert len(items) == 1
    assert items[0].media_type == "recording"
    assert items[0].file_path == str(out)


async def test_supervise_zero_byte_output_is_failed(sup, db, monkeypatch):
    out = pin_out(monkeypatch, "empty.mp4")
    script_spawns(monkeypatch, [FakeProc(exit_code=0)])  # exits ok, wrote nothing
    out.parent.mkdir(parents=True, exist_ok=True)

    rid = await make_recording(db)()
    await asyncio.wait_for(sup.start_recording(rid), timeout=5)

    rec = await fetch(db, rid)
    assert rec.status == "failed"


async def test_supervise_fallback_to_streamlink_on_failure(sup, db, monkeypatch):
    out = pin_out(monkeypatch, "y.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    procs = [FakeProc(exit_code=1), FakeProc(exit_code=0)]
    spawned = script_spawns(monkeypatch, procs)

    rid = await make_recording(db)()
    # Second engine "produces" the file before its wait returns.
    second_spawn = rec_mod.asyncio.create_subprocess_exec

    async def spawn_touch(*cmd, **kwargs):
        if cmd[0] == "streamlink":
            out.with_suffix(".flv").write_bytes(b"1" * 64)
        return await second_spawn(*cmd, **kwargs)

    monkeypatch.setattr(rec_mod.asyncio, "create_subprocess_exec", spawn_touch)

    await asyncio.wait_for(sup.start_recording(rid), timeout=5)

    assert [c[0] for c in spawned] == [rec_mod.sys.executable, "streamlink"]
    rec = await fetch(db, rid)
    assert rec.status == "finished"


async def test_supervise_all_engines_fail_marks_failed(sup, db, monkeypatch):
    pin_out(monkeypatch, "z.mp4")
    spawned = script_spawns(
        monkeypatch, [FakeProc(exit_code=2), FakeProc(exit_code=3)]
    )

    rid = await make_recording(db)()
    await asyncio.wait_for(sup.start_recording(rid), timeout=5)

    assert len(spawned) == 2  # exactly one streamlink retry
    rec = await fetch(db, rid)
    assert rec.status == "failed"
    assert rec.error
    assert "streamlink" in rec.error  # last engine's failure recorded


# ---- stop path ---------------------------------------------------------------


async def test_stop_sigterm_then_kill_tree_marks_ended(sup, db, monkeypatch):
    # SIGKILLed engines die with -9; the fake honors the mock kill by ending.
    long_proc = FakeProc(pid=4242, exit_code=-9, delay=999)
    pin_out(monkeypatch, "stopped.mp4")
    script_spawns(monkeypatch, [long_proc])
    kills: list[int] = []

    def fake_kill(pid):
        kills.append(pid)
        long_proc._delay = 0.0  # simulate the tree dying

    monkeypatch.setattr(rec_mod, "_kill_tree", fake_kill)
    sup.grace_s = 0.3  # shrink grace window for the test

    rid = await make_recording(db)()
    task = sup.start_recording(rid)
    await asyncio.sleep(0.05)  # let supervise register the proc
    assert await sup.stop(rid) is True
    await asyncio.wait_for(asyncio.shield(task), timeout=5)

    assert long_proc.signals.count(signal.SIGTERM) == 1
    assert kills == [long_proc.pid]  # grace expired -> tree killed
    rec = await fetch(db, rid)
    assert rec.status == "ended"
    assert rec.error is None


async def test_stop_graceful_exit_within_window_no_kill(sup, db, monkeypatch):
    proc = FakeProc(delay=999)
    # A well-behaved engine dies promptly on SIGTERM with code 0.
    proc.on_signal = lambda sig: setattr(proc, "_delay", 0.0)
    pin_out(monkeypatch, "graceful.mp4")
    script_spawns(monkeypatch, [proc])

    def must_not_kill(pid):
        raise AssertionError("kill_tree must not fire on graceful exit")

    monkeypatch.setattr(rec_mod, "_kill_tree", must_not_kill)
    sup.grace_s = 5.0

    rid = await make_recording(db)()
    task = sup.start_recording(rid)
    await asyncio.sleep(0.05)
    assert await sup.stop(rid) is True
    await asyncio.wait_for(asyncio.shield(task), timeout=5)

    assert proc.signals.count(signal.SIGTERM) == 1
    assert proc.returncode == 0
    rec = await fetch(db, rid)
    assert rec.status == "ended"  # intended stop, not 'finished'/'failed'


def test_signal_group_signals_the_whole_group_on_posix(monkeypatch):
    """The real _signal_group, which no_real_signals replaces everywhere else.

    A recording's engine leads its own session, and the child holding the
    output file is often not the pid we spawned (yt-dlp shells out to ffmpeg),
    so a stop that reaches only proc.pid leaves the writer alive.
    """
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(rec_mod.os, "name", "posix")
    # raising=False: these two do not exist on the Windows dev box at all.
    monkeypatch.setattr(rec_mod.os, "getpgid", lambda pid: 900 + pid, raising=False)
    monkeypatch.setattr(
        rec_mod.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)), raising=False
    )

    _real_signal_group(FakeProc(pid=7), signal.SIGTERM)

    assert sent == [(907, signal.SIGTERM)]


def test_signal_group_falls_back_to_the_handle_off_posix(monkeypatch):
    """No process groups on Windows; the handle is all there is."""
    monkeypatch.setattr(rec_mod.os, "name", "nt")
    proc = FakeProc(pid=7)

    _real_signal_group(proc, signal.SIGTERM)

    assert proc.signals == [signal.SIGTERM]


async def test_stop_between_engines_does_not_spawn_fallback(sup, db, monkeypatch):
    first = FakeProc(delay=999)
    # Dies on SIGTERM with failure code (as a SIGTERMed engine would).
    def die(sig):
        first._delay = 0.0
        first._exit_code = 1

    first.on_signal = die
    pin_out(monkeypatch, "mid.mp4")
    script_spawns(monkeypatch, [first])  # only ONE proc queued: spawning the
    # fallback would pop from an empty list and fail the test loudly.
    sup.grace_s = 5.0

    rid = await make_recording(db)()
    task = sup.start_recording(rid)
    await asyncio.sleep(0.05)
    assert await sup.stop(rid) is True
    await asyncio.wait_for(asyncio.shield(task), timeout=5)
    rec = await fetch(db, rid)
    assert rec.status == "ended"  # no AssertionError from unqueued spawn


# ---- reconcile_on_boot -----------------------------------------------------------


async def test_reconcile_watchlist_still_live_retriggers(sup, db, monkeypatch):
    rid = await make_recording(db, origin="watchlist")()
    monkeypatch.setattr(rec_mod, "probe_is_live", lambda url, cookiefile=None: True)
    retriggered = []

    async def fake_begin(room_url, platform, creator, *, origin):
        retriggered.append((room_url, platform, creator, origin))
        r = models.LiveRecording(
            room_url=room_url, platform=platform, creator=creator,
            origin=origin, status="recording",
        )
        async with db.async_session() as s:
            s.add(r)
            await s.commit()
            return r

    monkeypatch.setattr(rec_mod, "begin_recording", fake_begin)
    await sup.reconcile_on_boot()

    old = await fetch(db, rid)
    assert old.status == "interrupted"
    assert old.error
    assert retriggered == [
        ("https://live.bilibili.com/123", "bilibili", "c1", "watchlist")
    ]


async def test_reconcile_watchlist_offline_marks_ended(sup, db, monkeypatch):
    rid = await make_recording(db, origin="watchlist")()
    monkeypatch.setattr(rec_mod, "probe_is_live", lambda url, cookiefile=None: False)
    begin_calls = []

    async def fake_begin(*a, **k):
        begin_calls.append((a, k))

    monkeypatch.setattr(rec_mod, "begin_recording", fake_begin)
    await sup.reconcile_on_boot()

    old = await fetch(db, rid)
    assert old.status == "ended"
    assert old.ended_at is not None
    assert begin_calls == []


async def test_reconcile_manual_interrupted_no_auto_rerecord(sup, db, monkeypatch):
    rid = await make_recording(db, origin="manual")()
    probe_calls = []
    monkeypatch.setattr(
        rec_mod, "probe_is_live", lambda url, cookiefile=None: probe_calls.append(url) or True
    )
    begin_calls = []

    async def fake_begin(*a, **k):
        begin_calls.append((a, k))

    monkeypatch.setattr(rec_mod, "begin_recording", fake_begin)
    await sup.reconcile_on_boot()

    old = await fetch(db, rid)
    assert old.status == "interrupted"
    assert probe_calls == []  # manual rows are never probed
    assert begin_calls == []  # and never auto re-triggered


async def test_reconcile_leaves_terminal_rows_alone(sup, db, monkeypatch):
    done_id = await make_recording(db, status="finished")()
    failed_id = await make_recording(db, status="failed")()
    monkeypatch.setattr(rec_mod, "probe_is_live", lambda url, cookiefile=None: True)
    await sup.reconcile_on_boot()
    assert (await fetch(db, done_id)).status == "finished"
    assert (await fetch(db, failed_id)).status == "failed"


# ---- API endpoints -----------------------------------------------------------------


@pytest.fixture
def client(authed_client):
    """API tests ride authed_client's own fresh DB (conftest.current_db)."""
    from app.routers import recordings as rr

    c, stub = authed_client
    yield c, rr


def api_db():
    from tests.conftest import current_db

    return current_db()


async def test_record_live_endpoint(client, monkeypatch):
    c, rr = client
    calls = []

    class R:
        id = 77
        room_url = "https://live.bilibili.com/9"
        platform = "bilibili"
        creator = ""
        origin = "manual"
        status = "recording"
        started_at = datetime(2026, 8, 24, 12, 0, 0)
        ended_at = None
        output_path = None
        error = None

    async def fake_begin(url, platform, creator, *, origin):
        calls.append((url, platform, creator, origin))
        return R()

    monkeypatch.setattr(rr, "begin_recording", fake_begin)
    r = c.post("/api/downloads/record-live", json={"url": R.room_url})
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == 77 and body["status"] == "recording"
    assert body["platform"] == "bilibili"
    assert calls == [(R.room_url, "bilibili", "", "manual")]


async def test_record_live_bad_url_400(client):
    c, _rr = client
    r = c.post("/api/downloads/record-live", json={"url": "https://example.com/live"})
    assert r.status_code == 400


async def test_list_recordings(client):
    c, _rr = client
    await make_recording(api_db(), status="finished")()
    r = c.get("/api/recordings")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "finished"
    assert rows[0]["origin"] == "manual"


async def test_retry_only_interrupted(client, monkeypatch):
    c, rr = client
    adb = api_db()
    bad_id = await make_recording(adb, status="failed")()
    assert c.post(f"/api/recordings/{bad_id}/retry").status_code == 409

    good_id = await make_recording(adb, status="interrupted")()

    class R:
        id = 555
        room_url = "u"
        platform = "p"
        creator = "c"
        origin = "manual"
        status = "recording"
        started_at = datetime(2026, 8, 24)

    async def fake_begin(url, platform, creator, *, origin):
        return R()

    monkeypatch.setattr(rr, "begin_recording", fake_begin)
    r = c.post(f"/api/recordings/{good_id}/retry")
    assert r.status_code == 200
    assert r.json()["retried_from"] == good_id
    assert r.json()["id"] == 555


async def test_stop_endpoint_invokes_supervisor(client, monkeypatch):
    c, rr = client
    rid = await make_recording(api_db())()
    stopped = []

    class StubRec:
        async def stop(self, recording_id):
            stopped.append(recording_id)
            return True

    monkeypatch.setattr(rr, "get_recorder", lambda: StubRec())
    r = c.post(f"/api/recordings/{rid}/stop")
    assert r.status_code == 200
    assert stopped == [rid]

    missing = c.post("/api/recordings/99999/stop")
    assert missing.status_code == 404


async def test_stop_endpoint_409_when_no_process(client, monkeypatch):
    c, rr = client
    rid = await make_recording(api_db())()

    class StubRec:
        async def stop(self, recording_id):
            return False  # nothing registered

    monkeypatch.setattr(rr, "get_recorder", lambda: StubRec())
    assert c.post(f"/api/recordings/{rid}/stop").status_code == 409


# ---- live capture quality cap --------------------------------------------------


def test_engine_chain_caps_quality_in_both_lanes():
    """default_quality was ignored for live captures: engine_chain never read
    settings, so a room offering 4K was recorded at 4K whatever the cap said."""
    ytdlp_cmd, sl_cmd = engine_chain(
        "https://live.bilibili.com/1", "/tmp/o.mp4", None, "1080p"
    )
    assert ytdlp_cmd[ytdlp_cmd.index("-S") + 1] == "res:1080"
    assert sl_cmd[-3:] == ["1080p,best", "-o", "/tmp/o.mp4"]


def test_engine_chain_quality_cap_is_a_sort_never_a_filter():
    """These rooms are vertical: a `height<=1080` FILTER reads 1080x1920 as
    1920, drops the whole 1080p AND 720p ladder and lands on 480x852. `res`
    sorts on the smaller dimension. And streamlink gets a preference LIST, so
    a plugin that does not name streams by resolution falls back to best
    rather than failing to find the stream and recording nothing."""
    ytdlp_cmd, sl_cmd = engine_chain(
        "https://live.bilibili.com/1", "/tmp/o.mp4", None, "720p"
    )
    joined = " ".join(ytdlp_cmd)
    assert "height" not in joined
    assert "-f" not in ytdlp_cmd  # a format filter would hard-drop the ladder
    assert sl_cmd[-3] == "720p,best"
    assert sl_cmd[-3].endswith(",best")


def test_engine_chain_best_and_unset_add_no_cap():
    """'best' is the default; it must stay byte-identical to the old command
    so the cap cannot regress captures for anyone who never set one."""
    for quality in ("best", None):
        ytdlp_cmd, sl_cmd = engine_chain(
            "https://live.bilibili.com/1", "/tmp/o.mp4", None, quality
        )
        assert "-S" not in ytdlp_cmd
        assert sl_cmd[-3:] == ["best", "-o", "/tmp/o.mp4"]


def test_engine_chain_cap_survives_alongside_cookies():
    ytdlp_cmd, sl_cmd = engine_chain(
        "https://live.bilibili.com/1", "/tmp/o.mp4", "/tmp/ck.txt", "1440p"
    )
    assert ytdlp_cmd[ytdlp_cmd.index("--cookies") + 1] == "/tmp/ck.txt"
    assert ytdlp_cmd[ytdlp_cmd.index("-S") + 1] == "res:1440"
    assert sl_cmd[sl_cmd.index("--http-cookies-file") + 1] == "/tmp/ck.txt"
    # Trailing url/quality/output shape is what the engines actually parse.
    assert ytdlp_cmd[-3:] == ["https://live.bilibili.com/1", "-o", "/tmp/o.mp4"]
    assert sl_cmd[-3:] == ["1440p,best", "-o", "/tmp/o.mp4"]


def test_every_quality_choice_maps_to_a_usable_cap():
    """QUALITY_CHOICES is the dropdown. A value that produced a bogus -S or a
    stream name streamlink cannot parse would fail only at capture time."""
    from app.services.ytdlp import QUALITY_CHOICES

    for quality in QUALITY_CHOICES:
        ytdlp_cmd, sl_cmd = engine_chain("https://live.bilibili.com/1", "/o", None, quality)
        if quality == "best":
            assert "-S" not in ytdlp_cmd
            continue
        assert ytdlp_cmd[ytdlp_cmd.index("-S") + 1] == f"res:{quality.removesuffix('p')}"
        assert sl_cmd[sl_cmd.index("-o") - 1] == f"{quality},best"


async def test_supervise_passes_the_saved_quality_into_the_engine_command(
    sup, db, monkeypatch
):
    """engine_chain can cap all it likes if nothing hands it the setting.

    Reads default_quality at capture start rather than at watch creation: a
    recording row can sit queued across a settings change, and the tier that
    matters is the one wanted when the bytes actually land.
    """
    from app.services.settings_store import save_settings

    async with db.async_session() as s:
        await save_settings(s, {"default_quality": "720p"})

    out = pin_out(monkeypatch, "capped.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    spawned = script_spawns(monkeypatch, [FakeProc(exit_code=0)])

    real_spawn = rec_mod.asyncio.create_subprocess_exec

    async def spawn_touch(*cmd, **kwargs):
        out.write_bytes(b"0" * 128)
        return await real_spawn(*cmd, **kwargs)

    monkeypatch.setattr(rec_mod.asyncio, "create_subprocess_exec", spawn_touch)

    rid = await make_recording(db)()
    await asyncio.wait_for(sup.start_recording(rid), timeout=5)

    assert len(spawned) == 1
    cmd = list(spawned[0])
    assert cmd[cmd.index("-S") + 1] == "res:720"
