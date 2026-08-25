"""build_opts invariants: no EmbedThumbnail postprocessor.

Embedding crashes yt-dlp post-download when the output container is FLV
(bilibili live captures land as single .flv, no merge step) — a finished
capture would flip to 'failed'. The sidecar thumbnail (writethumbnail) is
what the Library uses instead.
"""

from types import SimpleNamespace

from app.services.ytdlp import QUALITY_CHOICES, build_opts, quality_sort


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


# ---- default_quality -> resolution cap ---------------------------------------


def test_best_applies_no_cap():
    for q in (None, "", "best"):
        assert quality_sort(q) is None
        assert "format_sort" not in build_opts(_job(selected_quality=q))


def test_cap_uses_res_sort_not_a_height_filter():
    """The cap MUST be format_sort. These platforms serve vertical video, so
    `height<=1080` on a 1080x1920 ladder drops to 480x852 — yt-dlp's `res`
    reads the smaller dimension and lands on the tier a person means."""
    opts = build_opts(_job(selected_quality="1080p"))
    assert opts["format_sort"] == ["res:1080"]
    assert "height" not in opts["format"]


def test_every_offered_choice_maps():
    for q in QUALITY_CHOICES:
        sort = quality_sort(q)
        assert sort is None if q == "best" else sort == [f"res:{q[:-1]}"]


def test_explicit_format_id_beats_the_default_cap():
    """A quality picked in the dropdown names the exact stream; a cap on top
    could only fight it."""
    opts = build_opts(_job(format_id="100028", selected_quality="360p"))
    assert opts["format"] == "100028"
    assert "format_sort" not in opts


def test_settings_default_applies_when_job_has_no_snapshot():
    opts = build_opts(_job(), {"default_quality": "720p"})
    assert opts["format_sort"] == ["res:720"]


def test_job_snapshot_beats_live_setting():
    """selected_quality is stamped at queue time so changing Settings does
    not re-aim jobs already in the queue."""
    opts = build_opts(_job(selected_quality="480p"), {"default_quality": "2160p"})
    assert opts["format_sort"] == ["res:480"]


def test_garbage_quality_is_ignored_not_crashed():
    assert quality_sort("potato") is None
    assert "format_sort" not in build_opts(_job(selected_quality="potato"))


# ---- probe: anthologies vs creator listings ---------------------------------


class _CapturingYDL:
    """Stands in for YoutubeDL so probe's opts can be inspected offline."""

    captured: dict = {}

    def __init__(self, opts):
        type(self).captured = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return {"formats": []}

    def sanitize_info(self, info):
        return info


def _probe_opts(monkeypatch, **kw) -> dict:
    import app.services.ytdlp as mod

    monkeypatch.setattr(mod, "YoutubeDL", _CapturingYDL)
    mod.probe("https://www.bilibili.com/video/BV1x", **kw)
    return _CapturingYDL.captured


def test_probe_sets_noplaylist_for_a_video(monkeypatch):
    """A bilibili anthology resolves to a playlist whose formats live in
    entries[]; the quality dropdown reads the top level and found none."""
    assert _probe_opts(monkeypatch)["noplaylist"] is True


def test_probe_leaves_flat_listings_enumerating(monkeypatch):
    """The poller's creator listing IS the playlist — noplaylist here would
    stop watchlist polling from seeing any posts at all."""
    assert "noplaylist" not in _probe_opts(monkeypatch, extract_flat=True)


def test_probe_keeps_playlist_items_cap(monkeypatch):
    opts = _probe_opts(monkeypatch, extract_flat=True, playlist_items="1-5")
    assert opts["playlist_items"] == "1-5"
