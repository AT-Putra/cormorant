"""Instagram live, which yt-dlp does not extract at all.

Pasting a live URL into the app answered

    ERROR: Unsupported URL: https://www.instagram.com/<user>/live/?broadcast_id=<id>

and that message was accurate rather than broken. yt-dlp ships InstagramIE
(/p/, /reel/, /tv/), InstagramStoryIE, InstagramTagIE and InstagramUserIE, and
not one of their _VALID_URLs has a /live/ branch -- InstagramUserIE stops at
the handle, so /<user>/live/ falls off the end of every pattern and no
extractor claims the URL. There is nothing to repair here; unlike
tiktok_live.py this file ADDS an extractor rather than subclassing a broken
one.

Where the stream actually is
----------------------------
A broadcast is described by www.instagram.com/api/v1/live/<broadcast_id>/info/
-- the same private JSON API InstagramBaseIE already talks to for posts and
stories, which is why this subclasses it: _API_BASE_URL, _api_headers, the
app-id handling and the sessionid check all come along for free and stay in
step with upstream. That answer carries dash_playback_url, an MPEG-DASH
manifest yt-dlp parses into an ordinary format ladder, plus
dash_abr_playback_url and, on some answers, the manifest inline as
dash_manifest. All three are read, in that order.

A URL with no ?broadcast_id= -- the shape the poller synthesizes for a watch,
and the shape a person types -- names no broadcast, so the running one is read
off live/web_info/?target_user_id=<account id>, the route instagram.com's own
/<user>/live/ page uses. The account id comes from ?target_user_id= (which
the app stores with a watch), a cache of every owner seen, or as a last
resort users/web_profile_info/, which Instagram rate-limits (see _USER_IDS).
The story feed (feed/user/<id>/story/) that used to carry the broadcast
refused a signed-in session outright on 2026-10-27.

Authentication is not optional, and the refusal lies
----------------------------------------------------
Every route above is account-only, and Instagram refuses in a shape that has
to be classified by BODY, never by status -- the same rule services/browser
and tiktok_live.py state from their own side. Measured 2026-09-02,
anonymously, against a real broadcast id:

    www.instagram.com/api/v1/live/<id>/info/  ->  200 OK, the HTML app shell
    i.instagram.com/api/v1/live/<id>/info/    ->  404, an HTML "Page Not Found"

The first is the one that costs something: 200 with HTML is exactly the answer
a status check reads as success, and the JSON parse is the only thing that
notices. So _api_json looks at the first byte of the body and reads anything
that is not JSON as "Instagram did not accept this session", which is what
raise_login_required exists to say.

What is NOT verified
--------------------
The field names above are the observed shape of the live API, but they could
not be checked against an AUTHENTICATED response while this was written: the
user's Instagram cookies stay Fernet-encrypted at rest and are only ever
decrypted into an engine call, so no signed-in answer was available to read.
Everything is therefore pulled with traverse_obj and tolerant of absence, no
key is required to exist, and _real_extract ends with _record() naming the
route that answered, the broadcast status and which manifest fields were
populated. A live broadcast reported there with `manifests: none` means the
key names -- not the plumbing -- are what moved.

That record goes through BOTH to_screen and the stdlib logger, because
to_screen alone would reach nobody. Every path in this app builds yt-dlp with
quiet=True (services/ytdlp.probe, build_opts) or `--quiet` (recorder's engine
argv) and wires no yt-dlp `logger`, and YoutubeDL.to_screen returns early on
quiet unless verbose is also set -- so a to_screen line is visible only from a
bare CLI run with -v. The logger call is what puts it in the app log for the
in-process callers: the probe endpoint and the poller. In the recorder's
capture subprocess neither lands, since _spawn_proc sends stdout and stderr to
DEVNULL; the probe that precedes a capture is where to read this.
"""

import logging
import xml.etree.ElementTree as ET

from yt_dlp.extractor.instagram import InstagramBaseIE
from yt_dlp.utils import (
    ExtractorError,
    UserNotLive,
    base_url,
    int_or_none,
    parse_qs,
    str_or_none,
    traverse_obj,
    url_or_none,
)

# Instagram path segments that are site features, never a handle. The same
# list app/util/platform.py keeps as _IG_RESERVED, duplicated rather than
# imported because this plugin has to keep working under a bare yt-dlp CLI
# with no app package on the path -- tiktok_live.py duplicates its own
# constants for that same reason.
_RESERVED = 'p|reel|reels|tv|stories|explore|accounts|direct|api'

# broadcast_status values that mean there are bytes to fetch. 'active' is a
# running broadcast; 'post_live' is the replay Instagram keeps serving for a
# while after the host stops, which is still worth downloading -- just not as
# a live capture, so it is reported with a different live_status.
_LIVE_STATUS = 'active'
_REPLAY_STATUS = 'post_live'

# Manifest fields, in the order they are tried. Named once because the
# diagnostic line at the end of _real_extract reports on exactly this set.
_MANIFEST_FIELDS = ('dash_playback_url', 'dash_abr_playback_url', 'dash_manifest')

log = logging.getLogger(__name__)

# handle (lowercased) -> numeric account id. The id never changes, and the
# one route that maps a handle to it, users/web_profile_info/, is the one
# Instagram rate-limits: measured 2026-10-27 with _hiyori_0203 live, it
# answered 429 on its first call of the day (users/<pk>/info/ too), while
# live/<id>/info/ and live/web_info/ kept answering. So every answer that
# names an owner is remembered, and the app stores the id with a watch
# (?target_user_id=), leaving the handle lookup as a last resort.
_USER_IDS: dict[str, str] = {}


class InstagramLiveIE(InstagramBaseIE):
    IE_NAME = 'instagram:live'
    # `id` is the HANDLE, not the broadcast: it is the only identifier the URL
    # is guaranteed to carry, so it is what errors should name. The broadcast
    # id rides in the query string when there is one at all.
    _VALID_URL = (
        rf'https?://(?:www\.)?instagram\.com/(?!(?:{_RESERVED})/)'
        r'(?P<id>[^/?#]+)/live/?(?:[?#]|$)')

    _TESTS = [{
        'url': 'https://www.instagram.com/zonezicos/live/?broadcast_id=18101286095626884',
        'only_matching': True,
    }, {
        'url': 'https://www.instagram.com/instagram/live/',
        'only_matching': True,
    }]

    def _api_json(self, path, video_id, note, query=None, expected_status=None):
        """Parsed JSON from the private API, or None when Instagram refused.

        Classified by body, never by status: signed out, the live route
        answers 200 OK with the ordinary HTML app shell (see the module
        docstring), so `if response.ok` calls a refusal a success. Never
        fatal -- every caller has a fallback or a UserNotLive to raise, and a
        refusal here must not look like a crash.
        """
        body = self._download_webpage(
            f'{self._API_BASE_URL}/{path}', video_id, note=note,
            errnote=f'{note}: request failed', fatal=False, query=query or {},
            headers=self._api_headers, expected_status=expected_status,
            impersonate=self._can_impersonate and self._is_web_app)
        if not body:
            return None
        if not body.lstrip().startswith('{'):
            self._record(
                f'{note}: answered {len(body)} bytes of non-JSON, so this '
                'session was not accepted')
            return None
        return self._parse_json(body, video_id, fatal=False) or None

    def _record(self, message):
        """Say something that has to survive quiet mode.

        to_screen is the right channel and reaches nobody here: every caller
        in this app builds yt-dlp quiet and wires no yt-dlp logger, and
        YoutubeDL.to_screen returns early on quiet unless verbose is set too.
        Both are sent, so a bare `yt-dlp -v` and the app's own log each get
        the line once.
        """
        self.to_screen(message)
        log.info('%s', message)

    @staticmethod
    def _as_broadcast(data):
        """The broadcast object inside an API answer, at whichever level it
        sits. Recognized by the keys the extraction needs rather than by the
        shape of the envelope, so a wrapper appearing or disappearing around
        it costs nothing."""
        for candidate in (data, traverse_obj(data, ('broadcast', {dict}))):
            # Values, not just keys: an answer carrying the fields as null is
            # no broadcast, and read as one it ends in "carries no playable
            # manifest", which the poller files as an error every sweep.
            if candidate and any(candidate.get(k) for k in
                                 (*_MANIFEST_FIELDS, 'broadcast_status')):
                return candidate
        return None

    def _broadcast_by_id(self, broadcast_id):
        """The broadcast the URL named, or None for any reason."""
        return self._as_broadcast(self._api_json(
            f'live/{broadcast_id}/info/', broadcast_id,
            'Downloading broadcast info'))

    @staticmethod
    def _remember(broadcast):
        """Learn the owner's account id from any broadcast answer."""
        owner = traverse_obj(broadcast, ('broadcast_owner', {dict})) or {}
        name, pk = owner.get('username'), traverse_obj(owner, ('pk', {str_or_none}))
        if name and pk:
            _USER_IDS[name.lower()] = pk

    def user_id(self, url, *, via_broadcast=True):
        """The numeric account id behind a live URL, or None.

        Public because the app stores it with an instagram watch as
        ?target_user_id=, and a stored id never needs the rate-limited handle
        lookup again. Tried cheapest first: the URL, the cache, the owner of
        the broadcast the URL names, and only then users/web_profile_info/.
        """
        username = self._match_id(url)
        query = parse_qs(url)
        user_id = (traverse_obj(query, ('target_user_id', 0, {str_or_none}))
                   or _USER_IDS.get(username.lower()))
        broadcast_id = traverse_obj(query, ('broadcast_id', 0, {str_or_none}))
        if not user_id and via_broadcast and broadcast_id:
            self._remember(self._broadcast_by_id(broadcast_id))
            user_id = _USER_IDS.get(username.lower())
        if not user_id:
            user_id = traverse_obj(self._api_json(
                'users/web_profile_info/', username, 'Resolving the account id',
                query={'username': username}),
                ('data', 'user', ('id', 'pk'), {str_or_none}, any))
            if user_id:
                _USER_IDS[username.lower()] = user_id
        return user_id

    def _current_broadcast(self, url, username):
        """Whatever the account is broadcasting right now, or None.

        live/web_info/ by account id, the route instagram.com's own
        /<user>/live/ page reads: it returned the running broadcast, manifests
        and all. The story feed this used before was refused outright.

        A refusal raises instead of returning None, at both hops: with the
        lookup refused, "not live" would be a guess, and the poller files
        UserNotLive as a quiet idle creator -- the way _hiyori_0203 went unseen
        while live. Only an answer that IS JSON and holds no broadcast means
        offline; 400 and 404 bodies are read for that, anything else (429,
        an HTML shell) is a refusal.
        """
        user_id = self.user_id(url, via_broadcast=False)
        if not user_id:
            raise ExtractorError(
                f'Instagram would not resolve {username} to an account id '
                '(refused or rate-limited), so whether they are live is '
                'unknown', expected=True)
        answer = self._api_json(
            'live/web_info/', username, 'Looking for a running broadcast',
            query={'target_user_id': user_id}, expected_status=(400, 404))
        if answer is None:
            raise ExtractorError(
                f'Instagram refused the live check for {username} (refused or '
                'rate-limited), so whether they are live is unknown',
                expected=True)
        return self._as_broadcast(answer)

    def _live_formats(self, broadcast, video_id):
        """The DASH ladder, off whichever manifest field this broadcast has.

        A playback URL is preferred over the inline manifest: fetching it lets
        yt-dlp resolve relative BaseURLs against the CDN host that served it,
        which an inline blob parsed in isolation cannot do.
        """
        formats, mpd_url = [], None
        for key, mpd_id in (('dash_playback_url', 'dash'),
                            ('dash_abr_playback_url', 'abr')):
            candidate = url_or_none(broadcast.get(key))
            if not candidate or candidate == mpd_url:
                continue
            mpd_url = candidate
            formats = self._extract_mpd_formats(
                mpd_url, video_id, mpd_id=mpd_id, fatal=False,
                note=f'Downloading {mpd_id} manifest')
            if formats:
                return formats

        manifest = traverse_obj(broadcast, ('dash_manifest', {str}, filter))
        if not manifest:
            return formats
        try:
            # Encoded, not handed over as str: a manifest carrying an XML
            # declaration with an encoding in it is a ValueError to parse from
            # a unicode string, and Instagram's does carry one.
            doc = ET.fromstring(manifest.encode('utf-8'))
        except (ET.ParseError, ValueError) as exc:
            self.report_warning(f'inline DASH manifest is unparseable: {exc}')
            return formats
        return formats + self._parse_mpd_formats(
            doc, mpd_id='dash', mpd_base_url=base_url(mpd_url or ''),
            mpd_url=mpd_url)

    def _real_extract(self, url):
        username = self._match_id(url)
        broadcast_id = traverse_obj(
            parse_qs(url), ('broadcast_id', 0, {str_or_none}))

        # Said before the first request rather than after four of them: no
        # route in this file has ever answered a signed-out caller, so an
        # anonymous attempt can only end in the HTML-shell refusal above, and
        # "you need cookies" is the whole of what the user has to act on.
        if not self._is_logged_in:
            self.raise_login_required(
                'Instagram serves live broadcasts to signed-in accounts only')

        via = 'broadcast_id'
        broadcast = self._broadcast_by_id(broadcast_id) if broadcast_id else None
        if not broadcast:
            via = 'web_info'
            broadcast = self._current_broadcast(url, username)
        if not broadcast:
            raise UserNotLive(video_id=username)
        self._remember(broadcast)

        current_id = traverse_obj(broadcast, ('id', {str_or_none})) or broadcast_id
        if broadcast_id and current_id and current_id != broadcast_id:
            # A pasted URL outlives the broadcast it names by minutes. Say so,
            # rather than capturing a different stream under the old id.
            self._record(
                f'Broadcast {broadcast_id} is gone; {username} is live as '
                f'{current_id} instead')

        status = traverse_obj(broadcast, ('broadcast_status', {str}))
        # Absent status is not treated as offline: the manifest fields are the
        # thing that decides whether there is anything to fetch, and a status
        # spelling we have not seen must not veto a working ladder.
        if status is not None and status not in (_LIVE_STATUS, _REPLAY_STATUS):
            raise UserNotLive(video_id=username)

        formats = self._live_formats(broadcast, current_id or username)
        found = [k for k in _MANIFEST_FIELDS if broadcast.get(k)]
        # The record the module docstring promises: one line per extraction
        # naming the route that answered, what Instagram called the state, and
        # which manifest fields were actually populated. A live broadcast that
        # reaches here with `manifests: none` means the key names moved.
        self._record(
            f'Broadcast {current_id or username} via {via}: {len(formats)} '
            f'formats, status {status!r}, manifests: {", ".join(found) or "none"}')
        if not formats:
            raise ExtractorError(
                f'Broadcast {current_id or username} carries no playable '
                f'manifest (status {status!r})', expected=True)

        owner = traverse_obj(broadcast, ('broadcast_owner', {dict})) or {}
        live = status != _REPLAY_STATUS
        return {
            'id': str(current_id or username),
            'title': f'Live by {owner.get("username") or username}',
            'formats': formats,
            'is_live': live,
            'live_status': 'is_live' if live else 'post_live',
            # yt-dlp's Instagram convention, kept deliberately: `channel` is
            # the handle, `uploader_id` the numeric account id, `uploader` the
            # display name. The app resolves a watch's creator id out of the
            # URL instead (util/platform.creator_id_from_url), precisely
            # because those two id spaces do not substitute for each other --
            # instagram.com/<pk>/ is not anybody's profile.
            'channel': owner.get('username') or username,
            'channel_id': traverse_obj(owner, ('pk', {str_or_none})),
            'uploader': traverse_obj(owner, ('full_name', {str})),
            'uploader_id': traverse_obj(owner, ('pk', {str_or_none})),
            'thumbnail': url_or_none(broadcast.get('cover_frame_url')),
            'timestamp': int_or_none(broadcast.get('published_time')),
            'concurrent_view_count': int_or_none(broadcast.get('viewer_count')),
            'webpage_url': f'https://www.instagram.com/{username}/live/',
        }
