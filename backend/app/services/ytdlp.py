"""Thin yt-dlp wrapper (plan step 6 / Decision C).

Every function here is SYNCHRONOUS — callers must run them via
asyncio.to_thread; progress hooks marshal back via loop.call_soon_threadsafe.
No FastAPI/DB imports on purpose: this module is trivially mockable.
"""

import queue
import threading
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL

from app.config import MEDIA_ROOT


class AbortDownload(Exception):
    """Raised inside a progress hook to stop yt-dlp mid-flight (pause/cancel).

    .part/fragment state is left on disk; resume re-invokes with continuedl.
    """


def _sanitize(component: str) -> str:
    return component.strip().replace("/", "_").replace("\\", "_") or "_"


def output_dir(job, settings: dict | None = None) -> Path:
    """MEDIA_ROOT / <folder template> — '{platform}/{creator}' by default.

    Recordings (US-008) pass a filename template embedding started_at so
    re-captures never collide or trip the dup check.
    """
    s = settings or {}
    folder = (s.get("folder_template") or "{platform}/{creator}").format(
        platform=_sanitize(getattr(job, "platform", "")),
        creator=_sanitize(getattr(job, "creator", "")),
    )
    return MEDIA_ROOT / folder


def build_opts(job, settings: dict | None = None, *, extra: dict | None = None) -> dict[str, Any]:
    """YoutubeDL options for a DownloadJob.

    settings keys: folder_template, container ('mp4'|'mkv'), subs (bool),
    audio ('mp3'|'m4a'|None). `extra` merges raw yt-dlp opts last (used to
    inject progress_hooks).
    """
    s = settings or {}
    if getattr(job, "kind", "") == "images":
        # Image posts arrive as playlists; number items (AC15), skip download.
        name = "%(playlist_index)03d-%(title).120B.%(ext)s"
    else:
        name = "%(title).120B.%(ext)s"
    opts: dict[str, Any] = {
        "format": getattr(job, "format_id", None) or "bestvideo*+bestaudio/best",
        "merge_output_format": s.get("container", "mp4"),
        "outtmpl": str(output_dir(job, s) / name),
        "writethumbnail": True,
        "continuedl": True,
        "retries": 5,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,  # hooks still fire
        "postprocessors": [{"key": "EmbedThumbnail", "already_have_thumbnail": True}],
    }
    if getattr(job, "kind", "") == "images":
        opts["skip_download"] = True
    else:
        opts["noplaylist"] = True
    if s.get("subs"):
        opts["writesubtitles"] = True
        opts["subtitlesformat"] = "vtt/srt/best"
        opts["convertsubtitles"] = "srt"
    audio = s.get("audio")
    if audio:
        opts["postprocessors"].insert(
            0, {"key": "FFmpegExtractAudio", "preferredcodec": audio}
        )
    if extra:
        opts.update(extra)
    return opts


def probe(
    url: str, cookiefile: str | None = None, *, extract_flat: bool = False
) -> dict:
    """Full extraction, or flat listing (channels/timelines) when
    extract_flat=True. Synchronous — run via to_thread."""
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": extract_flat,
    }
    if cookiefile:
        opts["cookiefile"] = cookiefile
    with YoutubeDL(opts) as ydl:
        return ydl.sanitize_info(ydl.extract_info(url, download=False))


def download(opts: dict, url: str) -> dict:
    """Run a download with prebuilt opts. Synchronous — run via to_thread."""
    with YoutubeDL(opts) as ydl:
        return ydl.sanitize_info(ydl.extract_info(url, download=True))


def final_path(info: dict) -> str | None:
    """Best-effort output filepath from an extract_info(download=True) result."""
    rd = info.get("requested_downloads") or []
    return rd[0].get("filepath") if rd else None


_PROGRESS_KEYS = (
    "status",
    "downloaded_bytes",
    "total_bytes",
    "total_bytes_estimate",
    "filename",
    "fragment_index",
    "fragment_count",
    # Live streams have no known total, so percent is meaningless for them;
    # speed + elapsed are the only evidence that capture is still advancing.
    "speed",
    "elapsed",
)


def make_progress_hook(
    loop: Any,  # unused: the queue is thread-safe, see hook() below
    progress_queue: queue.Queue,
    abort_event: threading.Event,
):
    """Build a yt-dlp progress hook for one job (runs on the engine thread).

    Raises AbortDownload when abort_event is set (the only way to stop a
    to_thread download), else marshals the payload to the event loop.
    """

    def hook(d: dict) -> None:
        if abort_event.is_set():
            raise AbortDownload("aborted")
        payload = {k: d[k] for k in _PROGRESS_KEYS if d.get(k) is not None}
        # queue.Queue is already thread-safe, so put directly from the engine
        # thread. Routing through call_soon_threadsafe instead deferred every
        # payload to the event loop, which is parked in `await to_thread(...)`
        # for the whole download — so progress only landed after the job had
        # already finished, and the UI never left 'probing'.
        try:
            progress_queue.put_nowait(payload)
        except Exception:  # pragma: no cover - unbounded queue
            pass

    return hook
