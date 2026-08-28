"""Three repairs to yt-dlp's TikTok live extractor, all measured on live rooms.

1. A dead HLS fallback kills the whole extraction.

   TikTokLiveIE builds its formats from webcast/room/info, then — only when
   that yielded no mp4/HLS entry — calls www.tiktok.com/api/live/detail hunting
   for an `liveUrl` m3u8. That endpoint now answers HTTP 400 for every caller
   and every parameter spelling. _call_api turns a failed download into {}, {}
   has no status == 2, and the method raises UserNotLive — surfacing as "The
   channel is not currently live" even though room/info already returned
   status 2 and a working FLV ladder. Softening it costs nothing: the caller's
   next line is `if url_or_none(live_info.get('liveUrl'))`, which just stays
   false. Only that URL is caught; a room/info failure still means no room.

   Upstream: yt-dlp/yt-dlp#16850 (open), PR #16783 (open).

2. The 1080p60 ladder is invisible, because it is HEVC.

   A room publishes TWO ladders. webcast/room/info carries only the H.264 one,
   which on every room measured stops at 720p/1.8Mbps — so yt-dlp reports 720p
   as the ceiling while tiktok.com's own picker offers 1080p60. The higher
   tiers live in an H.265 ladder that the API does not return at all; it is
   server-rendered into the /live page as
   SIGI_STATE.LiveRoom.liveRoomUserInfo.liveRoom.hevcStreamData, carrying
   origin / uhd_60 (1080x1920 @ 4Mbps) / hd_60 / hd / sd / ld with both FLV and
   HLS URLs. Verified by pulling uhd_60's HLS: hevc 1080x1920 @ 4.06Mbps.

   So the page gets fetched once more and those formats are appended. Every
   failure here is non-fatal: a WAF challenge page, a reshaped SIGI blob or a
   room with no HEVC ladder all leave the H.264 result exactly as it was.

   Both the HLS and the FLV URL of each tier are taken. The FLV ones are
   HEVC-in-FLV — the non-standard codec id 12 extension — which only ffmpeg
   8.0+ can demux; under Debian trixie's 7.1.5 they downloaded as bytes
   ffprobe read as codec unknown at 0x0, so they were dropped as unusable.
   The image now pins ffmpeg 9.0.1 (see backend/Dockerfile), which handles
   them, and they matter: rooms whose HEVC ladder is FLV-only exist, and
   without FLV they fall back to an H.264 720p ceiling. Ties between the two
   containers resolve through yt-dlp's own `ext` sort, which puts mp4 ahead
   of flv — so HLS still wins when a tier offers both.

3. The room id is scraped off a page a WAF no longer serves.

   TikTokLiveIE gets room_id by downloading www.tiktok.com HTML and reading
   SIGI_STATE / universal data out of it. TikTok now answers that host with a
   JS challenge -- 200 OK, ~1.1 KB, SlardarWAF / _wafchallengeid / "Please
   wait..." -- which a browser solves by running the script and yt-dlp cannot.
   No roomId is in the stub, so _real_extract raises UserNotLive and every
   live path reports "The channel is not currently live" for a running room.

   The challenge covers HTML routes only. Measured from the deploy host, same
   minute, same cookies: www.tiktok.com/@user and /@user/live both challenged
   (with and without curl_cffi impersonation, every target), while
   www.tiktok.com/api-live/user/room/ returned ordinary JSON carrying
   data.user.roomId, and webcast/room/info answered 57 KB with status == 2.
   So only the room-id lookup is broken; everything downstream still works.

   Resolved here off that JSON endpoint, then the base extractor is handed the
   m.tiktok.com/share/live/<room_id> form its own _VALID_URL already accepts,
   so it never needs the page. It still makes one soft request for the
   uploader handle, which the challenge fails harmlessly (fatal=False); the
   handle is put back afterwards, since only the error path uses it.

   While the challenge stands the HEVC ladder in repair 2 is unreachable --
   it lives in that same HTML -- so rooms fall back to the H.264 ladder.
   _hevc_formats already degrades to [] on its own, so this needs no guard and
   recovers by itself when the challenge lifts.
"""

import json

from yt_dlp.extractor.tiktok import TikTokLiveIE as _TikTokLiveIE
from yt_dlp.utils import ExtractorError, int_or_none, traverse_obj, url_or_none

# The class MUST keep the upstream name: yt-dlp's plugin loader replaces a
# built-in extractor by name, and a renamed subclass would register as an extra
# extractor that never wins the URL match.


class TikTokLiveIE(_TikTokLiveIE):
    _FALLBACK_EP = 'www.tiktok.com/api/live/detail'
    # The JSON twin of the WAF-challenged /@<uploader>/live page (repair 3).
    _ROOM_ID_EP = 'https://www.tiktok.com/api-live/user/room/'

    # Where each HEVC tier sits on the SAME 0-9 scale the base extractor gets
    # from qualities(('SD1','ld','SD2','sd','HD1','hd','FULL_HD1','uhd',
    # 'ORIGION','origin')) — mixing two scales would let a 720p H.264 stream
    # outrank 1080p60 under plain `best`, which is the whole point of this.
    # HEVC 'hd' deliberately sits just BELOW its H.264 twin: same 720p picture
    # at a lower bitrate, so the more compatible codec should win that tie.
    # Everything the H.264 ladder cannot offer at all sits above it.
    _HEVC_QUALITY = {
        'ld': 0.5,
        'sd': 2.5,
        'hd': 4.5,
        'hd_60': 6,       # 720p60 — no H.264 equivalent is published
        'uhd_60': 7.5,    # 1080p60
        'origin': 9.5,
    }

    def _call_api(self, url, param, room_id, uploader, key=None):
        if self._FALLBACK_EP not in url:
            return super()._call_api(url, param, room_id, uploader, key=key)
        try:
            return super()._call_api(url, param, room_id, uploader, key=key)
        except ExtractorError:
            self.report_warning(
                'live/detail fallback is unavailable; keeping the formats '
                'room/info already returned', video_id=room_id)
            return {}

    def _hevc_formats(self, url, video_id):
        """HEVC ladder scraped off the /live page, or [] for any reason."""
        webpage = self._download_webpage(
            url, video_id, note='Downloading webpage for the HEVC ladder',
            errnote='Unable to read the HEVC ladder', fatal=False)
        if not webpage:
            return []

        stream_data = traverse_obj(self._get_sigi_state(webpage, video_id), (
            'LiveRoom', 'liveRoomUserInfo', 'liveRoom', 'hevcStreamData',
            'pull_data', 'stream_data', {json.loads}, 'data', {dict})) or {}

        formats = []
        for quality, stream in stream_data.items():
            main = traverse_obj(stream, ('main', {dict})) or {}
            params = traverse_obj(main, ('sdk_params', {json.loads}, {dict})) or {}
            # 'ao' is the audio-only rung; the H.264 ladder already carries it.
            if quality == 'ao':
                continue
            for key, ext, protocol in (
                ('hls', 'mp4', 'm3u8_native'),
                ('flv', 'flv', 'https'),
            ):
                stream_url = url_or_none(main.get(key))
                if not stream_url:
                    continue
                formats.append({
                    'url': stream_url,
                    'ext': ext,
                    'protocol': protocol,
                    'format_id': f'hevc-{key}-{quality}',
                    'format_note': 'HEVC',
                    'vcodec': params.get('VCodec') or 'h265',
                    'tbr': int_or_none(params.get('vbitrate'), scale=1000),
                    'resolution': params.get('resolution') or None,
                    'quality': self._HEVC_QUALITY.get(quality, 0),
                })
        return formats

    def _room_id_from_api(self, uploader):
        """room_id off the JSON endpoint, or None for any reason.

        None is not a failure to report: the caller falls back to the base
        extractor's own scrape, which is still correct whenever the page is
        reachable, and raises the usual UserNotLive when it genuinely is not.
        """
        return traverse_obj(self._download_json(
            self._ROOM_ID_EP, uploader, fatal=False,
            note='Resolving the room id without the webpage',
            errnote='Room id endpoint unavailable',
            query={'aid': '1988', 'sourceType': 54, 'uniqueId': uploader},
        ), ('data', 'user', 'roomId', {str}, filter))

    def _real_extract(self, url):
        uploader, room_id = self._match_valid_url(url).group('uploader', 'id')
        if uploader and not room_id:
            room_id = self._room_id_from_api(uploader)
            if room_id:
                url = f'https://m.tiktok.com/share/live/{room_id}'

        info = super()._real_extract(url)
        if uploader:
            # The share/live form carries no handle, and the base extractor
            # only fills one in from the page it can no longer read.
            info.setdefault('uploader', uploader)
        try:
            extra = self._hevc_formats(
                f'https://www.tiktok.com/@{uploader}/live' if uploader else url,
                info.get('id'))
        except Exception as exc:
            # Never let the bonus ladder cost a capture that already works.
            self.report_warning(f'HEVC ladder unavailable: {exc}')
            return info
        if extra:
            info['formats'] = [*(info.get('formats') or []), *extra]
        return info
