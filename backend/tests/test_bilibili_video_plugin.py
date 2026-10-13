"""The Bilibili video override must be loaded, and must give every stream a
bounded HTTP chunk size.

bilibili's upos CDN cuts open-ended `Range: bytes=N-` downloads part-way
(job 14: "489 bytes read, 1601185267 more expected"), while bounded ranges
finish. The plugin sets yt-dlp's per-format `http_chunk_size`, and leaves the
live extractor alone: a bounded Range has no place on an endless FLV stream.
"""

import pytest

from app.services import ytdlp  # noqa: F401  (import installs plugins)


def _active(builtin):
    from yt_dlp.globals import extractors

    for cls in extractors.value.values():
        if getattr(cls, "IE_NAME", None) == builtin.IE_NAME:
            return cls
    pytest.fail(f"{builtin.IE_NAME} missing from the registry")


def test_plugin_overrides_the_builtin_video_extractor():
    from yt_dlp.extractor.bilibili import BiliBiliIE as builtin

    active = _active(builtin)
    assert active is not builtin, "plugin did not replace the built-in extractor"
    assert issubclass(active, builtin)
    assert active.__module__.startswith("yt_dlp_plugins.extractor")


def _play_info():
    """A playurl `data` answer in the shape the API sends: DASH video + audio."""
    base = "https://upos.example/upgcxcode/xx/xx/1/1-1-{}.m4s?sig=1"
    return {
        "dash": {
            "video": [{"id": 112, "baseUrl": base.format(30112), "codecs": "avc1",
                       "width": 1920, "height": 1080, "bandwidth": 3000000}],
            "audio": [{"id": 30280, "baseUrl": base.format(30280),
                       "codecs": "mp4a.40.2", "bandwidth": 320000}],
        },
    }


def test_every_stream_gets_a_bounded_chunk_size():
    from yt_dlp.extractor.bilibili import BiliBiliIE as builtin
    from yt_dlp_plugins.extractor.bilibili_video import HTTP_CHUNK_SIZE

    active = _active(builtin)
    ie = active.__new__(active)  # no downloader: extract_formats never needs one
    formats = ie.extract_formats(_play_info())
    assert [f["format_id"] for f in formats] == ["30280", "30112"]
    for f in formats:
        assert f["downloader_options"]["http_chunk_size"] == HTTP_CHUNK_SIZE


def test_live_extractor_stays_unchunked():
    """BiliLiveIE's FLV is an endless stream; only the video override chunks."""
    from yt_dlp.extractor.bilibili import BiliLiveIE as builtin

    active = _active(builtin)
    ie = active.__new__(active)
    flv = {
        "format_name": "flv",
        "codec": [{
            "codec_name": "avc", "current_qn": 250, "base_url": "/live-bvc/avc/",
            "url_info": [{"host": "https://cdn.example", "extra": "?sig=1"}],
        }],
    }
    formats = list(ie._parse_formats(250, flv))
    assert formats
    assert all("downloader_options" not in f for f in formats)
