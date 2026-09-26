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
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app import models
from app.services import browser, events, storage, ytdlp

log = logging.getLogger(__name__)

TERMINATE_GRACE_S = 10.0
STOP_POLL_S = 0.2
# How often the watchdog checks free space and looks for orphaned rows.
WATCHDOG_S = 30.0
# A row is born before its supervisor: the poller commits it, then starts the
# task. Anything younger than this may simply not have been handed over yet.
ORPHAN_GRACE_S = 120.0
ORPHAN_ERROR = "capture lost: its supervisor ended without recording an outcome"


def _db():
    """Fresh AsyncSession resolved at call time — module reloads (tests,
    config changes) must be honored."""
    import app.db

    return app.db.async_session()


# ---- pure helpers (unit-tested, no I/O) -------------------------------------


# Live capture writes FLV and finalizes to MP4, in two steps for two reasons.
# A cut MP4 has no moov atom and is unplayable, while a truncated FLV plays up
# to the cut -- so the bytes have to land in FLV. But the engines write the raw
# stream, so naming that file .mp4 did not make it one: TikTok captures were
# FLV bytes with an .mp4 extension, duration 0, and the ones recovery rescued
# stayed FLV -- HEVC-in-FLV, the codec id 12 extension ffmpeg itself only
# learned in 8.0, which VLC cannot demux at all. No picture, no sound.
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"


def capture_filename(started_at: datetime) -> str:
    return f"live_{started_at:%Y%m%d_%H%M%S}.flv"


def output_filename(started_at: datetime) -> str:
    return f"live_{started_at:%Y%m%d_%H%M%S}.mp4"


def _video_codec(src: Path) -> str:
    """ffprobe's name for the first video stream; "" when it cannot say."""
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1",
             str(src)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (out.stdout or "").strip()


def mp4_copy_args(src: Path) -> list[str]:
    """-c copy into MP4, with the video tag that keeps HEVC playable.

    The mp4 muxer tags HEVC 'hev1' by default; VLC, QuickTime and most TVs
    want 'hvc1' and give a black frame or nothing for 'hev1'. Only for HEVC:
    stamping hvc1 on an H.264 stream would break a file that already worked.
    """
    args = ["-c", "copy", "-movflags", "+faststart"]
    if _video_codec(src) == "hevc":
        args += ["-tag:v", "hvc1"]
    return args


async def remux_to_mp4(src: Path, dst: Path) -> bool:
    """Copy the capture into a real MP4. True when dst is usable.

    Never destructive on failure: the caller keeps the FLV, which is playable
    on its own for H.264 and at least holds the bytes for HEVC.
    """
    def _run() -> int:
        return subprocess.run(
            [FFMPEG, "-y", "-err_detect", "ignore_err", "-i", str(src),
             *mp4_copy_args(src), str(dst)],
            capture_output=True,
            timeout=3600,
        ).returncode

    try:
        rc = await asyncio.to_thread(_run)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("remux of %s raised %s", src.name, exc)
        return False
    if rc != 0 or not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        log.warning("remux of %s failed (rc=%s)", src.name, rc)
        return False
    return True


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


def part_path(capture: Path) -> Path:
    """Where an engine writes before it renames: <capture>.part."""
    return capture.with_name(capture.name + ".part")


def captured_file(capture: Path) -> Path | None:
    """The bytes the engine actually wrote, finished name or temp name.

    yt-dlp writes <name>.part and renames on a clean exit -- and an engine
    that takes a signal never reaches the rename. The supervisor used to look
    only for the renamed file, so a stopped capture counted as having produced
    nothing: no remux, no library item, and a row still advertising a path
    that was never created. The bytes survived only because the orphan sweep
    collected them ten minutes later under a "(recovered)" title, which is a
    safety net doing a job that belongs here.

    Whichever holds more, when both names exist -- because the engine chain
    can leave one of each. yt-dlp writes <name>.part, and streamlink, the
    fallback it falls through to, writes <name> directly, so a yt-dlp capture
    killed mid-flight and a fallback that managed a few seconds sit side by
    side. Preferring the finished name on principle handed the recording the
    smaller of the two: measured on a restart at 1.4 MB of streamlink against
    334 MB of yt-dlp. Size decides it correctly in both directions -- a
    complete fallback capture outweighs an abandoned .part, and a long .part
    outweighs a fallback that barely started.
    """
    best: Path | None = None
    best_size = 0
    for candidate in (capture, part_path(capture)):
        try:
            if not candidate.is_file():
                continue
            size = candidate.stat().st_size
        except OSError:
            continue
        if size > best_size:
            best, best_size = candidate, size
    return best


def drop_part_suffix(path: Path) -> Path:
    """Rename <x>.part -> <x>; returns the name the file now has.

    Anything left under a .part name is what services/recovery collects as an
    orphan, so finalizing has to consume the temp name or the same bytes get
    filed a second time as a separate "(recovered)" library item.
    """
    if path.suffix != ".part":
        return path
    final = path.with_suffix("")
    try:
        path.replace(final)
        return final
    except OSError:
        log.warning("could not drop .part from %s", path.name)
        return path


def engine_chain(
    room_url: str,
    outtmpl: str,
    cookiefile: str | None = None,
    quality: str | None = None,
) -> list[list[str]]:
    """Ordered engine commands: yt-dlp live first, one streamlink retry.

    Both engines take the same Netscape cookies.txt. Anonymous capture is not
    merely a login inconvenience: rooms that gate their top tier (or the room
    itself) behind an account hand a logged-out client the lower ladder and
    the recording silently lands at that quality, so the stored credential
    rides along whenever one exists.

    `quality` is the account's default_quality. Both lanes cap SOFTLY — they
    prefer the highest tier at or below the cap, and still record if a room
    only offers something above it. A hard filter would turn "I'd rather not
    fill the disk with 4K" into a capture that silently never happens.
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
        # Same reason the in-process probes send it (ytdlp.HTTP_HEADERS): with
        # yt-dlp's own Accept-Language, TikTok answers every page with a 200
        # and a "Site Maintenance" stub, and the capture dies claiming the room
        # is not live. A subprocess inherits none of the library options, so
        # both engines have to be told on the command line, in their own
        # spelling -- yt-dlp wants FIELD:VALUE, streamlink wants KEY=VALUE.
        "--add-header", f"Accept-Language:{ytdlp.BROWSER_ACCEPT_LANGUAGE}",
    ]
    streamlink_cmd = [
        "streamlink", "--quiet",
        "--http-header", f"Accept-Language={ytdlp.BROWSER_ACCEPT_LANGUAGE}",
    ]
    if cookiefile:
        ytdlp_cmd += ["--cookies", cookiefile]
        streamlink_cmd += ["--http-cookies-file", cookiefile]

    # yt-dlp: -S res:N, the same format_sort the VOD path uses, for the same
    # reason — these rooms are vertical, so a `height<=N` FILTER reads
    # 1080x1920 as 1920 and throws the whole 1080p ladder away. `res` sorts on
    # the smaller dimension, the way a person reads it.
    sort = ytdlp.quality_sort(quality)
    if sort:
        ytdlp_cmd += ["-S", ",".join(sort)]

    # streamlink: a comma-separated stream list is a PREFERENCE ORDER, so
    # "1080p,best" takes 1080p when the plugin names it and falls back rather
    # than failing. Not every plugin names streams by resolution — the
    # bilibili one yields "httpstream" and "hls" and picks quality server-side
    # via its own qn parameter — so for those the cap simply no-ops into
    # `best`, which is the pre-existing behaviour and still records.
    stream_pref = f"{quality},best" if sort else "best"

    return [
        [*ytdlp_cmd, room_url, "-o", outtmpl],
        [*streamlink_cmd, room_url, stream_pref, "-o", outtmpl],
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
        # The capture is the ONLY place the browser lane is worth its cost:
        # one Chrome run per recording buys the HEVC ladder, where switching it
        # on globally would spawn one per creator per poll sweep for a ladder
        # nothing is about to download. services/browser explains the rest.
        env={**os.environ, browser.ENABLE_ENV: "1"},
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
        # Set once teardown begins, so a supervision task that is between
        # engines does not start another one on the way out.
        self._shutting_down = False
        self.grace_s = TERMINATE_GRACE_S
        # Every recording with a live supervision task, from the moment it is
        # created -- _registry only learns of one once an engine has spawned.
        # A 'recording' row missing from here has nobody left to finalize it.
        self._supervised: set[int] = set()
        # recording_id -> why a stop was ordered, persisted as the row's error
        # (a floor stop has to say so, or it reads as the host ending).
        self._reasons: dict[int, str] = {}
        self._watchdog: asyncio.Task | None = None
        self.watchdog_s = WATCHDOG_S

    # ---- lifecycle -------------------------------------------------------

    def start_recording(self, recording_id: int) -> asyncio.Task | None:
        """Begin supervising a 'recording'-status LiveRecording row."""
        if recording_id in self._supervised:
            return None
        self._supervised.add(recording_id)
        return asyncio.create_task(self._supervise(recording_id))

    async def stop(
        self, recording_id: int, reason: str | None = None, as_status: str = "ended"
    ) -> bool:
        """Graceful stop: SIGINT to the group, grace window, then kill.
        Records 'ended' intent so the exit handler doesn't mark 'failed'.

        SIGINT, not SIGTERM, because the engines are Python: it arrives as
        KeyboardInterrupt, which yt-dlp's ExternalFD treats as a clean stop --
        it asks ffmpeg to quit, keeps the bytes and renames the .part. SIGTERM
        has no handler, so the process died where it stood and every user stop
        left a temp file the supervisor then read as "produced nothing".
        captured_file covers the deaths no signal choice can make graceful.

        A row nothing supervises any more has no engine to signal. Refusing
        it with a 409 left the Stop button dead on a capture that had been
        gone for five days; finalizing it is the only stop there is."""
        entry = self._registry.get(recording_id)
        if not entry or entry[0] is None:
            if recording_id not in self._supervised:
                return await self.reap_orphan(recording_id)
            return False
        proc = entry[0]
        self._intended[recording_id] = as_status
        if reason:
            self._reasons[recording_id] = reason
        try:
            _signal_group(proc, signal.SIGINT)
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
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                # The supervision task being cancelled is its business; the
                # CALLER being cancelled is not ours to swallow. The floor
                # watchdog calls this, and eating shutdown's cancel kept the
                # watchdog looping while shutdown() waited on it forever.
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    raise
        return True

    async def start_watchdog(self) -> None:
        """Begin the free-space and orphan checks (main.py lifespan).

        Separate from start() because it has to come AFTER reconcile_on_boot:
        at boot every leftover 'recording' row is unsupervised, and those are
        reconcile's to probe and retrigger, not the watchdog's to write off.
        """
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.watchdog_s)
            if self._shutting_down:
                return  # engines are being killed; there is nothing to guard
            try:
                await self.enforce_floor()
            except Exception:
                log.exception("space floor check failed")
            try:
                await self.reap_orphans()
            except Exception:
                log.exception("orphan reap failed")

    async def start(self) -> None:
        """Arm the supervisor for a fresh process life.

        shutdown() latches _shutting_down so a task sitting between engines
        does not spawn another on the way out, and nothing cleared it again.
        Harmless in production, where the process exits moments later -- but
        the supervisor is a module-level singleton, and conftest enters and
        leaves the app lifespan with `with TestClient(...)`, so the latch
        survived into every later test in the session. Nothing hits it today
        only because the recorder tests build their own supervisor; a test
        that recorded through the singleton would get no engine, no error and
        no capture, with the outcome depending on the random test order.

        Pairs with shutdown(), the way manager, poller and recovery already
        pair start with stop.
        """
        self._shutting_down = False

    async def shutdown(self) -> None:
        """Kill every registered engine child (main.py lifespan teardown)."""
        self._shutting_down = True
        if self._watchdog is not None:
            self._watchdog.cancel()
            await asyncio.gather(self._watchdog, return_exceptions=True)
            self._watchdog = None
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
            self._reasons.pop(recording_id, None)
            self._supervised.discard(recording_id)

    async def _supervise_inner(self, recording_id: int) -> None:
        async with _db() as session:
            rec = await session.get(models.LiveRecording, recording_id)
        if rec is None or rec.status != "recording":
            return

        out_path = recording_output_path(rec.platform, rec.creator, rec.started_at)
        capture_path = out_path.with_suffix(".flv")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Claim the path before a byte is written. recovery decides a .part is
        # an orphan by checking it against the output_path of every active
        # recording -- and this column used to stay NULL until finalize, so a
        # capture that was still running claimed nothing, and the sweep
        # remuxed the file out from under its own engine and registered the
        # result as a second, duplicate library item.
        async with _db() as session:
            claiming = await session.get(models.LiveRecording, recording_id)
            if claiming is not None:
                claiming.output_path = str(capture_path)
                await session.commit()
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
            # Read at capture start, not at watch-creation time: a recording
            # row can sit queued across a settings change, and the tier that
            # matters is the one wanted when the bytes actually land.
            from app.services.settings_store import aget_settings

            async with _db() as settings_session:
                quality = (await aget_settings(settings_session)).default_quality
            chain = engine_chain(
                rec.room_url,
                str(capture_path),
                str(cookiefile) if cookiefile else None,
                quality,
            )
            for cmd in chain:
                if self._shutting_down:
                    # The app is going down. The engine just killed leaves its
                    # .part; spawning the fallback only writes a few hundred KB
                    # under the FINISHED name before it is killed too, which is
                    # where every stray fragment sitting beside a large .part
                    # came from -- 655 KB of streamlink next to 646 MB of
                    # yt-dlp, measured on a deploy.
                    #
                    # Return rather than finalize: the row belongs to
                    # reconcile_on_boot, and a remux started here would be cut
                    # off mid-write by the container stop anyway.
                    return
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

        # Whatever the engine left behind, under either name (captured_file).
        captured = captured_file(capture_path)
        # Finalize even for a user stop: an intentionally ended recording is
        # still a recording someone wants to watch.
        final_path = await self._finalize_container(captured, out_path) if captured else None
        intended = self._intended.pop(recording_id, None) or (
            "ended" if chain_broken_by_stop else None
        )

        if intended:
            await self._finalize(
                recording_id, intended, final_path, self._reasons.pop(recording_id, None)
            )
        elif rc == 0 and captured:
            await self._finalize(recording_id, "finished", final_path, None)
        elif self._shutting_down:
            # The app is going down and the engine it killed was the LAST in
            # the chain, so the loop simply ended: the return above only runs
            # on the way to a next engine. This used to fall through to
            # 'failed' with no path -- "engine (streamlink) exited with code
            # -9" -- while the remux it had just finished sat on disk unseen,
            # 337 MB on one deploy, and the orphan sweep collects only .part
            # names so nothing would ever pick it up. What the engine wrote
            # is a recording someone wants to watch, the same as a user stop.
            await self._finalize(
                recording_id, "interrupted", final_path, "interrupted by app restart"
            )
        else:
            await self._finalize(
                recording_id, "failed", None, err or "engine produced no output"
            )

    async def _finalize_container(self, capture: Path, dst: Path) -> Path:
        """FLV capture -> MP4 for the library. Returns what to register.

        A failed remux keeps the FLV and registers that: a file the user has
        to work to open beats a recording that silently is not there. Either
        way the .part suffix goes -- see drop_part_suffix.
        """
        if await remux_to_mp4(capture, dst):
            capture.unlink(missing_ok=True)
            return dst
        return drop_part_suffix(capture)

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
            if status in ("finished", "ended", "interrupted"):
                rec.ended_at = models.utcnow()
            if out_path is not None:
                rec.output_path = str(out_path)
            else:
                # The path was claimed before the first byte so the orphan
                # sweep would not collect a running capture. Nothing came of
                # it, so stop pointing at a file that does not exist.
                rec.output_path = None
            # 'interrupted' arrives here only from a teardown that finished its
            # remux; reconcile_on_boot writes that status through
            # _patch_status and never brings a file with it.
            if status in ("finished", "ended", "interrupted") and out_path is not None and out_path.exists():
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

    # ---- watchdog ------------------------------------------------------------

    async def enforce_floor(self) -> int:
        """Stop and save every running capture once free space is below the
        floor. Returns how many were told to stop.

        Stopping costs the rest of the stream; not stopping cost everything
        else. The volume that holds the media also holds the database and
        Docker's own log, so a full disk did not just end the capture -- the
        row could not be finalized, the error could not be logged, and the
        orphan sat in 'recording' for five days, blocking that creator's
        every later live. Stopping at the floor leaves room for the remux
        and for the app to keep writing.
        """
        running = [
            rid
            for rid, (proc, _task) in self._registry.items()
            if proc is not None
            and proc.returncode is None
            and rid not in self._intended
        ]
        if not running:
            return 0
        status = await storage.space_status()
        if status is None or not status.below_floor:
            return 0
        reason = (
            f"stopped: free space {status.usage.free_pct:.1f}% "
            f"fell below the {status.floor_pct:g}% floor"
        )
        log.warning("%s; stopping %d capture(s)", reason, len(running))

        async def _stop(rid: int) -> None:
            # Gone since the snapshot: stop() would take it for an orphan and
            # remux it inline, on a disk that is already short.
            if rid not in self._registry:
                return
            # 'interrupted', not 'ended': the host did not end it, and it is
            # the status the retry endpoint accepts.
            await self.stop(rid, reason=reason, as_status="interrupted")

        # Together: each stop can wait out a 15s grace, and one that raises
        # must not leave the rest writing into the last of the disk.
        results = await asyncio.gather(
            *(_stop(rid) for rid in running), return_exceptions=True
        )
        for rid, result in zip(running, results):
            if isinstance(result, BaseException):
                log.error("floor stop failed for recording %s: %r", rid, result)
        return len(running)

    async def reap_orphans(self) -> int:
        """Finalize every 'recording' row that has no supervisor behind it.

        Nothing else would: the poller skips a creator with an active row,
        Stop found no engine to signal, and reconcile_on_boot only runs at a
        restart. A failed _finalize (the database write is the first thing a
        full disk takes) left the row there silently, so this retries every
        cycle until the write goes through.
        """
        cutoff = models.utcnow() - timedelta(seconds=ORPHAN_GRACE_S)
        async with _db() as session:
            ids = (
                (
                    await session.execute(
                        select(models.LiveRecording.id).where(
                            models.LiveRecording.status == "recording",
                            models.LiveRecording.started_at < cutoff,
                        )
                    )
                )
                .scalars()
                .all()
            )
        reaped = 0
        for rid in ids:
            if rid in self._supervised:
                continue
            if await self.reap_orphan(rid):
                log.warning("recording %s had no supervisor; finalized", rid)
                reaped += 1
        return reaped

    async def reap_orphan(self, recording_id: int) -> bool:
        """Finalize one unsupervised 'recording' row. True when it was one.

        Whatever the lost engine wrote is kept and registered, the same as a
        stopped capture: 'interrupted' with the bytes, 'failed' without.
        """
        if recording_id in self._supervised:
            return False
        # Claimed for the whole reap, and before the first await: Stop and the
        # watchdog can both arrive here, and two remuxes truncating the same
        # MP4 -- then two LibraryItems for one file_path -- is what follows.
        # stop(), reap_orphans() and this check all skip a claimed id.
        self._supervised.add(recording_id)
        try:
            return await self._reap_claimed(recording_id)
        finally:
            self._supervised.discard(recording_id)

    async def _reap_claimed(self, recording_id: int) -> bool:
        async with _db() as session:
            rec = await session.get(models.LiveRecording, recording_id)
        if rec is None or rec.status != "recording":
            return False
        final_path: Path | None = None
        if rec.output_path:
            capture = Path(rec.output_path)
            mp4 = capture.with_suffix(".mp4")
            captured = captured_file(capture)
            if captured is not None:
                final_path = await self._finalize_container(captured, mp4)
            elif mp4.is_file():
                # Remuxed by an earlier attempt whose database write failed;
                # the FLV is gone, so the MP4 is all there is to register.
                final_path = mp4
        if final_path is not None:
            await self._finalize(recording_id, "interrupted", final_path, ORPHAN_ERROR)
        else:
            await self._finalize(recording_id, "failed", None, ORPHAN_ERROR)
        return True

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
                space = await storage.space_status()
                if space is not None and not space.room_to_start:
                    # Same rule as the poller, which picks the room up again
                    # once there is room: a restart is no reason to record
                    # into the last of the disk.
                    events.publish(
                        {
                            "type": "watch.skipped_space_floor",
                            "creator": rec.creator,
                            "free_pct": round(space.usage.free_pct, 1),
                            "floor_pct": space.floor_pct,
                        }
                    )
                    continue
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
