"""Two repairs to yt-dlp's TikTok live extractor, both measured on live rooms.

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
"""

import json

from yt_dlp.extractor.tiktok import TikTokLiveIE as _TikTokLiveIE
from yt_dlp.utils import ExtractorError, int_or_none, traverse_obj, url_or_none

# The class MUST keep the upstream name: yt-dlp's plugin loader replaces a
# built-in extractor by name, and a renamed subclass would register as an extra
# extractor that never wins the URL match.


class TikTokLiveIE(_TikTokLiveIE):
    _FALLBACK_EP = 'www.tiktok.com/api/live/detail'

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

    def _real_extract(self, url):
        info = super()._real_extract(url)
        try:
            extra = self._hevc_formats(url, info.get('id'))
        except Exception as exc:
            # Never let the bonus ladder cost a capture that already works.
            self.report_warning(f'HEVC ladder unavailable: {exc}')
            return info
        if extra:
            info['formats'] = [*(info.get('formats') or []), *extra]
        return info
