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
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy import update as sa_update

from app import models
from app.services import events, ytdlp
from app.services.settings_store import decode_setting

log = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 3
# Reconnect budget for a live capture that drops mid-stream.
LIVE_MAX_RETRIES = 20
DEFAULT_SPACE_FLOOR_PCT = 5.0
WATCHER_INTERVAL_S = 30.0
# Hysteresis margin (percentage points) before auto-resume, per plan step 15.
RESUME_MARGIN_PCT = 2.0
# How often the first-bytes watchdog looks for the engine's own .part.
PART_PROBE_S = 2.0

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


def _size_of(path: Path) -> int:
    return path.stat().st_size


def part_snapshot(job) -> set[str]:
    """.part files already in this job's output dir before its engine ran.

    The dir is per creator, and the live RECORDER writes there too: an
    auto-record and a queued download of the same room land side by side. So
    "the largest .part here" is not necessarily this job's -- it can be a
    capture ffmpeg is still writing, which this module would otherwise promote
    on, rename out from under the recorder, and file as its own output.
    Anything present before we start belongs to somebody else.
    """
    try:
        return {str(p) for p in ytdlp.output_dir(job).glob("*.part") if p.is_file()}
    except OSError:
        return set()


def _arg_path(arg: str) -> str:
    """The filesystem path an engine argument refers to.

    yt-dlp hands ffmpeg its output as ffmpeg's own URL form -- verified
    against the running image, _ffmpeg_filename_argument turns
    /media/x/live.mp4.part into file:/media/x/live.mp4.part, because a bare
    path containing ':' would otherwise read as a protocol. Comparing raw
    argv against the path therefore matched nothing, and cancel was inert for
    the exact engine it was written to stop.
    """
    return arg[len("file:"):] if arg.startswith("file:") else arg


def terminate_engine_children(part: Path) -> int:
    """Stop our own engine subprocesses writing `part`; returns how many.

    Cancel is delivered by raising AbortDownload from a progress hook, and an
    external downloader fires none while it runs -- so cancelling a live HLS
    capture set the flag, marked the row 'cancelled', and left ffmpeg writing
    to disk until the broadcast ended on its own.

    yt-dlp spawns ffmpeg as a child of THIS process and hands it the temp
    filename, so the path is what tells one capture from another when several
    run at once. Match it exactly: cancelling one job must never reach into
    another job's engine, or the live recorder's.
    """
    import psutil

    needle = str(part)
    stopped = 0
    try:
        children = psutil.Process().children(recursive=True)
    except psutil.Error:
        return 0
    for child in children:
        try:
            argv = child.cmdline() or []
        except psutil.Error:
            continue  # already gone, or not ours to look at
        if any(_arg_path(a) == needle for a in argv):
            try:
                child.terminate()
                stopped += 1
            except psutil.Error:
                continue
    return stopped


class DownloadManager:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._watcher: asyncio.Task | None = None
        self._abort_events: dict[int, threading.Event] = {}
        self._cancelled: set[int] = set()
        # Reconnect attempts per job, reset once a run completes.
        self._live_retries: dict[int, int] = {}
        # Where each running job's engine is writing, so cancel can name it.
        self._job_parts: dict[int, Path] = {}
        # Jobs whose engine has reported at least one progress hook of its own.
        self._hook_seen: set[int] = set()

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
        # Key is "concurrency_cap", which is what settings_store writes. This
        # read said "concurrency" — a key nothing has ever written — so the
        # Settings slider moved a number the queue never looked at, and the
        # cap was pinned to DEFAULT_CONCURRENCY forever.
        async with _db() as s:
            row = await s.get(models.AppSetting, "concurrency_cap")
            if row is None:
                return DEFAULT_CONCURRENCY
            try:
                return max(1, min(8, int(decode_setting(row.value))))
            except (TypeError, ValueError):
                return DEFAULT_CONCURRENCY

    async def get_floor(self) -> float:
        async with _db() as s:
            row = await s.get(models.AppSetting, "space_floor_pct")
            if row is None:
                return DEFAULT_SPACE_FLOOR_PCT
            try:
                return float(decode_setting(row.value))
            except (TypeError, ValueError):
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
        # The event only reaches an engine that calls progress hooks; an
        # external downloader does not, so name its process and stop it too.
        part = self._job_parts.get(job_id)
        if part is not None:
            terminate_engine_children(part)

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
            job.started_at = models.utcnow()  # first probe of this run
            await session.commit()
            events.publish({"type": "job.probing", "job_id": job.id})

            abort = threading.Event()
            self._abort_events[job.id] = abort
            progress_q: pyqueue.Queue = pyqueue.Queue()
            loop = asyncio.get_running_loop()
            baseline = await asyncio.to_thread(part_snapshot, job)

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
            first_bytes = asyncio.create_task(
                self._report_from_disk(job, baseline)
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
                # Everything that touches the decrypted file lives INSIDE the
                # try. build_opts used to sit above it, which looked harmless
                # until you notice it renders folder_template: an unsupported
                # placeholder raises KeyError (or ValueError on a stray brace)
                # between creating the plaintext cookie file and arming the
                # cleanup, stranding it in /tmp forever, once per attempt.
                try:
                    if cookiefile:
                        extra["cookiefile"] = str(cookiefile)
                    opts = ytdlp.build_opts(job, settings, extra=extra)
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
                job.finished_at = models.utcnow()
                if out:
                    job.output_path = out
                job.redownload_requested = False
                await session.commit()
                from app.services.library_writer import write_library_item_for_job

                await write_library_item_for_job(session, job)
                events.publish({"type": "job.done", "job_id": job.id})
            except ytdlp.AbortDownload:
                if job.id in self._cancelled:
                    self._cleanup_parts(job, baseline)
                    await self._set_status(
                        session, job, status="failed", error="cancelled"
                    )
                    events.publish({"type": "job.cancelled", "job_id": job.id})
                else:
                    # Pause: .part files stay on disk; resume continues them.
                    await self._set_status(session, job, "paused")
                    events.publish({"type": "job.paused", "job_id": job.id})
            except Exception as exc:
                captured = self._captured_part(job, baseline)
                if job.id in self._cancelled:
                    # cancel() stopped the engine; whatever it raised on the
                    # way down is that cancel, not a fault of its own. Without
                    # this the Queue reported a killed capture as "ffmpeg
                    # exited with code 255".
                    self._cleanup_parts(job, baseline)
                    await self._set_status(
                        session, job, status="failed", error="cancelled"
                    )
                    events.publish({"type": "job.cancelled", "job_id": job.id})
                elif _is_stream_over(exc) and captured:
                    # The host ended the stream: yt-dlp raises, but everything
                    # up to that point is on disk and is the whole recording.
                    out = self._finalize_part(captured)
                    await session.refresh(job)  # progress writes made this stale
                    job.status = "done"
                    job.progress = 100.0
                    job.error = None
                    job.finished_at = models.utcnow()
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
                first_bytes.cancel()
                progress_q.put(None)  # sentinel: consumer drains then exits
                try:
                    await asyncio.wait_for(asyncio.shield(consumer), timeout=2)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    consumer.cancel()
                self._abort_events.pop(job.id, None)
                self._cancelled.discard(job.id)
                self._hook_seen.discard(job.id)
                # Keep the claim while a re-queued retry or a paused job may
                # still resume onto the same .part; drop it once the run has
                # settled, so the next one starts fresh.
                if job.status in TERMINAL:
                    self._job_parts.pop(job.id, None)
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

    def _own_claim(self, job: models.DownloadJob) -> str | None:
        """The .part this job already claimed on an earlier attempt, if any.

        getattr, because _captured_part only ever needed a job to name its
        output directory; a caller holding something job-shaped keeps working
        and simply has no claim.
        """
        mine = self._job_parts.get(getattr(job, "id", None))
        return str(mine) if mine is not None else None

    def _not_mine(
        self, job: models.DownloadJob, baseline: set[str] | None
    ) -> set[str]:
        """`baseline` minus the file this job claimed for itself.

        part_snapshot is taken at the start of EVERY run, so on a resumed or
        reconnected attempt the job's own .part from the previous attempt is
        already on disk -- and the raw baseline disowned it. That cut the live
        reconnect chain to a single retry, left a capture that ended after a
        drop unfiled, and put a resumed HLS job back in 'probing'.
        """
        skip = set(baseline or ())
        mine = self._own_claim(job)
        if mine is not None:
            skip.discard(mine)
        return skip

    def _captured_part(
        self, job: models.DownloadJob, baseline: set[str] | None = None
    ) -> Path | None:
        """Largest non-empty .part this job's own engine wrote, if any.

        `baseline` is part_snapshot()'s reading from before the engine ran;
        without it a shared output dir hands back the recorder's live capture.
        """
        skip = self._not_mine(job, baseline)
        try:
            parts = [
                p
                for p in ytdlp.output_dir(job).glob("*.part")
                if p.is_file() and str(p) not in skip and p.stat().st_size > 0
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

    def _cleanup_parts(
        self, job: models.DownloadJob, baseline: set[str] | None = None
    ) -> None:
        """Cancel removes this job's leftover .part/.ytdl temp fragments.

        Scoped by `baseline` for the reason part_snapshot spells out: the glob
        also matches the live recorder's in-flight capture, and cancelling a
        download must not delete somebody else's recording. The job's own
        claim still goes -- cancelling a resumed job discards its bytes.
        """
        skip = self._not_mine(job, baseline)
        try:
            for p in ytdlp.output_dir(job).glob("*.part*"):
                if str(p) not in skip:
                    p.unlink(missing_ok=True)
            for p in ytdlp.output_dir(job).glob("*.ytdl"):
                p.unlink(missing_ok=True)
        except OSError:
            log.warning("temp cleanup failed for job %s", job.id)

    async def _report_from_disk(
        self, job: models.DownloadJob, baseline: set[str]
    ) -> None:
        """Status, size and rate for an engine that reports none of its own.

        yt-dlp hands a LIVE m3u8 to FFmpegFD, and ExternalFD fires exactly one
        progress hook -- 'finished', after the capture is over. Everything the
        Queue shows about a running job comes from those hooks, so a tiktok
        live (whose top tiers are all HLS) sat at 'probing', 0 MB and no rate
        for its entire run while ffmpeg wrote gigabytes. Bilibili was fine by
        luck: its FLV ladder is plain https, which goes through HttpFD and
        does report bytes.

        The .part on disk carries the same two facts the panel wants, so read
        them there and publish the events the engine will not. This goes quiet
        the moment a real hook arrives, so an engine that does report keeps
        its own numbers -- yt-dlp's rate is instantaneous where this one is an
        average over the poll window, and two sources would fight.
        """
        promoted = False
        claimed = False
        prev_size = 0
        prev_at = time.monotonic()
        while True:
            await asyncio.sleep(PART_PROBE_S)
            part = await asyncio.to_thread(self._captured_part, job, baseline)
            if part is None:
                continue
            self._job_parts[job.id] = part
            if not claimed:
                await self._claim_output(job, part)
                claimed = True
            if job.id in self._hook_seen:
                return  # claimed; the engine reports its own numbers
            try:
                size = await asyncio.to_thread(_size_of, part)
            except OSError:
                continue
            # Latch only on a promotion that won the UPDATE, same as
            # _consume_progress: a no-op means the row was not 'probing' yet.
            if not promoted and await self._patch(job.id, status="downloading"):
                promoted = True
                events.publish({"type": "job.downloading", "job_id": job.id})
            now = time.monotonic()
            window = now - prev_at
            speed = (size - prev_size) / window if window > 0 and size > prev_size else None
            prev_size, prev_at = size, now
            events.publish(
                {
                    "type": "job.progress",
                    "job_id": job.id,
                    "status": "downloading",
                    "downloaded_bytes": size,
                    "speed": speed,
                }
            )

    async def _claim_output(self, job: models.DownloadJob, part: Path) -> None:
        """Record where this job is writing, before it has finished writing.

        recovery decides a .part is an orphan by checking it against the
        output_path of every unsettled job -- and this column stayed NULL
        until a job SUCCEEDED. So a job that was merely paused claimed
        nothing, and the sweep collected its capture as abandoned, remuxed it
        away and deleted the source; Resume then had nothing to continue
        from. Caught with 1.9 GB of paused live captures one sweep away from
        exactly that.

        The recorder has claimed its path before the first byte for this same
        reason since the sweep remuxed a file out from under its own engine.
        Jobs never did. The claimed name is the .part without its suffix,
        which is where yt-dlp renames it on success, so the early claim and
        the eventual real path agree.
        """
        name = str(part)
        final = name[: -len(".part")] if name.endswith(".part") else name
        async with _db() as s:
            await s.execute(
                sa_update(models.DownloadJob)
                .where(models.DownloadJob.id == job.id)
                .values(output_path=final)
            )
            await s.commit()

    async def _consume_progress(self, job_id: int, q: pyqueue.Queue) -> None:
        """Drain hook payloads (marshaled via call_soon_threadsafe) until the
        None sentinel; exits promptly after run_job finishes."""
        promoted = False
        while True:
            payload = await asyncio.to_thread(q.get)
            if payload is None:
                break
            # Tell _report_from_disk to stand down: this engine reports.
            self._hook_seen.add(job_id)
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
        # Terminal runs get a finish stamp; any other state (probing/paused)
        # clears one left over from a prior run.
        if status in TERMINAL:
            job.finished_at = models.utcnow()
        else:
            job.finished_at = None
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
        # Values are json.dumps()'d on the way in. Returning row.value raw
        # handed folder_template back wrapped in literal quote characters,
        # which .format() then baked straight into the output path.
        row = await session.get(models.AppSetting, key)
        if row is None:
            return default
        value = decode_setting(row.value)
        return value if isinstance(value, str) else default

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
