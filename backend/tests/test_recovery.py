"""Orphan-recovery: claim filtering, candidates, remux+register flow."""

import asyncio
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import recovery as rec


# ---- pure helpers ------------------------------------------------------------


def test_claimed_paths_covers_part_and_ytdl_siblings():
    jobs = [SimpleNamespace(output_path="/media/bili/c/live_1.mp4")]
    claimed = rec.claimed_paths(jobs, [])
    assert "/media/bili/c/live_1.mp4" in claimed
    assert "/media/bili/c/live_1.mp4.part" in claimed
    assert "/media/bili/c/live_1.mp4.ytdl" in claimed


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
    return tmp_path


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


def test_a_paused_jobs_capture_is_not_collected_as_an_orphan(
    media_root, monkeypatch
):
    """Pause keeps the .part on disk so Resume can continue it.

    The sweep only shielded queued/probing/downloading jobs, so a paused one
    claimed nothing: its capture was collected as abandoned, remuxed away and
    its source deleted, leaving Resume with nothing to continue from. Caught
    with 1.9 GB of paused live captures one sweep away from exactly that.
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
    _seed_job("paused", str(paused_dir / "T.mp4"))

    async def instant_true(_path):
        return True

    monkeypatch.setattr(rec, "_size_stable", instant_true)
    monkeypatch.setattr(rec.subprocess, "run", _ok_ffmpeg)

    n = asyncio.run(rec.recovery.sweep_once())

    assert n == 1  # the real orphan, and only it
    assert resumable.is_file(), "swept a paused job's capture out from under it"
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
