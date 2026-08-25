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
    assert rec.recovered_name(p).name == "live_x_recovered.flv"


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
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(rec.subprocess, "run", fake_run)

    n = asyncio.run(rec.recovery.sweep_once())
    assert n == 1
    assert (orphan_dir / "live_old_recovered.flv").is_file()
    assert not orphan.exists()  # source dropped after successful remux
    assert growing.exists()  # claimed by the active recording → untouched
    assert len(cmds) == 1

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
                            LibraryItem.file_path == str(orphan_dir / "live_old_recovered.flv")
                        )
                    )
                )
                .scalars()
                .all()
            )

    assert asyncio.run(_count()) == 1
    assert asyncio.run(rec.recovery.sweep_once()) == 0


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
