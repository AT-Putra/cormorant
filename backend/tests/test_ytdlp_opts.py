"""build_opts invariants: no EmbedThumbnail postprocessor.

Embedding crashes yt-dlp post-download when the output container is FLV
(bilibili live captures land as single .flv, no merge step) — a finished
capture would flip to 'failed'. The sidecar thumbnail (writethumbnail) is
what the Library uses instead.
"""

from types import SimpleNamespace

from app.services.ytdlp import build_opts


def _job(**kw):
    base = {"platform": "bilibili", "creator": "c", "kind": "video", "format_id": None}
    base.update(kw)
    return SimpleNamespace(**base)


def test_no_embed_thumbnail_postprocessor():
    opts = build_opts(_job())
    pps = [p["key"] for p in opts.get("postprocessors", [])]
    assert "EmbedThumbnail" not in pps


def test_writethumbnail_stays_for_library():
    opts = build_opts(_job())
    assert opts["writethumbnail"] is True


def test_audio_extract_pp_survives():
    """Audio postprocessing is unaffected by removing thumbnail embedding."""
    job = _job(kind="story")
    opts = build_opts(job, {"audio": "mp3"})
    keys = [p["key"] for p in opts.get("postprocessors", [])]
    assert "FFmpegExtractAudio" in keys
