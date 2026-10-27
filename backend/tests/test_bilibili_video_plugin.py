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


BASE = "https://akam.example/upgcxcode/xx/xx/1/1-1-{}.m4s?sig=1"
BACKUP = "https://cosov.example/upgcxcode/xx/xx/1/1-1-{}.m4s?sig=2"


def _play_info():
    """A playurl `data` answer in the shape the API sends: DASH video + audio,
    each with the backupUrl list bilibili sends beside the baseUrl."""
    return {
        "dash": {
            "video": [{"id": 112, "baseUrl": BASE.format(30112),
                       "backupUrl": [BACKUP.format(30112)], "codecs": "avc1",
                       "width": 1920, "height": 1080, "bandwidth": 3000000}],
            "audio": [{"id": 30280, "baseUrl": BASE.format(30280),
                       "backupUrl": [BACKUP.format(30280)],
                       "codecs": "mp4a.40.2", "bandwidth": 320000}],
        },
    }


def _video_ie(**bilibili_args):
    """The active video extractor, attached to a YoutubeDL carrying these
    extractor args. Constructing one makes no request."""
    from yt_dlp import YoutubeDL
    from yt_dlp.extractor.bilibili import BiliBiliIE as builtin

    active = _active(builtin)
    args = {k: [v] for k, v in bilibili_args.items()}
    return active(YoutubeDL({"quiet": True, "extractor_args": {"bilibili": args}}))


def test_every_stream_gets_a_bounded_chunk_size():
    from yt_dlp_plugins.extractor.bilibili_video import HTTP_CHUNK_SIZE

    formats = _video_ie().extract_formats(_play_info())
    assert [f["format_id"] for f in formats] == ["30280", "30112"]
    for f in formats:
        assert f["downloader_options"]["http_chunk_size"] == HTTP_CHUNK_SIZE


def test_streams_stay_on_the_base_url_by_default():
    """The base host is the fast one when it works (job 16: 1.6 GB in ~2 min)."""
    formats = _video_ie().extract_formats(_play_info())
    assert {f["url"] for f in formats} == {BASE.format(30112), BASE.format(30280)}


def test_mirror_arg_moves_every_stream_to_its_backup_url():
    """Job 19: the akamai base died at byte 63274104 on every resume, while
    the backupUrl served that offset."""
    formats = _video_ie(mirror="1").extract_formats(_play_info())
    assert {f["url"] for f in formats} == {BACKUP.format(30112), BACKUP.format(30280)}


def test_a_stream_without_a_backup_keeps_its_base_url():
    info = _play_info()
    del info["dash"]["audio"][0]["backupUrl"]
    by_id = {f["format_id"]: f["url"]
             for f in _video_ie(mirror="1").extract_formats(info)}
    assert by_id == {"30112": BACKUP.format(30112), "30280": BASE.format(30280)}


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
