"""The Instagram live extractor this repo adds, and the two app-side wires
that make it reachable.

yt-dlp claims no /live/ URL at all, so `https://www.instagram.com/<user>/live/
?broadcast_id=<id>` came back "Unsupported URL" from the probe endpoint. The
plugin adds instagram:live; these tests pin the ways that silently stops
working:

  * the extractor not being registered (wrong directory nesting — yt-dlp
    iterdir()s the path it is handed), or its pattern drifting off the two URL
    shapes that reach it: the one a person copies out of a live page and the
    one poller.live_url() synthesizes;
  * the 200-OK HTML refusal being read as a successful answer;
  * an idle creator being worded in a way poller._OFFLINE_RE does not match,
    which is how tiktok buried the activity feed in poll_errors twice;
  * a watch added from a live URL storing Instagram's numeric account id,
    which formats into instagram.com/<pk>/ and is nobody's profile.
"""

import pytest
from yt_dlp.utils import UserNotLive

from app.services import poller, ytdlp  # noqa: F401  (import installs plugins)
from app.services.downloader import _is_stream_over
from app.util.platform import creator_id_from_url, normalize_url


def _live_ie_class():
    from yt_dlp.globals import extractors

    for cls in extractors.value.values():
        if getattr(cls, "IE_NAME", None) == "instagram:live":
            return cls
    pytest.fail("instagram:live extractor missing from the registry")


def test_plugin_is_registered():
    from yt_dlp.extractor.instagram import InstagramBaseIE

    cls = _live_ie_class()
    assert issubclass(cls, InstagramBaseIE)
    assert cls.__module__.startswith("yt_dlp_plugins.extractor")


@pytest.mark.parametrize(
    "url",
    [
        # The URL from the report that started this.
        "https://www.instagram.com/zonezicos/live/?broadcast_id=18101286095626884",
        "https://www.instagram.com/zonezicos/live/",
        "https://www.instagram.com/zonezicos/live",
        "https://instagram.com/zonezicos/live/",
        # What poller.live_url() builds for an instagram watch.
        "https://www.instagram.com/zonezicos/live",
    ],
)
def test_pattern_claims_live_urls(url):
    assert _live_ie_class().suitable(url), url


@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/zonezicos/",  # profile — instagram:user's
        "https://www.instagram.com/p/CxYzAbC/",  # post — InstagramIE's
        "https://www.instagram.com/stories/zonezicos/",
        "https://www.instagram.com/explore/live/",  # reserved first segment
        "https://www.instagram.com/zonezicos/live/extra",
    ],
)
def test_pattern_leaves_everything_else_alone(url):
    assert not _live_ie_class().suitable(url), url


def test_poller_points_an_instagram_watch_at_the_live_url():
    # The profile probe routes to instagram:user (_WORKING = False), which can
    # never report is_live; /live reaches this extractor instead.
    class W:
        platform = "instagram"
        creator_id = "zonezicos"
        live_url = None

    assert poller.live_url(W()) == "https://www.instagram.com/zonezicos/live"
    assert _live_ie_class().suitable(poller.live_url(W()))


def test_live_url_states_the_handle_not_the_account_id():
    # A watch added from a live URL must poll back the handle. Without this the
    # id comes from the probe's uploader_id, Instagram's numeric pk, and
    # _PROFILE_TEMPLATES formats it into a profile that does not exist.
    assert creator_id_from_url(
        "https://www.instagram.com/zonezicos/live/?broadcast_id=1810"
    ) == "zonezicos"
    assert creator_id_from_url("https://www.instagram.com/zonezicos/live") == "zonezicos"
    # The bare feature page names no creator.
    assert creator_id_from_url("https://www.instagram.com/live/") is None


def test_broadcast_id_survives_normalization():
    # It is content identity, not tracking noise: two broadcasts by the same
    # creator must not collapse onto one another in the dup check.
    assert "broadcast_id=18101286095626884" in normalize_url(
        "https://www.instagram.com/zonezicos/live/?broadcast_id=18101286095626884&igsh=x"
    )


class _FakeYDL:
    """Just enough downloader for InstagramBaseIE's cached properties: _app_id
    reads params['extractor_args'] and _can_impersonate asks whether a
    curl_cffi target is available."""

    params: dict = {}

    def _impersonate_target_available(self, target):
        return False


def _stub(*, answers=None, formats=None, logged_in=True):
    """The plugin with every network call replaced by a lookup table.

    Subclassed off the REGISTERED class, so the super() chain and attribute
    lookup behave exactly as they do in production; the real __init__ is
    skipped because nothing here needs a live downloader. `answers` maps a
    fragment of a request URL to the body Instagram would return for it, so
    each test states the API answers it is about and nothing else.
    """

    class Stub(_live_ie_class()):
        def __init__(self):
            self._downloader = _FakeYDL()
            self.screen = []
            self.warnings = []

        # InstagramBaseIE reads this off the cookie jar.
        @property
        def _is_logged_in(self):
            return logged_in

        def to_screen(self, msg, *a, **k):
            self.screen.append(msg)

        def report_warning(self, msg, *a, **k):
            self.warnings.append(msg)

        def _download_webpage(self, url, *a, **k):
            for fragment, body in (answers or {}).items():
                if fragment in url:
                    return body
            return None

        def _extract_mpd_formats(self, *a, **k):
            return formats or []

    return Stub()


def test_html_answer_is_a_refusal_not_a_result():
    # Signed out, www.instagram.com/api/v1/live/<id>/info/ answers 200 OK with
    # the HTML app shell — measured. Anything that checks status instead of
    # body reads that as success.
    ie = _stub(answers={"live/1810/info/": "<!DOCTYPE html><html>...</html>"})
    assert ie._api_json("live/1810/info/", "1810", "Downloading broadcast info") is None
    assert any("not accepted" in m for m in ie.screen)


def test_json_answer_is_parsed():
    ie = _stub(answers={"live/1810/info/": '{"broadcast_status": "active"}'})
    assert ie._api_json("live/1810/info/", "1810", "note") == {
        "broadcast_status": "active"
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"broadcast_status": "active", "dash_playback_url": "https://x/m.mpd"},
        {"broadcast": {"broadcast_status": "active", "dash_playback_url": "https://x/m.mpd"}},
    ],
)
def test_broadcast_is_found_at_either_level(payload):
    assert _live_ie_class()._as_broadcast(payload)["broadcast_status"] == "active"


def test_an_envelope_with_no_broadcast_keys_is_not_a_broadcast():
    assert _live_ie_class()._as_broadcast({"status": "ok", "reels": {}}) is None


def _extract(ie, url="https://www.instagram.com/zonezicos/live/"):
    return ie._real_extract(url)


def test_idle_creator_is_worded_the_way_the_app_matches_on():
    # Both matchers key off the words, not a status code. A spelling neither
    # knows publishes a watch.poll_error every sweep describing normal life.
    ie = _stub(answers={})  # every route answers nothing
    with pytest.raises(UserNotLive) as exc:
        _extract(ie)
    assert poller._OFFLINE_RE.search(str(exc.value))
    assert _is_stream_over(exc.value)


def test_no_session_is_a_login_error_not_an_idle_creator():
    # Instagram serves no live route at all to a signed-out caller, so
    # "offline" would be a guess dressed as an observation. The message has to
    # stay outside _OFFLINE_RE or the poller swallows it — and inside
    # watchlist._AUTH_RE, which is what turns it into advice on the add form.
    from app.routers.watchlist import _AUTH_RE

    ie = _stub(logged_in=False, answers={})
    with pytest.raises(Exception) as exc:
        _extract(ie)
    assert not poller._OFFLINE_RE.search(str(exc.value))
    assert _AUTH_RE.search(str(exc.value))


def test_a_stopped_broadcast_reads_as_idle_too():
    ie = _stub(answers={"live/1810/info/": '{"broadcast_status": "stopped"}'})
    with pytest.raises(UserNotLive) as exc:
        _extract(ie, "https://www.instagram.com/zonezicos/live/?broadcast_id=1810")
    assert poller._OFFLINE_RE.search(str(exc.value))


def test_a_running_broadcast_reports_live_and_names_the_handle():
    ie = _stub(
        answers={
            "live/1810/info/": (
                '{"id": "1810", "broadcast_status": "active",'
                ' "dash_playback_url": "https://cdn.example/m.mpd",'
                ' "broadcast_owner": {"username": "zonezicos", "pk": "42",'
                ' "full_name": "Zone Zicos"}}'
            )
        },
        formats=[{"url": "https://cdn.example/seg", "format_id": "dash-0"}],
    )
    info = _extract(ie, "https://www.instagram.com/zonezicos/live/?broadcast_id=1810")

    assert poller.is_live(info)
    assert info["id"] == "1810"
    assert info["channel"] == "zonezicos"
    assert info["uploader_id"] == "42"  # yt-dlp's convention: the numeric pk
    assert info["formats"]
    # The record the module docstring promises, so the first real capture
    # states the shape the API actually returned.
    assert any("manifests: dash_playback_url" in m for m in ie.screen)


def test_the_record_survives_quiet_mode(caplog):
    # to_screen alone reaches nobody: every caller in this app builds yt-dlp
    # quiet with no yt-dlp logger, and YoutubeDL.to_screen returns early on
    # quiet unless verbose is set too. The stdlib logger is what puts the shape
    # of a real API answer in the app log, which is the whole point of the line.
    ie = _stub(
        answers={
            "live/1810/info/": (
                '{"id": "1810", "broadcast_status": "active",'
                ' "dash_playback_url": "https://cdn.example/m.mpd"}'
            )
        },
        formats=[{"url": "https://cdn.example/seg", "format_id": "dash-0"}],
    )
    with caplog.at_level("INFO"):
        _extract(ie, "https://www.instagram.com/zonezicos/live/?broadcast_id=1810")
    assert any("manifests: dash_playback_url" in r.message for r in caplog.records)


def test_a_replay_is_downloadable_but_not_reported_as_live():
    ie = _stub(
        answers={
            "live/1810/info/": (
                '{"id": "1810", "broadcast_status": "post_live",'
                ' "dash_playback_url": "https://cdn.example/m.mpd"}'
            )
        },
        formats=[{"url": "https://cdn.example/seg", "format_id": "dash-0"}],
    )
    info = _extract(ie, "https://www.instagram.com/zonezicos/live/?broadcast_id=1810")
    assert info["live_status"] == "post_live"
    assert not poller.is_live(info)


def test_a_dead_broadcast_id_falls_through_to_the_running_one():
    # A pasted URL outlives the broadcast it names. The story-feed route knows
    # the current one; the switch is stated rather than done silently.
    ie = _stub(
        answers={
            "users/web_profile_info/": '{"data": {"user": {"id": "42"}}}',
            "feed/user/42/story/": (
                '{"broadcast": {"id": "9999", "broadcast_status": "active",'
                ' "dash_playback_url": "https://cdn.example/m.mpd"}}'
            ),
        },
        formats=[{"url": "https://cdn.example/seg", "format_id": "dash-0"}],
    )
    info = _extract(ie, "https://www.instagram.com/zonezicos/live/?broadcast_id=1810")
    assert info["id"] == "9999"
    assert any("1810 is gone" in m for m in ie.screen)
