"""The Bilibili live override must be loaded, and must rename only the HLS
ladder.

BiliLiveIE copies getRoomPlayInfo's format_name into each format's `ext`, and
the HLS ladder is named "fmp4" -- not an extension, and not in yt-dlp's
allow-list, so process_info refuses the file the moment the HEVC HLS entry
wins `best` (which it does: HEVC-in-FLV sits at preference -100). The plugin
maps that one name to "mp4", the label yt-dlp itself gives fMP4 HLS.

These tests pin the plugin being registered at all and the mapping being
exactly one entry wide: FLV must stay FLV, and a name the plugin has never
seen must reach yt-dlp untouched so the allow-list still rules on it.
"""

import pytest

from app.services import ytdlp  # noqa: F401  (import installs plugins)


def _live_ie_class():
    from yt_dlp.extractor.bilibili import BiliLiveIE as builtin
    from yt_dlp.globals import extractors

    for cls in extractors.value.values():
        if getattr(cls, "IE_NAME", None) == builtin.IE_NAME:
            return cls, builtin
    pytest.fail("BiliLive extractor missing from the registry")


def test_plugin_overrides_the_builtin_live_extractor():
    active, builtin = _live_ie_class()
    assert active is not builtin, "plugin did not replace the built-in extractor"
    assert issubclass(active, builtin)
    assert active.__module__.startswith("yt_dlp_plugins.extractor")


def _fmt(format_name, *codecs):
    """One getRoomPlayInfo `format` entry, in the shape the API answers with."""
    return {
        "format_name": format_name,
        "codec": [
            {
                "codec_name": codec,
                "current_qn": 250,
                "base_url": f"/live-bvc/{codec}/",
                "url_info": [{"host": "https://cdn.example", "extra": "?sig=1"}],
            }
            for codec in codecs
        ],
    }


def _parse(format_name, *codecs):
    active, _ = _live_ie_class()
    ie = active.__new__(active)  # no downloader: _parse_formats never needs one
    return list(ie._parse_formats(250, _fmt(format_name, *codecs)))


def test_hls_ladder_is_named_mp4():
    formats = _parse("fmp4", "avc", "hevc")
    assert [f["ext"] for f in formats] == ["mp4", "mp4"]
    assert [f["vcodec"] for f in formats] == ["avc", "hevc"]
    assert formats[0]["url"] == "https://cdn.example/live-bvc/avc/?sig=1"


def test_flv_ladder_is_left_alone():
    assert [f["ext"] for f in _parse("flv", "avc")] == ["flv"]


def test_unknown_container_names_pass_through_to_the_allow_list():
    assert [f["ext"] for f in _parse("webm-ish", "avc")] == ["webm-ish"]
