"""Downloads API: probe, create, list, detail, pause/resume/cancel/retry.

Threading discipline (plan Decision C): every yt-dlp engine call runs via
asyncio.to_thread; the event loop never touches the extractor.
"""

import asyncio
import functools

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas
from app.db import get_session
from app.models import DownloadJob
from app.routers.credentials import aget_cookiefile
from app.services import ytdlp
from app.services.settings_store import aget_settings
from app.services.downloader import TERMINAL, manager
from app.util.platform import detect_platform


def get_manager():
    """Indirection so tests can stub the manager."""
    return manager

router = APIRouter(prefix="/api/downloads", tags=["downloads"])

# Formats worth showing in a quality dropdown: real video+audio muxes and
# progressive files. Storyboards and fragment manifests are dropped outright;
# tiny ladders are demoted rather than dropped, and resurface when a video has
# nothing above the floor (see _quality_options).
_MIN_TBR = 200.0


def _quality_options(info: dict) -> list[schemas.QualityOption]:
    # The floor is a PREFERENCE, not a filter: a video whose whole ladder sits
    # under _MIN_TBR would otherwise return [], and Queue.tsx hides the picker
    # behind formats.length > 0 — an empty dropdown with nothing to explain it,
    # the same silent failure anthologies used to cause. Thin formats are held
    # back and used only when nothing else survives.
    kept: list[schemas.QualityOption] = []
    thin: list[schemas.QualityOption] = []
    for f in info.get("formats") or []:
        if not f.get("format_id"):
            continue
        if f.get("vcodec") == "none" and f.get("acodec") == "none":
            continue  # storyboards / manifests
        tbr = f.get("tbr")
        bucket = thin if (tbr is not None and tbr < _MIN_TBR) else kept
        bucket.append(
            schemas.QualityOption(
                format_id=str(f["format_id"]),
                ext=f.get("ext") or "",
                resolution=f.get("resolution"),
                fps=f.get("fps"),
                vcodec=f.get("vcodec"),
                acodec=f.get("acodec"),
                filesize_approx=f.get("filesize_approx"),
                tbr=tbr,
                format_note=f.get("format_note"),
                protocol=f.get("protocol"),
            )
        )
    fmts = kept or thin
    fmts.sort(
        key=lambda o: (
            o.tbr or 0,
            int(o.resolution.split("x")[1]) if o.resolution and "x" in o.resolution else 0,
        ),
        reverse=True,
    )
    return fmts


@router.post("/probe", response_model=schemas.ProbeResult)
async def probe_url(
    body: schemas.ProbeRequest, session: AsyncSession = Depends(get_session)
) -> schemas.ProbeResult:
    platform = detect_platform(body.url)
    if not platform:
        raise HTTPException(400, detail="Unsupported URL")
    cookiefile = await aget_cookiefile(platform)
    # The dropdown's 'Best available' entry is the no-format_id download path,
    # which build_opts caps with the default quality -- so the probe has to be
    # asked the same question, or it marks a tier the download will not take.
    settings = await aget_settings(session)
    sort = ytdlp.quality_sort(settings.default_quality)
    try:
        info = await asyncio.to_thread(
            functools.partial(
                ytdlp.probe,
                body.url,
                str(cookiefile) if cookiefile else None,
                format_sort=sort,
            )
        )
    except Exception as exc:
        raise HTTPException(400, detail=f"Probe failed: {exc}") from exc
    finally:
        if cookiefile:
            cookiefile.unlink(missing_ok=True)
    return schemas.ProbeResult(
        platform=platform,
        title=info.get("title"),
        duration=info.get("duration"),
        formats=_quality_options(info),
        best_format_id=(str(info["format_id"]) if info.get("format_id") else None),
    )


def _job_out(job: DownloadJob) -> schemas.DownloadJobOut:
    return schemas.DownloadJobOut.model_validate(job)


async def _get_job_or_404(job_id: int, session: AsyncSession) -> DownloadJob:
    job = await session.get(DownloadJob, job_id)
    if not job:
        raise HTTPException(404, detail="Download not found")
    return job


@router.post("", response_model=schemas.DownloadJobOut, status_code=201)
async def create_download(
    body: schemas.DownloadJobCreate, session: AsyncSession = Depends(get_session)
) -> schemas.DownloadJobOut:
    platform = detect_platform(body.url)
    if not platform:
        raise HTTPException(400, detail="Unsupported URL")

    # Creator+title come from a metadata-only probe off the loop (Decision C).
    creator = ""
    title = body.url
    cookiefile = await aget_cookiefile(platform)
    try:
        info = await asyncio.to_thread(
            ytdlp.probe, body.url, str(cookiefile) if cookiefile else None
        )
        title = info.get("title") or title
        uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id")
        creator = uploader or platform
    except Exception:  # metadata fetch is best-effort; job still queues
        creator = platform
    finally:
        if cookiefile:
            cookiefile.unlink(missing_ok=True)

    job = DownloadJob(
        url=body.url,
        platform=platform,
        kind=body.kind,
        title=title,
        creator=creator,
        format_id=body.format_id,
        # Snapshot the account default the way the poller does: a later
        # Settings change must not retroactively re-aim a queued job.
        selected_quality=(await aget_settings(session)).default_quality,
        status="queued",
        is_auto=False,
        redownload_requested=body.redownload,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    get_manager().enqueue(job.id)
    return _job_out(job)


@router.get("", response_model=list[schemas.DownloadJobOut])
async def list_downloads(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[schemas.DownloadJobOut]:
    q = select(DownloadJob).order_by(DownloadJob.created_at.desc(), DownloadJob.id.desc())
    if status:
        q = q.where(DownloadJob.status == status)
    q = q.limit(limit).offset(offset)
    rows = (await session.execute(q)).scalars().all()
    return [_job_out(j) for j in rows]


@router.get("/{job_id}", response_model=schemas.DownloadJobOut)
async def get_download(
    job_id: int, session: AsyncSession = Depends(get_session)
) -> schemas.DownloadJobOut:
    return _job_out(await _get_job_or_404(job_id, session))


_TRANSITIONS: dict[str, set[str]] = {
    # pause only makes sense while work is pending/in-flight
    "pause": {"probing", "downloading"},
    # resume re-enqueues anything parked
    "resume": {"paused", "paused_space_floor"},
    # cancel aborts active or queued work
    "cancel": {"queued", "probing", "downloading", "paused", "paused_space_floor"},
    # retry resets a failed job back into the queue
    "retry": {"failed"},
}


async def _apply_transition(
    job: DownloadJob, action: str, session: AsyncSession
) -> DownloadJob:
    allowed = _TRANSITIONS[action]
    if job.status not in allowed:
        raise HTTPException(
            409, detail=f"Cannot {action} job in status '{job.status}'"
        )

    # Manager methods are synchronous (queue puts / abort-event sets).
    mgr = get_manager()
    if action == "pause":
        mgr.pause(job.id)
        job.status = "paused"
    elif action == "resume":
        job.status = "queued"
        await session.commit()
        mgr.enqueue(job.id)  # continuedl picks up .part files
        await session.refresh(job)
        return job
    elif action == "cancel":
        job.error = "cancelled"  # run_job keys off this after AbortDownload
        mgr.cancel(job.id)
        job.status = "failed"
    elif action == "retry":
        job.status = "queued"
        job.error = None
        await session.commit()
        mgr.enqueue(job.id)  # without this the job sits 'queued' forever
        await session.refresh(job)
        return job

    await session.commit()
    await session.refresh(job)
    return job


# Statuses with a worker actually inside run_job. Anything else -- queued,
# paused, the space-floor pause -- has no run to clear its manager state.
_IN_FLIGHT = ("probing", "downloading")


@router.delete("/{job_id}", status_code=204)
async def delete_download(
    job_id: int, session: AsyncSession = Depends(get_session)
) -> Response:
    """Drop a job from the queue. Active work is aborted first so the worker
    doesn't write to a row that no longer exists."""
    job = await _get_job_or_404(job_id, session)
    if job.status not in TERMINAL:
        # cancel() aborts the engine thread and sweeps .part files; run_job's
        # own status write then no-ops because the row is gone.
        get_manager().cancel(job.id)
    mid_run = job.status in _IN_FLIGHT
    await session.delete(job)
    await session.commit()
    if not mid_run:
        # Nothing is running to clear the cancel mark this leaves behind, and
        # SQLite hands the id straight to the next job created. See
        # DownloadManager.forget.
        get_manager().forget(job_id)
    return Response(status_code=204)


@router.post("/{job_id}/{action}", response_model=schemas.DownloadJobOut)
async def transition_download(
    job_id: int, action: str, session: AsyncSession = Depends(get_session)
) -> schemas.DownloadJobOut:
    if action not in _TRANSITIONS:
        raise HTTPException(404, detail="Unknown action")
    job = await _get_job_or_404(job_id, session)
    job = await _apply_transition(job, action, session)
    return _job_out(job)
