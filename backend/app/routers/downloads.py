"""Downloads API: probe, create, list, detail, pause/resume/cancel/retry.

Threading discipline (plan Decision C): every yt-dlp engine call runs via
asyncio.to_thread; the event loop never touches the extractor.
"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas
from app.db import get_session
from app.models import DownloadJob
from app.routers.credentials import aget_cookiefile
from app.services import ytdlp
from app.services.downloader import TERMINAL, manager
from app.util.platform import detect_platform


def get_manager():
    """Indirection so tests can stub the manager."""
    return manager

router = APIRouter(prefix="/api/downloads", tags=["downloads"])

# Formats worth showing in a quality dropdown: real video+audio muxes and
# progressive files; drop storyboards, m3u8 fragment manifests, tiny audio-only
# ladders unless they're the only thing available.
_MIN_TBR = 200.0


def _quality_options(info: dict) -> list[schemas.QualityOption]:
    fmts = []
    for f in info.get("formats") or []:
        if not f.get("format_id"):
            continue
        if f.get("vcodec") == "none" and f.get("acodec") == "none":
            continue  # storyboards / manifests
        tbr = f.get("tbr")
        if tbr is not None and tbr < _MIN_TBR:
            continue
        fmts.append(
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
    fmts.sort(
        key=lambda o: (
            o.tbr or 0,
            int(o.resolution.split("x")[1]) if o.resolution and "x" in o.resolution else 0,
        ),
        reverse=True,
    )
    return fmts


@router.post("/probe", response_model=schemas.ProbeResult)
async def probe_url(body: schemas.ProbeRequest) -> schemas.ProbeResult:
    platform = detect_platform(body.url)
    if not platform:
        raise HTTPException(400, detail="Unsupported URL")
    cookiefile = await aget_cookiefile(platform)
    try:
        info = await asyncio.to_thread(
            ytdlp.probe, body.url, str(cookiefile) if cookiefile else None
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
    await session.delete(job)
    await session.commit()
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
