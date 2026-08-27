"""Orphaned-capture auto-recovery.

A live recording killed mid-flight (connection drop, container restart,
engine crash) leaves `<name>.flv.part` on disk with no LibraryItem and no
job that will ever finish it. This service sweeps MEDIA_ROOT for such
orphans, remuxes them with ffmpeg (-c copy, truncation-tolerant) into a
playable file next to the original, and registers the LibraryItem.

Safety: a .part is only an orphan when NO active download job or live
recording claims it (active engines write .part continuously; remuxing one
would race the writer). Files still growing are also left alone — size must
be stable across two checks. Runs at boot and every SWEEP_INTERVAL_S.
"""

import asyncio
import logging
import subprocess
import time
from pathlib import Path

from sqlalchemy import select

from app import models
from app.services import events
from app.services.recorder import mp4_copy_args
from app.services.ytdlp import _sanitize

log = logging.getLogger(__name__)

SWEEP_INTERVAL_S = 600  # 10 min
# Size must be identical after this long before we consider the file dead.
STABLE_WINDOW_S = 90
ffmpeg = "ffmpeg"


def _db():
    """Module-reload-safe DB handle (same pattern as recorder.py)."""
    import app.db

    return app.db.async_session()


def claimed_paths(job_rows, rec_rows) -> set[str]:
    """Filesystem prefixes active jobs/recordings are writing right now."""
    claimed: set[str] = set()
    for j in job_rows:
        if j.output_path:
            claimed.add(j.output_path)
    for r in rec_rows:
        if r.output_path:
            claimed.add(r.output_path)
    # Active engine writes <final>.part / <final>.ytdl next to these.
    return {c + suffix for c in claimed for suffix in ("", ".part", ".ytdl")}


def orphan_candidates(root: Path) -> list[Path]:
    """Every *.part under MEDIA_ROOT (caller filters against active work)."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.part") if p.is_file() and p.suffix == ".part")


def recovered_name(part: Path) -> Path:
    """live_x.flv.part -> live_x_recovered.mp4.

    Lands in MP4, not FLV: an orphan is usually a TikTok capture, and TikTok
    serves its top tier as HEVC-in-FLV -- which ffmpeg can read but VLC cannot
    demux, so a rescued .flv opened to no picture and no sound. Same bytes,
    container players understand.
    """
    base = part.with_suffix("")  # drop .part -> live_x.flv
    return base.with_name(f"{base.stem}_recovered.mp4")


async def _size_stable(path: Path) -> bool:
    """True when file size unchanged across STABLE_WINDOW_S (writer is gone)."""

    def _sizes():
        try:
            s1 = path.stat().st_size
        except OSError:
            return None
        time.sleep(STABLE_WINDOW_S)
        try:
            return path.stat().st_size == s1
        except OSError:
            return False

    return await asyncio.to_thread(_sizes)


async def remux_and_register(part: Path) -> models.LibraryItem | None:
    """ffmpeg -c copy the orphan into *_recovered.mp4; register LibraryItem.

    Returns None when nothing was produced or the row already existed.
    """
    final = recovered_name(part)
    if final.exists() or not part.exists():
        return None

    # ponytail: blocking ffmpeg via to_thread instead of exec — output is
    # unbounded but bounded by disk; switch to create_subprocess_exec with
    # DEVNULL if this ever needs cancellation.
    def _run_ffmpeg() -> int:
        return subprocess.run(
            [
                ffmpeg,
                "-y",
                "-err_detect",
                "ignore_err",
                "-i",
                str(part),
                *mp4_copy_args(part),
                str(final),
            ],
            capture_output=True,
            timeout=3600,
        ).returncode

    rc = await asyncio.to_thread(_run_ffmpeg)
    if rc != 0 or not final.exists() or final.stat().st_size == 0:
        # Remux failed: keep the .part so data isn't lost; retry next sweep.
        final.unlink(missing_ok=True)
        log.warning("recovery remux failed (%s) for %s", rc, part)
        # Say so. A sweep that silently fails every cycle is indistinguishable
        # from one that never ran.
        events.publish(
            {"type": "recording.recover_failed", "file": part.name, "ffmpeg_rc": rc}
        )
        return None

    # Drop the source only once a playable copy exists.
    part.unlink(missing_ok=True)

    # platform/creator from the path shape MEDIA_ROOT/<platform>/<creator>/...
    rel = part.parent.relative_to(_media_root())
    parts = rel.parts
    platform = _sanitize(parts[-2]) if len(parts) >= 2 else "unknown"
    creator = _sanitize(parts[-1]) if len(parts) >= 1 else "unknown"

    async with _db() as session:
        dup = await session.execute(
            select(models.LibraryItem).where(models.LibraryItem.file_path == str(final))
        )
        if dup.scalar_one_or_none():
            return None
        item = models.LibraryItem(
            file_path=str(final),
            thumbnail_path=_find_thumb(final),
            platform=platform,
            creator=creator,
            title=f"{creator} (recovered)",
            media_type="recording",
            size_bytes=final.stat().st_size,
        )
        session.add(item)
        await session.commit()
    log.info("recovered orphan %s -> %s", part.name, final.name)
    # Recovery used to register a library item and publish nothing, so a file
    # nobody started appeared in the library with no way to find out why.
    events.publish(
        {
            "type": "recording.recovered",
            "file": final.name,
            "source": part.name,
            "size_bytes": item.size_bytes,
            "creator": creator,
        }
    )
    return item


def _media_root() -> Path:
    from app.config import MEDIA_ROOT

    return MEDIA_ROOT


_THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _find_thumb(media: Path) -> str | None:
    for ext in _THUMB_EXTS:
        cand = media.with_suffix(ext)
        if cand.exists():
            return str(cand)
    return None


class RecoveryService:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.sweep_once()
            except Exception:
                log.exception("orphan recovery sweep failed")
            await asyncio.sleep(SWEEP_INTERVAL_S)

    async def sweep_once(self) -> int:
        """One sweep; returns how many orphans were recovered."""
        root = _media_root()
        candidates = [p for p in orphan_candidates(root)]
        if not candidates:
            return 0

        async with _db() as session:
            jobs = (
                (
                    await session.execute(
                        select(models.DownloadJob).where(
                            models.DownloadJob.status.in_(("queued", "probing", "downloading"))
                        )
                    )
                )
                .scalars()
                .all()
            )
            recs = (
                (
                    await session.execute(
                        select(models.LiveRecording).where(
                            models.LiveRecording.status.in_(("recording", "starting"))
                        )
                    )
                )
                .scalars()
                .all()
            )

        claimed = claimed_paths(jobs, recs)
        recovered = 0
        for p in candidates:
            # Skip anything an active engine might be writing, including
            # sibling temp fragments.
            if str(p) in claimed or any(str(p).startswith(c) for c in claimed):
                continue
            if not await _size_stable(p):
                continue  # still being written
            if await remux_and_register(p):
                recovered += 1
        return recovered


recovery = RecoveryService()


import time  # noqa: E402  (used inside _size_stable's thread closure)
