"""Async download manager (plan step 7 / Decision C).

Worker pool over an asyncio.Queue; each YoutubeDL call runs in a thread via
to_thread. Pause raises AbortDownload from the progress hook (preserves .part
files); resume re-enqueues with continuedl + the locked format_id (no
re-probe). Space-floor gate pauses AUTO jobs only; hysteresis auto-resume
never touches user-paused jobs.
"""

import asyncio
import logging
import queue as pyqueue
import threading
from pathlib import Path

from sqlalchemy import select
from sqlalchemy import update as sa_update

from app import models
from app.services import events, ytdlp

log = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 3
# Reconnect budget for a live capture that drops mid-stream.
LIVE_MAX_RETRIES = 20
DEFAULT_SPACE_FLOOR_PCT = 5.0
WATCHER_INTERVAL_S = 30.0
# Hysteresis margin (percentage points) before auto-resume, per plan step 15.
RESUME_MARGIN_PCT = 2.0

TERMINAL = {"done", "failed", "skipped"}


def _db():
    """Fresh AsyncSession resolved at call time — module reloads (tests,
    config changes) must be honored."""
    import app.db

    return app.db.async_session()


def free_space_pct(path: Path) -> float:
    """Free % on the volume holding `path` (media volume, not root fs)."""
    try:
        import psutil

        usage = psutil.disk_usage(str(path))
        return usage.free / usage.total * 100 if usage.total else 0.0
    except OSError:
        return 0.0


class DownloadManager:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._watcher: asyncio.Task | None = None
        self._abort_events: dict[int, threading.Event] = {}
        self._cancelled: set[int] = set()
        # Reconnect attempts per job, reset once a run completes.
        self._live_retries: dict[int, int] = {}

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        await self._recover_interrupted()
        n = await self.get_concurrency()
        for i in range(n):
            self._workers.append(asyncio.create_task(self._worker(f"dl-{i}")))
        self._watcher = asyncio.create_task(self._space_watcher())

    async def _recover_interrupted(self) -> None:
        """Park jobs left mid-flight by a crash/restart as 'paused'.

        run_job refuses to re-enter a job already marked 'downloading' (its
        re-entry guard), and 'resume' only accepts parked states — so without
        this a killed download is unreachable forever. .part files survive, so
        resume continues rather than restarts.
        """
        async with _db() as s:
            rows = (
                await s.execute(
                    select(models.DownloadJob).where(
                        models.DownloadJob.status.in_(("probing", "downloading"))
                    )
                )
            ).scalars().all()
            for job in rows:
                job.status = "paused"
            if rows:
                await s.commit()
                log.info("recovered %d interrupted job(s) as paused", len(rows))

    async def stop(self) -> None:
        # Abort in-flight engine threads first so workers drain quickly.
        for ev in list(self._abort_events.values()):
            ev.set()
        tasks = list(self._workers)
        if self._watcher:
            tasks.append(self._watcher)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def get_concurrency(self) -> int:
        async with _db() as s:
            row = await s.get(models.AppSetting, "concurrency")
            try:
                return max(1, min(8, int(row.value))) if row else DEFAULT_CONCURRENCY
            except ValueError:
                return DEFAULT_CONCURRENCY

    async def get_floor(self) -> float:
        async with _db() as s:
            row = await s.get(models.AppSetting, "space_floor_pct")
            try:
                return float(row.value) if row else DEFAULT_SPACE_FLOOR_PCT
            except ValueError:
                return DEFAULT_SPACE_FLOOR_PCT

    # ---- public API ------------------------------------------------------

    def enqueue(self, job_id: int) -> None:
        self._queue.put_nowait(job_id)

    def pause(self, job_id: int) -> bool:
        ev = self._abort_events.get(job_id)
        if ev:
            ev.set()  # hook raises AbortDownload -> job marked paused
            return True
        return False

    def cancel(self, job_id: int) -> None:
        self._cancelled.add(job_id)
        # setdefault covers cancel-before-worker-starts.
        self._abort_events.setdefault(job_id, threading.Event()).set()

    def resume(self, job_id: int) -> None:
        """Re-enqueue a paused job — continuedl picks up .part files."""
        self.enqueue(job_id)

    # ---- worker loop -----------------------------------------------------

    async def _worker(self, name: str) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self.run_job(job_id)
            except Exception:
                log.exception("worker %s crashed on job %s", name, job_id)
                await self._patch(
                    job_id, status="failed", error="internal error"
                )
            finally:
                self._queue.task_done()

    async def run_job(self, job_id: int) -> None:
        async with _db() as session:
            job = await session.get(models.DownloadJob, job_id)
            if not job or job.status in TERMINAL or job.status == "downloading":
                return

            floor = await self.get_floor()
            if job.is_auto and free_space_pct(_gate_path()) < floor:
                await self._set_status(session, job, "paused_space_floor")
                events.publish({"type": "job.paused_space_floor", "job_id": job.id})
                return

            if not job.redownload_requested and await self._is_duplicate(session, job):
                await self._set_status(session, job, "skipped")
                events.publish({"type": "job.skipped", "job_id": job.id})
                return

            await self._set_status(session, job, "probing")
            events.publish({"type": "job.probing", "job_id": job.id})

            abort = threading.Event()
            self._abort_events[job.id] = abort
            progress_q: pyqueue.Queue = pyqueue.Queue()
            loop = asyncio.get_running_loop()

            settings = {
                "folder_template": await self._setting_str(
                    session, "folder_template", "{platform}/{creator}"
                ),
                "container": await self._setting_str(session, "container", "mp4"),
                "subs": (await self._setting_str(session, "subs", "0")) == "1",
                "audio": (
                    lambda v: v if v in ("mp3", "m4a") else None
                )(await self._setting_str(session, "audio", "")),
            }

            consumer = asyncio.create_task(
                self._consume_progress(job.id, progress_q)
            )
            try:
                extra: dict = {
                    "progress_hooks": [
                        ytdlp.make_progress_hook(loop, progress_q, abort)
                    ]
                }
                # Stored cookies gate premium/HD ladders and members-only posts;
                # without them the extractor silently returns the public formats.
                # Local import: credentials imports app.services, so a
                # module-level import here would be circular.
                from app.routers.credentials import aget_cookiefile

                cookiefile = await aget_cookiefile(job.platform)
                if cookiefile:
                    extra["cookiefile"] = str(cookiefile)
                opts = ytdlp.build_opts(job, settings, extra=extra)
                try:
                    info = await asyncio.to_thread(ytdlp.download, opts, job.url)
                finally:
                    if cookiefile:
                        cookiefile.unlink(missing_ok=True)
                out = ytdlp.final_path(info) or job.output_path
                # _patch writes progress from its own session while this one
                # holds a row loaded before the download; refresh before the
                # terminal write so those commits don't leave this copy stale.
                await session.refresh(job)
                job.status = "done"
                job.progress = 100.0
                job.error = None
                if out:
                    job.output_path = out
                job.redownload_requested = False
                await session.commit()
                from app.services.library_writer import write_library_item_for_job

                await write_library_item_for_job(session, job)
                events.publish({"type": "job.done", "job_id": job.id})
            except ytdlp.AbortDownload:
                if job.id in self._cancelled:
                    self._cleanup_parts(job)
                    await self._set_status(
                        session, job, status="failed", error="cancelled"
                    )
                    events.publish({"type": "job.cancelled", "job_id": job.id})
                else:
                    # Pause: .part files stay on disk; resume continues them.
                    await self._set_status(session, job, "paused")
                    events.publish({"type": "job.paused", "job_id": job.id})
            except Exception as exc:
                captured = self._captured_part(job)
                if _is_stream_over(exc) and captured:
                    # The host ended the stream: yt-dlp raises, but everything
                    # up to that point is on disk and is the whole recording.
                    out = self._finalize_part(captured)
                    await session.refresh(job)  # progress writes made this stale
                    job.status = "done"
                    job.progress = 100.0
                    job.error = None
                    job.output_path = str(out)
                    job.redownload_requested = False
                    await session.commit()
                    from app.services.library_writer import (
                        write_library_item_for_job,
                    )

                    await write_library_item_for_job(session, job)
                    events.publish({"type": "job.done", "job_id": job.id})
                elif captured and _is_reconnectable(exc):
                    # Mid-stream drop with bytes on disk: re-queue so continuedl
                    # resumes and we capture as much of the live as possible.
                    attempts = self._live_retries.get(job.id, 0) + 1
                    if attempts <= LIVE_MAX_RETRIES:
                        self._live_retries[job.id] = attempts
                        await self._set_status(session, job, "queued")
                        events.publish(
                            {
                                "type": "job.reconnecting",
                                "job_id": job.id,
                                "attempt": attempts,
                            }
                        )
                        self.enqueue(job.id)
                    else:
                        await self._set_status(
                            session, job, status="failed", error=str(exc)[:500]
                        )
                        events.publish(
                            {
                                "type": "job.failed",
                                "job_id": job.id,
                                "error": str(exc)[:200],
                            }
                        )
                else:
                    await self._set_status(
                        session, job, status="failed", error=str(exc)[:500]
                    )
                    events.publish(
                        {"type": "job.failed", "job_id": job.id, "error": str(exc)[:200]}
                    )
            finally:
                progress_q.put(None)  # sentinel: consumer drains then exits
                try:
                    await asyncio.wait_for(asyncio.shield(consumer), timeout=2)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    consumer.cancel()
                self._abort_events.pop(job.id, None)
                self._cancelled.discard(job.id)
                # Keep the counter only while a reconnect chain is in flight;
                # any settled job starts fresh next time.
                if job.status != "queued":
                    self._live_retries.pop(job.id, None)

    # ---- helpers ---------------------------------------------------------

    async def _is_duplicate(self, session, job: models.DownloadJob) -> bool:
        """Output already on disk OR prior done/skipped job with same
        normalized URL + kind counts as duplicate (AC19). Comparison happens
        in Python because stored URLs are raw."""
        from app.util.platform import normalize_url

        target = normalize_url(job.url)
        rows = (
            (
                await session.execute(
                    select(models.DownloadJob).where(
                        models.DownloadJob.kind == job.kind,
                        models.DownloadJob.id != job.id,
                        models.DownloadJob.status.in_(("done", "skipped")),
                    )
                )
            )
            .scalars()
            .all()
        )
        return any(normalize_url(prior.url) == target for prior in rows)

    def _captured_part(self, job: models.DownloadJob) -> Path | None:
        """Largest non-empty .part for this job, if any bytes were captured."""
        try:
            parts = [
                p
                for p in ytdlp.output_dir(job).glob("*.part")
                if p.is_file() and p.stat().st_size > 0
            ]
        except OSError:
            return None
        return max(parts, key=lambda p: p.stat().st_size) if parts else None

    def _finalize_part(self, part: Path) -> Path:
        """Drop the .part suffix so the capture becomes a playable file."""
        final = part.with_suffix("")
        try:
            part.replace(final)
            return final
        except OSError:
            log.warning("could not finalize %s", part)
            return part

    def _cleanup_parts(self, job: models.DownloadJob) -> None:
        """Cancel removes leftover .part/.ytdl temp fragments."""
        try:
            for p in ytdlp.output_dir(job).glob("*.part*"):
                p.unlink(missing_ok=True)
            for p in ytdlp.output_dir(job).glob("*.ytdl"):
                p.unlink(missing_ok=True)
        except OSError:
            log.warning("temp cleanup failed for job %s", job.id)

    async def _consume_progress(self, job_id: int, q: pyqueue.Queue) -> None:
        """Drain hook payloads (marshaled via call_soon_threadsafe) until the
        None sentinel; exits promptly after run_job finishes."""
        promoted = False
        while True:
            payload = await asyncio.to_thread(q.get)
            if payload is None:
                break
            fields: dict = {"progress": _pct(payload)}
            # First byte-level hook means probing is over. run_job sets
            # 'probing' up front and only writes a terminal status at the end,
            # so without this the job reads as 'probing' for its whole life.
            #
            # run_job does not yield between the engine returning and its
            # terminal commit, so by the time these drain the job is often
            # already settled. Re-read status inside the same session as the
            # write instead of trusting a snapshot: a settled job keeps its
            # terminal status and only takes the progress number.
            if not promoted and payload.get("status") == "downloading":
                fields["status"] = "downloading"
            if await self._patch(job_id, **fields):
                # Latch only on a promotion that actually won the UPDATE; a
                # no-op means the row was not 'probing' yet, so keep trying.
                promoted = True
                events.publish({"type": "job.downloading", "job_id": job_id})
            events.publish({"type": "job.progress", "job_id": job_id, **payload})

    async def _set_status(
        self, session, job: models.DownloadJob, status: str, error: str | None = None
    ) -> None:
        # Progress writes land on a separate session mid-download, so this
        # instance can be stale by the time a terminal status is set.
        try:
            await session.refresh(job)
        except Exception:  # row vanished (cancel+delete races)
            return
        job.status = status
        if error is not None:
            job.error = error
        elif status != "failed":
            job.error = None
        await session.commit()

    async def _patch(self, job_id: int, **fields) -> bool:
        """Apply fields; returns True if this call promoted to 'downloading'.

        The status check happens against the row read in this session, so a
        job that run_job already settled keeps its terminal status and only
        takes the progress number.
        """
        # Only the 'downloading' promotion is conditional; every other status
        # (e.g. the worker's crash path) is an unconditional write.
        promote = fields.get("status") == "downloading"
        if promote:
            fields.pop("status")
        async with _db() as s:
            promoted = False
            if promote:
                # Conditional UPDATE, not a read-then-write: run_job owns the
                # terminal status from another session and may commit before
                # or after this drains. Matching only 'probing' makes a late
                # promotion a no-op instead of resurrecting a finished job.
                res = await s.execute(
                    sa_update(models.DownloadJob)
                    .where(
                        models.DownloadJob.id == job_id,
                        models.DownloadJob.status == "probing",
                    )
                    .values(status="downloading")
                )
                promoted = bool(res.rowcount)
            if fields:
                await s.execute(
                    sa_update(models.DownloadJob)
                    .where(models.DownloadJob.id == job_id)
                    .values(**fields)
                )
            await s.commit()
            return promoted

    async def _setting_str(self, session, key: str, default: str) -> str:
        row = await session.get(models.AppSetting, key)
        return row.value if row else default

    # ---- space-floor watcher ----------------------------------------------

    async def _space_watcher(self) -> None:
        """Every WATCHER_INTERVAL_S offer auto-resume; the hysteresis gate
        lives inside resume_space_floor_jobs. User-paused jobs are status
        'paused' and are never matched."""
        while True:
            await asyncio.sleep(WATCHER_INTERVAL_S)
            try:
                n = await self.resume_space_floor_jobs()
                if n:
                    log.info("auto-resumed %d space-floor-paused jobs", n)
            except Exception:
                log.exception("space watcher iteration failed")

    async def resume_space_floor_jobs(self) -> int:
        """Re-enqueue paused_space_floor jobs once free% >= floor +
        RESUME_MARGIN_PCT (hysteresis, plan step 15). Gate checked here so
        no caller can bypass it; user-paused jobs never match the query."""
        if free_space_pct(_gate_path()) < await self.get_floor() + RESUME_MARGIN_PCT:
            return 0
        async with _db() as s:
            rows = (
                (
                    await s.execute(
                        select(models.DownloadJob).where(
                            models.DownloadJob.status == "paused_space_floor"
                        )
                    )
                )
                .scalars()
                .all()
            )
            ids = [r.id for r in rows]
        for jid in ids:
            self.enqueue(jid)
        return len(ids)


def _gate_path() -> Path:
    """Volume the floor gate measures (media volume per plan risk table).
    Imported lazily so tests can monkeypatch config paths after import."""
    from app.config import MEDIA_ROOT

    return MEDIA_ROOT


# A live host ending the broadcast is a normal finish, not a failure.
_STREAM_OVER_MARKERS = (
    "streamer is not live",
    "is not currently live",
    "live event has ended",
    "stream is offline",
    "room is offline",
)

# Transport-level interruptions worth reconnecting for; a live stream drops
# these routinely and the capture can continue from the .part file.
_RECONNECTABLE_MARKERS = (
    "more expected",
    "connection reset",
    "connection aborted",
    "timed out",
    "read timeout",
    "incomplete read",
    "temporary failure",
    "http error 5",
)


def _is_stream_over(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _STREAM_OVER_MARKERS)


def _is_reconnectable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _RECONNECTABLE_MARKERS)


def _pct(payload: dict) -> float:
    total = payload.get("total_bytes") or payload.get("total_bytes_estimate")
    done = payload.get("downloaded_bytes", 0)
    if not total:
        return 0.0
    return round(min(done / total * 100, 100.0), 1)


manager = DownloadManager()  # singleton wired into main.py lifespan (US-005)
