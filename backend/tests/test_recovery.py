"""Orphan-recovery: claim filtering, candidates, remux+register flow."""

import asyncio
import importlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.services import recovery as rec


# ---- pure helpers ------------------------------------------------------------


def test_claimed_paths_covers_part_and_ytdl_siblings():
    jobs = [SimpleNamespace(output_path="/media/bili/c/live_1.mp4")]
    claimed = rec.claimed_paths(jobs, [])
    assert "/media/bili/c/live_1.mp4" in claimed
    assert "/media/bili/c/live_1.mp4.part" in claimed
    assert "/media/bili/c/live_1.mp4.ytdl" in claimed


def test_a_merged_download_claims_its_other_streams_too():
    """Claimed by the video stream's name, the job fails on the audio's."""
    jobs = [SimpleNamespace(output_path="/media/bili/c/T.f30112.mp4")]
    claimed = rec.claimed_paths(jobs, [])
    audio = "/media/bili/c/T.f30280.m4a.part"
    assert any(audio.startswith(c) for c in claimed)
    other = "/media/bili/c/Other.f30280.m4a.part"
    assert not any(other.startswith(c) for c in claimed)


def test_a_cancelled_job_claims_nothing():
    """Cancel gave the bytes up; hiding them from the sweep would strand them."""
    jobs = [SimpleNamespace(output_path="/media/bili/c/T.mp4", error="cancelled")]
    assert rec.claimed_paths(jobs, []) == set()


def test_orphan_candidates_finds_parts_only(tmp_path):
    (tmp_path / "a.flv.part").write_bytes(b"x")
    (tmp_path / "b.mp4").write_bytes(b"x")  # not a part
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "c.flv.part").write_bytes(b"x")
    assert {p.name for p in rec.orphan_candidates(tmp_path)} == {
        "a.flv.part",
        "c.flv.part",
    }


def test_recovered_name():
    p = Path("/m/live_x.flv.part")
    assert rec.recovered_name(p).name == "live_x_recovered.mp4"


# ---- sweep -------------------------------------------------------------------


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "_media_root", lambda: tmp_path)
    monkeypatch.setattr(rec, "ffmpeg", "echo-ffmpeg")
    # A database of its own. remux_and_register writes LibraryItems and reads
    # the space floor, and these tests leaned on whichever database an earlier
    # module had left initialised: run alone against an empty DATA_DIR, three
    # of them failed on "no such table" before a line of recovery ran.
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    import app.config as config
    import app.db as db_mod

    importlib.reload(config)
    importlib.reload(db_mod)
    asyncio.run(db_mod.init_db())
    yield tmp_path
    asyncio.run(db_mod.engine.dispose())


def _seed_recording(status: str, output_path: str | None):
    """Insert one LiveRecording row into a fresh isolated test DB."""
    from app.db import init_db
    from app.models import LiveRecording, utcnow

    async def _go():
        await init_db()
        import app.db as db_mod

        async with db_mod.async_session() as s:
            s.add(
                LiveRecording(
                    platform="bilibili",
                    room_url="https://live.bilibili.com/1",
                    creator="creator2",
                    origin="watchlist",
                    status=status,
                    output_path=output_path,
                    started_at=utcnow(),
                )
            )
            await s.commit()

    asyncio.run(_go())


def _seed_kept_flv(root: Path) -> tuple[Path, int]:
    """A finished recording the recorder kept as FLV, plus its library row."""
    import app.db as db_mod
    from app.models import LibraryItem, LiveRecording, utcnow

    flv = root / "bilibili" / "c" / "live_1.flv"
    flv.parent.mkdir(parents=True)
    flv.write_bytes(b"F" * 64)

    async def _go():
        async with db_mod.async_session() as s:
            item = LibraryItem(file_path=str(flv), thumbnail_path=None, platform="bilibili",
                               creator="c", title="Live", media_type="recording", size_bytes=64)
            s.add(item)
            s.add(LiveRecording(platform="bilibili", room_url="https://live.bilibili.com/1",
                                creator="c", origin="watchlist", status="finished",
                                output_path=str(flv), started_at=utcnow()))
            await s.commit()
            return item.id

    return flv, asyncio.run(_go())


def _paths(item_id):
    import app.db as db_mod
    from app.models import LibraryItem, LiveRecording

    async def _go():
        async with db_mod.async_session() as s:
            item = await s.get(LibraryItem, item_id)
            recs = (await s.execute(select(LiveRecording.output_path))).scalars().all()
            return item.file_path, list(recs)

    return asyncio.run(_go())


def _fake_remux(monkeypatch, calls):
    async def remux(src, dst):
        calls.append(src)
        dst.write_bytes(b"M" * 80)
        return True

    monkeypatch.setattr(rec, "remux_to_mp4", remux)


def test_a_kept_flv_becomes_an_mp4_once_there_is_room(media_root, monkeypatch):
    """Item 250 stayed an unplayable FLV for two days with room to spare."""
    flv, item_id = _seed_kept_flv(media_root)
    calls = []
    _fake_remux(monkeypatch, calls)
    monkeypatch.setattr(rec, "_timeline_s", lambda p: 10788.75)
    monkeypatch.setattr(rec.storage, "room_for_copy", _async_true)

    assert asyncio.run(rec.convert_kept_flvs()) == 1
    mp4 = flv.with_suffix(".mp4")
    assert mp4.is_file() and not flv.exists()
    assert _paths(item_id) == (str(mp4), [str(mp4)])


def test_a_kept_flv_waits_while_there_is_no_room(media_root, monkeypatch):
    flv, item_id = _seed_kept_flv(media_root)
    calls = []
    _fake_remux(monkeypatch, calls)

    async def no_room(size):
        return False

    monkeypatch.setattr(rec.storage, "room_for_copy", no_room)
    assert asyncio.run(rec.convert_kept_flvs()) == 0
    assert calls == [] and flv.is_file()
    assert _paths(item_id)[0] == str(flv)


def test_a_short_mp4_is_thrown_away_and_not_retried(media_root, monkeypatch):
    """The FLV is the only copy; it goes only for an MP4 of the same length."""
    flv, item_id = _seed_kept_flv(media_root)
    monkeypatch.setattr(rec, "_FLV_FAILED", set())
    calls = []
    _fake_remux(monkeypatch, calls)
    monkeypatch.setattr(rec, "_timeline_s", lambda p: 100.0 if p.suffix == ".flv" else 60.0)
    monkeypatch.setattr(rec.storage, "room_for_copy", _async_true)

    assert asyncio.run(rec.convert_kept_flvs()) == 0
    assert flv.is_file() and not flv.with_suffix(".mp4").exists()
    assert _paths(item_id)[0] == str(flv)
    asyncio.run(rec.convert_kept_flvs())
    assert len(calls) == 1, "retried a conversion that already failed"


def test_a_partial_mp4_from_a_cut_conversion_is_redone(media_root, monkeypatch):
    """A restart mid-remux leaves live_1.mp4 behind; it must not block the
    item forever."""
    flv, item_id = _seed_kept_flv(media_root)
    flv.with_suffix(".mp4").write_bytes(b"half")
    calls = []
    _fake_remux(monkeypatch, calls)
    monkeypatch.setattr(rec, "_timeline_s", lambda p: 50.0)
    monkeypatch.setattr(rec.storage, "room_for_copy", _async_true)

    assert asyncio.run(rec.convert_kept_flvs()) == 1
    assert calls == [flv]
    assert flv.with_suffix(".mp4").read_bytes() == b"M" * 80


def test_conversion_keeps_the_start_margin_free(media_root, monkeypatch):
    """Down to the bare floor, a capture running beside it gets stopped."""
    flv, _ = _seed_kept_flv(media_root)
    asked = []

    async def room(size):
        asked.append(size)
        return False

    monkeypatch.setattr(rec.storage, "disk_usage", lambda *a: SimpleNamespace(total=100_000))
    monkeypatch.setattr(rec.storage, "room_for_copy", room)
    asyncio.run(rec.convert_kept_flvs())
    assert asked == [64 + int(100_000 * rec.storage.RESUME_MARGIN_PCT / 100)]


def test_timeline_is_read_off_the_last_progress_line(monkeypatch, tmp_path):
    """Measured on item 250: header 02:57:26.63, packets to 02:59:48.75."""
    stderr = ("  Duration: 02:57:26.63, start: 0.004000, bitrate: 3399 kb/s\n"
              "frame=1 fps=0 time=00:00:01.00 bitrate=N/A\n"
              "frame=323665 fps=25159 time=02:59:48.75 bitrate=N/A speed=839x\n")
    monkeypatch.setattr(rec.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stderr=stderr, returncode=0))
    assert rec._timeline_s(tmp_path / "x.flv") == pytest.approx(10788.75)


async def _async_true(*_a):
    return True


def test_recovery_announces_what_it_rescued(media_root, monkeypatch):
    """A library item nobody started, with nothing in Activity explaining it,
    is indistinguishable from a bug. Recovery used to publish nothing."""
    root: Path = media_root
    d = root / "tiktok" / "someone"
    d.mkdir(parents=True)
    part = d / "live_x.flv.part"
    part.write_bytes(bytes(64))

    published: list[dict] = []
    monkeypatch.setattr(rec.events, "publish", published.append)
    monkeypatch.setattr(rec.subprocess, "run", _ok_ffmpeg)

    asyncio.run(rec.remux_and_register(part))

    assert [e["type"] for e in published] == ["recording.recovered"]
    assert published[0]["file"] == "live_x_recovered.mp4"
    assert published[0]["source"] == "live_x.flv.part"
    assert published[0]["creator"] == "someone"


def test_recovery_announces_a_failed_remux(media_root, monkeypatch):
    """A sweep failing every cycle must not look like a sweep never running."""
    root: Path = media_root
    d = root / "tiktok" / "someone"
    d.mkdir(parents=True)
    part = d / "live_y.flv.part"
    part.write_bytes(bytes(64))

    published: list[dict] = []
    monkeypatch.setattr(rec.events, "publish", published.append)
    monkeypatch.setattr(
        rec.subprocess, "run", lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="")
    )

    assert asyncio.run(rec.remux_and_register(part)) is None

    assert [e["type"] for e in published] == ["recording.recover_failed"]
    assert published[0]["ffmpeg_rc"] == 1
    assert part.exists()  # source kept for the next sweep


def test_recovery_waits_when_the_copy_would_breach_the_floor(media_root, monkeypatch):
    """The remux writes a whole second copy before the source goes; at the
    floor that copy is what fills the volume. The orphan can wait."""
    from app.services import storage

    root: Path = media_root
    d = root / "tiktok" / "someone"
    d.mkdir(parents=True)
    part = d / "live_z.flv.part"
    part.write_bytes(bytes(64))
    monkeypatch.setattr(
        storage,
        "disk_usage",
        lambda path=None: storage.DiskUsage(total=100 * 1024**3, free=4 * 1024**3),
    )
    ran: list = []
    monkeypatch.setattr(rec.subprocess, "run", lambda cmd, **kw: ran.append(cmd))
    published: list[dict] = []
    monkeypatch.setattr(rec.events, "publish", published.append)

    assert asyncio.run(rec.remux_and_register(part)) is None

    assert ran == []  # ffmpeg never started
    assert published == []  # and no activity row per sweep while short
    assert part.exists()  # still an orphan for the next sweep


def _ok_ffmpeg(cmd, **kw):
    """subprocess.run stand-in: ffprobe answers nothing, ffmpeg writes output."""
    if cmd[0].endswith("ffprobe"):
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    Path(cmd[-1]).write_bytes(b"MP4fake")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_a_partial_recovery_is_retried_not_treated_as_done(media_root, monkeypatch):
    """A remux cut off partway must not strand the orphan for good.

    The rescue that finishes deletes its source, so a recovered file sitting
    NEXT TO the .part it came from is debris, not a result. Reading it as
    "already recovered" made every later sweep take the same early exit, and
    the real capture stayed a .part forever -- nothing else on the system
    clears either file. Restarting during a rescue is how you get one.
    """
    root: Path = media_root
    d = root / "tiktok" / "someone"
    d.mkdir(parents=True)
    part = d / "live_x.flv.part"
    part.write_bytes(bytes(4096))
    partial = rec.recovered_name(part)
    partial.write_bytes(b"tr")  # debris of the attempt that was cut off

    monkeypatch.setattr(rec.subprocess, "run", _ok_ffmpeg)

    item = asyncio.run(rec.remux_and_register(part))

    assert item is not None, "refused to retry a partial recovery"
    assert partial.read_bytes() == b"MP4fake"  # redone, not left as debris
    assert not part.exists()  # source consumed by the completed rescue


def test_a_failed_cleanup_still_registers_and_is_not_redone(media_root, monkeypatch):
    """Deleting the source is the last step, and it used to be able to undo
    the whole rescue.

    unlink(missing_ok=True) forgives only a file that is already gone; a
    read-only disk or a permissions problem still raises, and it raised
    BEFORE the LibraryItem was written. That left a good recovered file
    unregistered and sitting next to its .part -- exactly the shape that now
    reads as an interrupted attempt, so every later sweep threw the good file
    away and re-ran the whole remux. Forever, on a file that can be a
    gigabyte.
    """
    root: Path = media_root
    d = root / "tiktok" / "someone"
    d.mkdir(parents=True)
    part = d / "live_z.flv.part"
    part.write_bytes(bytes(4096))
    final = rec.recovered_name(part)

    monkeypatch.setattr(rec.subprocess, "run", _ok_ffmpeg)

    def _cannot_delete(self, missing_ok=False):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "unlink", _cannot_delete)

    item = asyncio.run(rec.remux_and_register(part))

    assert item is not None, "a failed cleanup cost the registration"
    assert final.is_file()
    assert part.exists()  # could not be removed, and that is survivable

    # A later sweep sees both files, but the library row says it is done.
    monkeypatch.undo()
    monkeypatch.setattr(rec.subprocess, "run", _boom_ffmpeg)
    assert asyncio.run(rec.remux_and_register(part)) is None
    assert final.is_file(), "threw away a capture already in the library"


def _boom_ffmpeg(cmd, **kw):
    raise AssertionError("re-remuxed a capture already in the library")


def test_an_already_recovered_orphan_is_not_redone(media_root, monkeypatch):
    """The other half of the rule: a rescue that finished stays finished."""
    root: Path = media_root
    d = root / "tiktok" / "someone"
    d.mkdir(parents=True)
    part = d / "live_y.flv.part"
    # No .part on disk -- the finished rescue consumed it.
    rec.recovered_name(part).write_bytes(b"MP4fake")

    def _boom(cmd, **kw):
        raise AssertionError("remuxed an orphan that was already recovered")

    monkeypatch.setattr(rec.subprocess, "run", _boom)
    assert asyncio.run(rec.remux_and_register(part)) is None


def test_sweep_recovers_orphan_skips_active(media_root, monkeypatch):
    root: Path = media_root
    orphan_dir = root / "bili" / "creator"
    active_dir = root / "bili" / "creator2"
    for d in (orphan_dir, active_dir):
        d.mkdir(parents=True)

    orphan = orphan_dir / "live_old.flv.part"
    orphan.write_bytes(b"\x00" * 64)
    growing = active_dir / "live_now.flv.part"
    growing.write_bytes(b"\x00" * 32)

    _seed_recording("recording", str(active_dir / "live_now.flv"))

    # Stability gate passes instantly; capture ffmpeg invocations.
    async def instant_true(_path):
        return True

    monkeypatch.setattr(rec, "_size_stable", instant_true)
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        Path(cmd[-1]).write_bytes(b"FLVfake")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(rec.subprocess, "run", fake_run)

    n = asyncio.run(rec.recovery.sweep_once())
    assert n == 1
    assert (orphan_dir / "live_old_recovered.mp4").is_file()
    assert not orphan.exists()  # source dropped after successful remux
    assert growing.exists()  # claimed by the active recording → untouched
    # Two invocations now: mp4_copy_args ffprobes the source for its codec
    # before the copy, and both run through the same patched subprocess.run.
    assert len([c for c in cmds if c[0] == rec.ffmpeg]) == 1

    # Row registered exactly once; second sweep is a no-op.
    from sqlalchemy import select

    from app.models import LibraryItem

    async def _count():
        import app.db as db_mod

        async with db_mod.async_session() as s:
            return len(
                (
                    await s.execute(
                        select(LibraryItem).where(
                            LibraryItem.file_path == str(orphan_dir / "live_old_recovered.mp4")
                        )
                    )
                )
                .scalars()
                .all()
            )

    assert asyncio.run(_count()) == 1
    assert asyncio.run(rec.recovery.sweep_once()) == 0


def _seed_job(status: str, output_path: str | None):
    """Insert one DownloadJob row into the fresh isolated test DB."""
    from app.db import init_db
    from app.models import DownloadJob

    async def _go():
        await init_db()
        import app.db as db_mod

        async with db_mod.async_session() as s:
            s.add(
                DownloadJob(
                    url="https://www.tiktok.com/@someone/live",
                    platform="tiktok",
                    kind="video",
                    title="T",
                    creator="someone",
                    status=status,
                    output_path=output_path,
                )
            )
            await s.commit()

    asyncio.run(_go())


@pytest.mark.parametrize("status", ["paused", "failed"])
def test_a_resumable_jobs_capture_is_not_collected_as_an_orphan(
    media_root, monkeypatch, status
):
    """Pause keeps the .part on disk so Resume can continue it, and a failed
    job keeps it so Retry can.

    The sweep only shielded queued/probing/downloading jobs, so a paused one
    claimed nothing: its capture was collected as abandoned, remuxed away and
    its source deleted, leaving Resume with nothing to continue from. Caught
    with 1.9 GB of paused live captures one sweep away from exactly that.
    Failed jobs stayed exposed until job 19 lost its 63 MB .part that way.
    """
    root: Path = media_root
    paused_dir = root / "tiktok" / "someone"
    orphan_dir = root / "tiktok" / "nobody"
    for d in (paused_dir, orphan_dir):
        d.mkdir(parents=True)

    resumable = paused_dir / "T.mp4.part"
    resumable.write_bytes(bytes(4096))
    orphan = orphan_dir / "live_gone.flv.part"
    orphan.write_bytes(bytes(64))

    # The job claims the name it is writing, minus the .part suffix.
    _seed_job(status, str(paused_dir / "T.mp4"))

    async def instant_true(_path):
        return True

    monkeypatch.setattr(rec, "_size_stable", instant_true)
    monkeypatch.setattr(rec.subprocess, "run", _ok_ffmpeg)

    n = asyncio.run(rec.recovery.sweep_once())

    assert n == 1  # the real orphan, and only it
    assert resumable.is_file(), f"swept a {status} job's capture out from under it"
    assert not (paused_dir / "T_recovered.mp4").exists()
    assert not orphan.exists()  # genuinely abandoned, so collected as before


def test_sweep_leaves_growing_file_alone(media_root, monkeypatch):
    """A .part whose size still changes is never remuxed."""
    root: Path = media_root
    p = root / "b" / "c"
    p.mkdir(parents=True)
    f = p / "x.flv.part"
    f.write_bytes(b"\x00" * 8)

    async def not_stable(_path):
        return False

    monkeypatch.setattr(rec, "_size_stable", not_stable)
    ran = []

    monkeypatch.setattr(
        rec.subprocess,
        "run",
        lambda cmd, **kw: ran.append(cmd) or SimpleNamespace(returncode=0),
    )

    assert asyncio.run(rec.recovery.sweep_once()) == 0
    assert ran == []
    assert f.exists()
