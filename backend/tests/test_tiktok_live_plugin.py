"""The TikTok live/detail override must actually be loaded, and must only
soften that one endpoint.

yt-dlp's TikTokLiveIE calls www.tiktok.com/api/live/detail as an HLS fallback
whenever room/info produced no mp4 format — which is every FLV-only room. That
endpoint answers HTTP 400 for everyone right now, and _call_api turns the
failure into UserNotLive, so a live channel reports "The channel is not
currently live" despite room/info having returned status 2 and a usable FLV
ladder (yt-dlp/yt-dlp#16850). The plugin catches the fallback's failure only.

These tests pin the two ways the fix silently stops working: the plugin not
being registered at all (wrong directory nesting, since yt-dlp iterdir()s the
path it is given), and the recorder subprocess — which never sees the
in-process load — losing its --plugin-dirs flags.
"""

import pytest
from yt_dlp.utils import ExtractorError, UserNotLive

from app.services import recorder, ytdlp  # noqa: F401  (import installs plugins)


def _live_ie_class():
    from yt_dlp.extractor.tiktok import TikTokLiveIE as builtin
    from yt_dlp.globals import extractors

    for cls in extractors.value.values():
        if getattr(cls, "IE_NAME", None) == "tiktok:live":
            return cls, builtin
    pytest.fail("tiktok:live extractor missing from the registry")


def test_plugin_overrides_the_builtin_live_extractor():
    active, builtin = _live_ie_class()
    assert active is not builtin, "plugin did not replace the built-in extractor"
    assert issubclass(active, builtin)
    assert active.__module__.startswith("yt_dlp_plugins.extractor")


def test_plugin_root_points_at_the_package_parent():
    # yt-dlp scans the CHILDREN of a --plugin-dirs path, so the importable
    # package must sit one level below PLUGIN_ROOT.
    assert (ytdlp.PLUGIN_ROOT / "vd" / "yt_dlp_plugins" / "extractor").is_dir()


def _stub_extractor(monkeypatch):
    """A live extractor whose every network call fails, warnings captured.

    Subclassed off the registered plugin class so attribute lookup and the
    super() chain behave exactly as they do in production; __init__ is skipped
    because nothing here touches the downloader.
    """
    active, builtin = _live_ie_class()
    monkeypatch.setattr(
        builtin, "_call_api",
        lambda *a, **k: (_ for _ in ()).throw(UserNotLive(video_id="someone")),
    )

    class Stub(active):
        def __init__(self):
            self.warnings = []

        def report_warning(self, msg, video_id=None):
            self.warnings.append(msg)

    return Stub()


def _call_api(ie, url):
    return ie._call_api(url, "room_id", "1234", "someone")


def test_fallback_failure_is_swallowed(monkeypatch):
    ie = _stub_extractor(monkeypatch)
    out = _call_api(ie, "https://www.tiktok.com/api/live/detail/?roomID=1")
    assert out == {}
    assert ie.warnings, "a swallowed fallback must still say so"


def test_primary_room_info_failure_still_raises(monkeypatch):
    ie = _stub_extractor(monkeypatch)
    with pytest.raises(ExtractorError):
        _call_api(ie, "https://webcast.tiktok.com/webcast/room/info")


def test_recorder_subprocess_carries_the_plugin_dirs():
    ytdlp_cmd = recorder.engine_chain("https://x/live", "/tmp/o.%(ext)s")[0]
    assert "--plugin-dirs" in ytdlp_cmd
    assert str(ytdlp.PLUGIN_ROOT) in ytdlp_cmd
    # "default" must survive too, or a user's own plugins get dropped.
    assert "default" in ytdlp_cmd


def test_recorder_yt_dlp_flags_are_accepted_by_the_cli():
    """Guard against library-option names leaking into the CLI invocation.

    "--noprogress" is the YoutubeDL param spelling; the CLI only knows
    "--no-progress" and exits 2 on the other, before parsing the URL — which
    made the yt-dlp engine a no-op and handed every capture to the streamlink
    retry without a word in the log.
    """
    import subprocess
    import sys

    cmd = recorder.engine_chain("https://x/live", "/tmp/o.%(ext)s")[0]
    flags = [a for a in cmd if a.startswith("--")]
    help_text = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--help"],
        capture_output=True, text=True, check=True,
    ).stdout
    unknown = [f for f in flags if f not in help_text]
    assert not unknown, f"yt-dlp CLI does not accept: {unknown}"


# ---- the HEVC ladder ---------------------------------------------------------

import json as _json


def _sigi_page(hevc_streams):
    """A /live page carrying a hevcStreamData blob shaped like TikTok's."""
    state = {
        "LiveRoom": {
            "liveRoomUserInfo": {
                "liveRoom": {
                    "hevcStreamData": {
                        "pull_data": {
                            "stream_data": _json.dumps({"data": hevc_streams})
                        }
                    }
                }
            }
        }
    }
    return (
        '<html><script id="SIGI_STATE" type="application/json">'
        + _json.dumps(state)
        + "</script></html>"
    )


def _stream(resolution, vbitrate, *, hls=True, flv=True, codec="h265"):
    main = {
        "sdk_params": _json.dumps(
            {"resolution": resolution, "vbitrate": vbitrate, "VCodec": codec}
        )
    }
    if hls:
        main["hls"] = f"https://cdn.example/{resolution}/index.m3u8"
    if flv:
        main["flv"] = f"https://cdn.example/{resolution}.flv"
    return {"main": main}


def _ie_with_page(monkeypatch, page):
    """The real extractor, wired to a real (silent) YoutubeDL.

    _get_sigi_state goes through _search_json, which reaches for the
    downloader on a miss — a bare instance would blow up on the empty-page
    case instead of returning [].
    """
    from yt_dlp import YoutubeDL

    active, _ = _live_ie_class()
    ie = active(YoutubeDL({"quiet": True, "no_warnings": True}))
    monkeypatch.setattr(type(ie), "_download_webpage", lambda *a, **k: page)
    return ie


def test_hevc_ladder_is_added_from_the_page(monkeypatch):
    ie = _ie_with_page(monkeypatch, _sigi_page({
        "uhd_60": _stream("1080x1920", 4000000),
        "hd": _stream("720x1280", 1350000),
    }))
    got = {f["format_id"]: f for f in ie._hevc_formats("https://x/live", "room-1")}
    assert set(got) == {
        "hevc-hls-uhd_60", "hevc-flv-uhd_60", "hevc-hls-hd", "hevc-flv-hd",
    }
    top = got["hevc-hls-uhd_60"]
    assert top["resolution"] == "1080x1920"
    assert top["tbr"] == 4000  # vbitrate is bits, tbr is kbits
    assert top["vcodec"] == "h265"
    assert top["protocol"] == "m3u8_native"


def test_flv_only_hevc_tiers_are_kept(monkeypatch):
    """The image pins ffmpeg 9, which demuxes HEVC-in-FLV (codec id 12).

    Under trixie's 7.1.5 these were unusable and got dropped; keeping them is
    the whole reason the Dockerfile stopped taking ffmpeg from apt. A room
    whose HEVC ladder is FLV-only falls back to H.264 720p without them.
    """
    ie = _ie_with_page(monkeypatch, _sigi_page({
        "uhd_60": _stream("1080x1920", 4000000, hls=False),
    }))
    got = [f["format_id"] for f in ie._hevc_formats("https://x/live", "room-1")]
    assert got == ["hevc-flv-uhd_60"]


def test_hls_is_preferred_when_a_tier_offers_both(monkeypatch):
    """Same picture, two containers — mp4 must sort ahead of flv.

    Both carry the same `quality`, so the tie falls to the base extractor's
    _format_sort_fields = ('quality', 'ext'), and yt-dlp's ext order puts mp4
    above flv. Pinning the shape here: equal quality, HLS listed first.
    """
    ie = _ie_with_page(monkeypatch, _sigi_page({
        "uhd_60": _stream("1080x1920", 4000000),
    }))
    got = ie._hevc_formats("https://x/live", "room-1")
    assert [f["format_id"] for f in got] == ["hevc-hls-uhd_60", "hevc-flv-uhd_60"]
    assert got[0]["quality"] == got[1]["quality"]
    assert (got[0]["ext"], got[1]["ext"]) == ("mp4", "flv")


def test_audio_only_rung_is_not_duplicated(monkeypatch):
    ie = _ie_with_page(monkeypatch, _sigi_page({
        "ao": _stream("", 0),
        "hd": _stream("720x1280", 1350000),
    }))
    got = [f["format_id"] for f in ie._hevc_formats("https://x/live", "room-1")]
    assert got == ["hevc-hls-hd", "hevc-flv-hd"]


def test_hevc_1080p60_outranks_the_h264_720p(monkeypatch):
    """Ranking is the whole point: `best` must not keep landing on 720p.

    The base extractor scores its H.264 ladder with
    qualities(('SD1','ld','SD2','sd','HD1','hd',...)), so 'hd' is 5. Anything
    the H.264 ladder cannot offer has to score above that.
    """
    active, _ = _live_ie_class()
    assert active._HEVC_QUALITY["uhd_60"] > 5
    assert active._HEVC_QUALITY["hd_60"] > 5
    # Same 720p picture at a lower bitrate: the compatible codec wins the tie.
    assert active._HEVC_QUALITY["hd"] < 5


def test_unreadable_page_leaves_the_base_formats_alone(monkeypatch):
    ie = _ie_with_page(monkeypatch, "")  # WAF challenge, redirect, anything
    assert ie._hevc_formats("https://x/live", "room-1") == []
