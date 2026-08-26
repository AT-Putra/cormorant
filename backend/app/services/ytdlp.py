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
from yt_dlp.globals import plugin_dirs as _plugin_dirs
from yt_dlp.plugins import load_all_plugins as _load_all_plugins

from app.config import MEDIA_ROOT

# Root passed to yt-dlp as a plugin search directory. Its children are the
# roots yt-dlp scans, so the tree is <PLUGIN_ROOT>/vd/yt_dlp_plugins/extractor
# — one level deeper than it looks like it should be, by design of
# plugins.candidate_plugin_paths(), which iterdir()s what you hand it.
PLUGIN_ROOT = Path(__file__).resolve().parent.parent / "ytdlp_plugins"

# yt-dlp only wires plugins up from its CLI entrypoint, so a library caller
# has to do it by hand: set the global search path, then load, BEFORE any
# YoutubeDL is constructed (each instance snapshots the extractor registry).
# "default" is kept first so a user's own ~/.config plugins still load.
_plugin_dirs.value = ["default", str(PLUGIN_ROOT)]
_load_all_plugins()


class AbortDownload(Exception):
    """Raised inside a progress hook to stop yt-dlp mid-flight (pause/cancel).

    .part/fragment state is left on disk; resume re-invokes with continuedl.
    """


# A resolution cap goes through format_sort, NEVER through a format filter.
# These platforms serve vertical video: bilibili reports 1080p as 1080x1920,
# so `bestvideo*[height<=1080]` throws away the whole 1080p AND 720p ladder
# and silently lands on 480x852 — measured, not theorised. yt-dlp's `res`
# sort field uses the smaller dimension, so it reads 1080x1920 as 1080 the
# way a person does. Ties below the cap fall through to yt-dlp's own order
# (quality, fps, codec), which is what "best 1080p" should mean.
QUALITY_CHOICES = ("best", "2160p", "1440p", "1080p", "720p", "480p", "360p")


def quality_sort(quality: str | None) -> list[str] | None:
    """format_sort fields capping resolution, or None for 'best'/unset."""
    if not quality or quality == "best":
        return None
    digits = quality.removesuffix("p")
    return [f"res:{digits}"] if digits.isdigit() else None


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
        # ponytail: no EmbedThumbnail PP — it hard-crashes post-download when
        # the output container is FLV (bilibili live captures), failing a
        # finished job. writethumbnail keeps the sidecar .jpg which the
        # Library uses directly. Re-add per-container embedding only if
        # cover art inside files becomes a requirement.
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
        opts.setdefault("postprocessors", []).insert(
            0, {"key": "FFmpegExtractAudio", "preferredcodec": audio}
        )
    # An explicit per-job format_id already names the exact stream to fetch,
    # so a cap on top could only fight it; the dropdown wins over the default.
    if not getattr(job, "format_id", None):
        sort = quality_sort(
            getattr(job, "selected_quality", None) or s.get("default_quality")
        )
        if sort:
            opts["format_sort"] = sort
    if extra:
        opts.update(extra)
    return opts


def probe(
    url: str,
    cookiefile: str | None = None,
    *,
    extract_flat: bool = False,
    playlist_items: str | None = None,
) -> dict:
    """Full extraction, or flat listing (channels/timelines) when
    extract_flat=True. Synchronous — run via to_thread.

    playlist_items ('1', '1-5', ...) caps how far a channel listing is walked:
    without it yt-dlp pages through a creator's whole upload history, which is
    both slow and a fast route to a rate-limit block on identity probes.
    """
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": extract_flat,
    }
    if not extract_flat:
        # Anthologies (bilibili 合集) resolve to a playlist whose formats sit
        # one level down in entries[], so a quality probe found none at the
        # top level and the dropdown silently vanished — after spending ~54s
        # walking all 22 parts. build_opts already downloads with noplaylist,
        # so this only makes the probe describe the part that will be fetched.
        # Guarded on extract_flat: the poller's creator listing IS the
        # playlist, and must keep enumerating.
        opts["noplaylist"] = True
    if playlist_items:
        opts["playlist_items"] = playlist_items
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
