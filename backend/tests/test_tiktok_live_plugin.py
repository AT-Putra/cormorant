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
from yt_dlp.utils import ExtractorError, UserNotLive, UserNotLive

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


# ---- repair 3: the room id no longer comes off the webpage ----------------
#
# TikTok now answers www.tiktok.com HTML with a JS challenge yt-dlp cannot run,
# so the base extractor finds no roomId and reports a live room as offline. The
# id is read off api-live/user/room instead, and the base extractor is handed
# the share/live form its own _VALID_URL accepts. These pin both halves: the
# id actually being read, and the page scrape still getting its chance when the
# endpoint says nothing.


def _ie_with_room_endpoint(monkeypatch, payload):
    """The real extractor with the room-id endpoint stubbed to `payload`."""
    from yt_dlp import YoutubeDL

    active, _ = _live_ie_class()
    ie = active(YoutubeDL({"quiet": True, "no_warnings": True}))
    monkeypatch.setattr(type(ie), "_download_json", lambda *a, **k: payload)
    return ie


def test_room_id_comes_from_the_json_endpoint(monkeypatch):
    ie = _ie_with_room_endpoint(
        monkeypatch, {"data": {"user": {"roomId": "7679151507547179797"}}})
    assert ie._room_id_from_api("lalaaaey") == "7679151507547179797"


@pytest.mark.parametrize("payload", [
    None,                                  # fatal=False turns a failure into this
    {},                                    # reshaped or empty answer
    {"data": {"user": {}}},                # user known, no room
    {"data": {"user": {"roomId": ""}}},    # how an idle creator reports it
])
def test_no_room_id_reads_as_none(monkeypatch, payload):
    ie = _ie_with_room_endpoint(monkeypatch, payload)
    assert ie._room_id_from_api("someone") is None


def _ie_recording_the_base_url(monkeypatch, room_id):
    """Plugin extractor that records the URL it hands the base extractor."""
    from yt_dlp import YoutubeDL

    active, builtin = _live_ie_class()
    seen = {}

    def fake_real_extract(self, url):
        seen["url"] = url
        return {"id": "room", "formats": []}

    monkeypatch.setattr(builtin, "_real_extract", fake_real_extract)
    ie = active(YoutubeDL({"quiet": True, "no_warnings": True}))
    monkeypatch.setattr(type(ie), "_room_id_from_api", lambda self, u: room_id)
    monkeypatch.setattr(type(ie), "_hevc_formats", lambda self, url, vid: [])
    return ie, seen


def test_live_url_is_rewritten_to_the_share_form(monkeypatch):
    ie, seen = _ie_recording_the_base_url(monkeypatch, "7679151507547179797")
    info = ie._real_extract("https://www.tiktok.com/@lalaaaey/live")
    assert seen["url"] == "https://m.tiktok.com/share/live/7679151507547179797"
    # the share form carries no handle, so the plugin has to put it back
    assert info["uploader"] == "lalaaaey"


def test_unresolvable_room_id_still_lets_the_page_be_tried(monkeypatch):
    ie, seen = _ie_recording_the_base_url(monkeypatch, None)
    ie._real_extract("https://www.tiktok.com/@lalaaaey/live")
    assert seen["url"] == "https://www.tiktok.com/@lalaaaey/live"


def _ie_whose_base_raises(monkeypatch, exc, room_id: str | None = "7679151507547179797"):
    """Plugin extractor whose base _real_extract raises `exc`."""
    from yt_dlp import YoutubeDL

    active, builtin = _live_ie_class()

    def fake_real_extract(self, url):
        raise exc

    monkeypatch.setattr(builtin, "_real_extract", fake_real_extract)
    ie = active(YoutubeDL({"quiet": True, "no_warnings": True}))
    monkeypatch.setattr(type(ie), "_room_id_from_api", lambda self, u: room_id)
    monkeypatch.setattr(type(ie), "_hevc_formats", lambda self, url, vid: [])
    return ie


def test_idle_room_keeps_the_handles_wording(monkeypatch):
    """The share/live rewrite must not rename the ordinary offline state.

    _call_api says "This livestream has ended" only because the rewrite took
    the handle away; with the handle it would raise UserNotLive. The whole app
    matches offline on the words, so the rewrite silently turned every idle
    tiktok sweep into a watch.poll_error.
    """
    ie = _ie_whose_base_raises(
        monkeypatch, ExtractorError("This livestream has ended", expected=True))
    with pytest.raises(UserNotLive) as caught:
        ie._real_extract("https://www.tiktok.com/@lalaaaey/live")
    assert "not currently live" in str(caught.value)


def test_a_handleless_share_url_is_left_alone(monkeypatch):
    """Nothing to restore the wording from, so the error passes through and
    poller._OFFLINE_RE is what has to recognise it."""
    ie = _ie_whose_base_raises(
        monkeypatch, ExtractorError("This livestream has ended", expected=True),
        room_id=None)
    with pytest.raises(ExtractorError) as caught:
        ie._real_extract("https://m.tiktok.com/share/live/7679151507547179797")
    assert "This livestream has ended" in str(caught.value)


def test_other_extractor_errors_are_not_reworded(monkeypatch):
    ie = _ie_whose_base_raises(
        monkeypatch, ExtractorError("Unable to download JSON metadata"))
    with pytest.raises(ExtractorError) as caught:
        ie._real_extract("https://www.tiktok.com/@lalaaaey/live")
    assert "Unable to download JSON metadata" in str(caught.value)


def test_hevc_ladder_still_reads_the_uploader_page(monkeypatch):
    """The rewrite must not send the HEVC scrape at the share URL, which
    carries none of the SIGI blob it needs."""
    ie, _seen = _ie_recording_the_base_url(monkeypatch, "42")
    asked = {}
    monkeypatch.setattr(
        type(ie), "_hevc_formats",
        lambda self, url, vid: asked.setdefault("url", url) and [])
    ie._real_extract("https://www.tiktok.com/@lalaaaey/live")
    assert asked["url"] == "https://www.tiktok.com/@lalaaaey/live"
