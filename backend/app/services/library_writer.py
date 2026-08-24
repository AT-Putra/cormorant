"""LibraryItem writer for finished downloads (US-013).

Called from downloader.run_job right after the job commits 'done'. Mirrors the
row shape recorder._finalize uses for captures. Never raises: a failed library
write must not flip an already-'done' job to 'failed'.
"""

import logging
from pathlib import Path

from sqlalchemy import select

from app import models

log = logging.getLogger(__name__)

# kind -> media_type (story renders as a video); audio wins via file extension
# because the audio postprocessor renames the final file to .mp3/.m4a.
_KIND_TO_MEDIA = {"video": "video", "story": "video", "images": "image_set"}
_AUDIO_EXTS = {".mp3", ".m4a"}
_THUMB_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _media_type(output_path: Path, kind: str) -> str:
    if output_path.suffix.lower() in _AUDIO_EXTS:
        return "audio"
    return _KIND_TO_MEDIA.get(kind, "video")


def _find_thumbnail(media: Path) -> str | None:
    """yt-dlp writethumbnail leaves <stem>.<img> next to the final file."""
    for ext in _THUMB_EXTS:
        cand = media.with_suffix(ext)
        if cand.exists():
            return str(cand)
    return None


async def write_library_item_for_job(
    session, job: models.DownloadJob
) -> models.LibraryItem | None:
    """Write a LibraryItem for a done job. Idempotent per file_path."""
    try:
        out = job.output_path
        if not out:
            return None
        media = Path(out)
        if not media.is_file():
            return None
        existing = await session.execute(
            select(models.LibraryItem).where(models.LibraryItem.file_path == out)
        )
        if existing.scalar_one_or_none():
            return None
        item = models.LibraryItem(
            file_path=out,
            thumbnail_path=_find_thumbnail(media),
            platform=job.platform,
            creator=job.creator,
            title=job.title,
            media_type=_media_type(media, job.kind),
            size_bytes=media.stat().st_size,
            duration_seconds=None,  # ponytail: needs ffprobe; upgrade when the player wants durations
        )
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item
    except Exception:
        log.exception("library write failed for job %s", getattr(job, "id", "?"))
        return None
