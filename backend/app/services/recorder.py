"""Live recorder supervisor (US-010 / plan step 16).

Engine chain is DETERMINISTIC: the yt-dlp live extractor runs FIRST (native
bilibili/douyin/tiktok room coverage + HLS merge); streamlink is tried ONCE
only if the yt-dlp process exits non-zero (its twitch-class HLS plugins).
Join-point capture only — `--live-from-start` is never passed.

Exactly one engine subprocess runs at a time per recording, spawned via
asyncio.create_subprocess_exec (POSIX: start_new_session=True so the child
leads its own process group; cancel sends SIGTERM to the group, waits
TERMINATE_GRACE_S, then SIGKILLs it — Windows dev falls back to a psutil
kill-tree). Output filenames embed started_at
(`{platform}/{creator}/live_<ts>.mp4`) so re-captures never collide or trip
the downloader dup-check.

Restart recovery (reconcile_on_boot, called from main.py lifespan after
init_db): LiveRecordings stuck in 'recording' — watchlist origins are probed;
a still-live room flips the stale row to 'interrupted' and auto re-triggers a
fresh recording row, an offline room flips to 'ended'; manual origins flip to
'interrupted' (the Queue retry button re-triggers; no auto re-record).
"""

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app import models
from app.services import events, ytdlp

log = logging.getLogger(__name__)

TERMINATE_GRACE_S = 10.0
STOP_POLL_S = 0.2


def _db():
    """Fresh AsyncSession resolved at call time — module reloads (tests,
    config changes) must be honored."""
    import app.db

    return app.db.async_session()


# ---- pure helpers (unit-tested, no I/O) -------------------------------------


def output_filename(started_at: datetime) -> str:
    return f"live_{started_at:%Y%m%d_%H%M%S}.mp4"


def recording_output_path(platform: str, creator: str, started_at: datetime) -> Path:
    """MEDIA_ROOT/{platform}/{creator}/live_<started_at>.mp4. Config imported
    lazily so test reloads of app.config are honored."""
    from app.config import MEDIA_ROOT

    return (
        MEDIA_ROOT
        / ytdlp._sanitize(platform)
        / ytdlp._sanitize(creator)
        / output_filename(started_at)
    )


def engine_chain(
    room_url: str, outtmpl: str, cookiefile: str | None = None
) -> list[list[str]]:
    """Ordered engine commands: yt-dlp live first, one streamlink retry.

    Both engines take the same Netscape cookies.txt. Anonymous capture is not
    merely a login inconvenience: rooms that gate their top tier (or the room
    itself) behind an account hand a logged-out client the lower ladder and
    the recording silently lands at that quality, so the stored credential
    rides along whenever one exists.
    """
    # The capture engine is a SUBPROCESS, so app.services.ytdlp's in-process
    # plugin load does not reach it — the TikTok live/detail override has to be
    # handed over on the command line or every FLV-only room dies with
    # "The channel is not currently live". "default" first keeps yt-dlp's own
    # plugin directories, which the flag would otherwise replace outright.
    ytdlp_cmd = [
        # "--no-progress", not "--noprogress": the latter is the LIBRARY option
        # name, and yt-dlp's CLI rejects it outright with exit 2 before it ever
        # looks at the URL — so the yt-dlp engine never ran and every live
        # capture silently came from the streamlink retry instead.
        sys.executable, "-m", "yt_dlp", "--quiet", "--no-progress",
        "--plugin-dirs", "default", "--plugin-dirs", str(ytdlp.PLUGIN_ROOT),
    ]
    streamlink_cmd = ["streamlink", "--quiet"]
    if cookiefile:
        ytdlp_cmd += ["--cookies", cookiefile]
        streamlink_cmd += ["--http-cookies-file", cookiefile]
    return [
        [*ytdlp_cmd, room_url, "-o", outtmpl],
        [*streamlink_cmd, room_url, "best", "-o", outtmpl],
    ]


def probe_is_live(room_url: str, cookiefile: str | None = None) -> bool:
    """Sync live-status probe for boot reconciliation (Decision C: yt-dlp
    probes only, run via to_thread). Any extractor error counts as offline.

    The cookies matter as much here as they do for the capture itself: a room
    that answers a logged-out probe with "not live" is indistinguishable from
    one that really ended, and reconcile_on_boot would quietly write the
    recording off as 'ended' rather than resume it. Resolved by the async
    caller and handed in, because decrypting it here would mean opening a
    second event loop inside this worker thread.
    """
    try:
        info = ytdlp.probe(room_url, cookiefile)
    except Exception:
        return False
    return bool(info.get("is_live"))


# ---- subprocess control ------------------------------------------------------


async def _spawn_proc(cmd: list[str]) -> asyncio.subprocess.Process:
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True  # child leads its own process group
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        **kwargs,
    )


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    """POSIX: signal the engine's whole group (the child is its own leader via
    start_new_session). Windows dev: direct handle (maps to TerminateProcess);
    tree cleanup happens in _kill_tree if needed."""
    if os.name == "posix":
        os.killpg(os.getpgid(proc.pid), sig)
    else:
        proc.send_signal(sig)


def _kill_tree(pid: int) -> None:
    """Last-resort kill after the grace window (Risk table: no orphans)."""
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        import psutil

        try:
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
        except psutil.Error:
            pass


class RecorderSupervisor:
    def __init__(self) -> None:
        # recording_id -> (engine Process, supervision Task)
        self._registry: dict[int, tuple[asyncio.subprocess.Process, asyncio.Task | None]] = {}
        # recording_id -> status to apply on exit instead of the computed one
        # (user-initiated stop => 'ended', not 'failed')
        self._intended: dict[int, str] = {}
        self.grace_s = TERMINATE_GRACE_S

    # ---- lifecycle -------------------------------------------------------

    def start_recording(self, recording_id: int) -> asyncio.Task | None:
        """Begin supervising a 'recording'-status LiveRecording row."""
        # ponytail: guard is advisory — two starts racing the registry write
        # inside _supervise can double-spawn; single-user app, acceptable.
        if recording_id in self._registry:
            return None
        return asyncio.create_task(self._supervise(recording_id))

    async def stop(self, recording_id: int) -> bool:
        """Graceful stop: SIGTERM to the group, grace window, then kill.
        Records 'ended' intent so the exit handler doesn't mark 'failed'."""
        entry = self._registry.get(recording_id)
        if not entry or entry[0] is None:
            return False
        proc = entry[0]
        self._intended[recording_id] = "ended"
        try:
            _signal_group(proc, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already gone; exit handler below still finalizes
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.grace_s
        while proc.returncode is None and loop.time() < deadline:
            await asyncio.sleep(STOP_POLL_S)
        if proc.returncode is None:
            _kill_tree(proc.pid)
        # Let the exit handler persist terminal state before callers read it.
        if entry[1] is not None:
            try:
                await asyncio.wait_for(asyncio.shield(entry[1]), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        return True

    async def shutdown(self) -> None:
        """Kill every registered engine child (main.py lifespan teardown)."""
        for rid, (proc, _task) in list(self._registry.items()):
            if proc is not None and getattr(proc, "returncode", 0) is None:
                try:
                    _kill_tree(proc.pid)
                except Exception:
                    log.exception("kill failed for recording %s", rid)
        tasks = [t for _p, t in list(self._registry.values()) if t is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---- supervision -----------------------------------------------------

    async def _supervise(self, recording_id: int) -> None:
        try:
            await self._supervise_inner(recording_id)
        except Exception as exc:
            log.exception("recording %s supervision crashed", recording_id)
            try:
                await self._finalize(recording_id, "failed", None, str(exc)[:500])
            except Exception:
                log.exception("crash-finalize failed for recording %s", recording_id)
        finally:
            self._registry.pop(recording_id, None)
            self._intended.pop(recording_id, None)

    async def _supervise_inner(self, recording_id: int) -> None:
        async with _db() as session:
            rec = await session.get(models.LiveRecording, recording_id)
        if rec is None or rec.status != "recording":
            return

        out_path = recording_output_path(rec.platform, rec.creator, rec.started_at)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        events.publish({"type": "recording.started", "recording_id": recording_id})

        me = asyncio.current_task()
        rc: int | None = -1
        err = ""
        chain_broken_by_stop = False
        # Local import: credentials imports app.services, so a module-level
        # import here would be circular (same reason as downloader.py).
        from app.routers.credentials import aget_cookiefile

        cookiefile = await aget_cookiefile(rec.platform)
        try:
            chain = engine_chain(
                rec.room_url, str(out_path), str(cookiefile) if cookiefile else None
            )
            for cmd in chain:
                if self._intended.get(recording_id):
                    # User stop landed while we were between engines — do not
                    # spawn the fallback; finalize as 'ended' below.
                    chain_broken_by_stop = True
                    break
                proc = await _spawn_proc(cmd)
                self._registry[recording_id] = (proc, me)
                rc = await proc.wait()
                if rc == 0:
                    break
                err = f"engine ({cmd[0]}) exited with code {rc}"
        finally:
            # A capture can run for hours; the temp file has to outlive the
            # whole chain, not just the first engine.
            if cookiefile:
                cookiefile.unlink(missing_ok=True)

        produced = out_path.exists() and out_path.stat().st_size > 0
        intended = self._intended.pop(recording_id, None) or (
            "ended" if chain_broken_by_stop else None
        )

        if intended:
            await self._finalize(recording_id, intended, out_path if produced else None, None)
        elif rc == 0 and produced:
            await self._finalize(recording_id, "finished", out_path, None)
        else:
            await self._finalize(
                recording_id, "failed", None, err or "engine produced no output"
            )

    async def _finalize(
        self,
        recording_id: int,
        status: str,
        out_path: Path | None,
        error: str | None,
    ) -> None:
        """Persist terminal state; LibraryItem on a usable capture; publish."""
        async with _db() as session:
            rec = await session.get(models.LiveRecording, recording_id)
            if rec is None:
                return
            rec.status = status
            rec.error = error
            if status in ("finished", "ended"):
                rec.ended_at = models.utcnow()
            if out_path is not None:
                rec.output_path = str(out_path)
            if status in ("finished", "ended") and out_path is not None and out_path.exists():
                session.add(
                    models.LibraryItem(
                        file_path=str(out_path),
                        platform=rec.platform,
                        creator=rec.creator,
                        title=f"Live {rec.started_at:%Y-%m-%d %H:%M}",
                        media_type="recording",
                        size_bytes=out_path.stat().st_size,
                    )
                )
            await session.commit()
        etype = {
            "finished": "recording.finished",
            "ended": "recording.ended",
            "interrupted": "recording.interrupted",
            "failed": "recording.failed",
        }[status]
        payload = {"type": etype, "recording_id": recording_id}
        if error:
            payload["error"] = error[:200]
        events.publish(payload)

    # ---- boot reconciliation ----------------------------------------------

    async def reconcile_on_boot(self) -> None:
        """Resolve LiveRecordings stuck in 'recording' after a restart
        (plan step 16). Never leaves a row lingering in 'recording'."""
        async with _db() as session:
            rows = (
                (
                    await session.execute(
                        select(models.LiveRecording).where(
                            models.LiveRecording.status == "recording"
                        )
                    )
                )
                .scalars()
                .all()
            )

        for rec in rows:
            if rec.origin != "watchlist":
                # Manual: Queue retry button handles the user's re-trigger.
                await self._patch_status(
                    rec.id, "interrupted", error="interrupted by app restart"
                )
                continue
            from app.routers.credentials import aget_cookiefile

            cookiefile = await aget_cookiefile(rec.platform)
            try:
                live = await asyncio.to_thread(
                    probe_is_live, rec.room_url, str(cookiefile) if cookiefile else None
                )
            finally:
                if cookiefile:
                    cookiefile.unlink(missing_ok=True)
            if live:
                await self._patch_status(
                    rec.id, "interrupted", error="interrupted by app restart"
                )
                new = await begin_recording(
                    rec.room_url, rec.platform, rec.creator, origin="watchlist"
                )
                events.publish(
                    {
                        "type": "recording.retriggered",
                        "recording_id": new.id,
                        "previous_id": rec.id,
                    }
                )
            else:
                async with _db() as session:
                    row = await session.get(models.LiveRecording, rec.id)
                    if row:
                        row.status = "ended"
                        row.ended_at = models.utcnow()
                        row.error = None
                        await session.commit()
                events.publish({"type": "recording.ended", "recording_id": rec.id})

    async def _patch_status(self, recording_id: int, status: str, error: str) -> None:
        async with _db() as session:
            rec = await session.get(models.LiveRecording, recording_id)
            if rec:
                rec.status = status
                rec.error = error
                await session.commit()
        events.publish({"type": f"recording.{status}", "recording_id": recording_id})


async def begin_recording(
    room_url: str, platform: str, creator: str, *, origin: str
) -> models.LiveRecording:
    """Create a LiveRecording row and hand it to the supervisor. Single entry
    point for the poller (origin='watchlist') and the manual-record endpoint
    (origin='manual')."""
    async with _db() as session:
        rec = models.LiveRecording(
            room_url=room_url, platform=platform, creator=creator, origin=origin
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
    recorder.start_recording(rec.id)
    return rec


recorder = RecorderSupervisor()  # singleton wired into main.py lifespan
