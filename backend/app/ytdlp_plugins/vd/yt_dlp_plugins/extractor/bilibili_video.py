"""One change to yt-dlp's Bilibili video extractor: fetch every stream in
bounded ranges, the way yt-dlp's own YouTube extractor already does.

Bilibili's upos CDN cuts open-ended downloads part-way. Measured 2026-10-13
on BV1JgkZY6E61 against upos-hz-mirrorakam.akamaized.net: 4 of 6
`Range: bytes=N-` requests were cut after 0.9-1.9 MB with "N bytes read, M
more expected", while 8 of 8 bounded `bytes=a-b` requests finished, and a
40 MB yt-dlp run went from 489 KB/s to 1821 KB/s with 4 MiB chunks. Unchunked,
each cut spends one of yt-dlp's retries; on prod, job 14 (1.6 GB) used up all
5, then the queue's 20 reconnects, twice, and failed with

    Got error: 489 bytes read, 1601185267 more expected. Giving up after 5
    retries

Per format, through `downloader_options`, rather than the global
`http_chunk_size` param: a live room queued as a download (BiliLiveIE) streams
FLV through the same HTTP downloader, and a bounded Range has no business on
an endless stream. The global param would also outrank this per-format value.

Second change: the `mirror` extractor arg. Chunking does not help when the
CDN edge itself holds a broken stretch of the file. Measured 2026-10-27 on
BV1FZS7B3EeL f30112 (job 19): on upos-hz-mirrorakam.akamaized.net, reads
starting at 63269813 and at 63273909 both died at byte 63274104, a few KB on
got HTTP 503, and 64.3 MB onwards was fine. A resume always restarts at the
.part's length, so every retry for four days hit the same byte:

    Got error: 195 bytes read, 4179998 more expected. Giving up after 5
    retries

The same offset was fine on upos-sz-mirrorcosov.bilivideo.com, which is the
`backupUrl` bilibili's playurl answer lists next to every `baseUrl`, and
which yt-dlp drops. `mirror=N` picks entry N of [baseUrl, *backupUrl] per
stream (wrapping), so the default stays on the base URL, the fast one when
it works. downloader.run_job sets it for one retry when a download dies.

The class MUST keep the upstream name, for the reason bilibili_live gives:
yt-dlp's plugin loader replaces a built-in extractor by name.
"""

from yt_dlp.extractor.bilibili import BiliBiliIE as _BiliBiliIE
from yt_dlp.utils import int_or_none, traverse_obj

# The bounded size that finished 8 of 8 in the measurement above.
HTTP_CHUNK_SIZE = 4 * 1024 * 1024


def _backup_urls(play_info):
    """baseUrl -> bilibili's backupUrl list, for every DASH stream."""
    streams = traverse_obj(play_info, (
        'dash', (('video', ...), ((None, 'dolby'), 'audio', ...), ('flac', 'audio')),
        {dict}))
    return {traverse_obj(s, 'baseUrl', 'base_url', 'url'):
            traverse_obj(s, 'backupUrl', 'backup_url', default=[]) for s in streams}


class BiliBiliIE(_BiliBiliIE):
    def extract_formats(self, play_info):
        formats = super().extract_formats(play_info)
        mirror = int_or_none(traverse_obj(self._configuration_arg('mirror'), 0)) or 0
        backups = _backup_urls(play_info) if mirror else {}
        for f in formats:
            f.setdefault('downloader_options', {}).setdefault(
                'http_chunk_size', HTTP_CHUNK_SIZE)
            # Without a mirror asked for, the url is left untouched.
            if backup := backups.get(f.get('url')):
                f['url'] = [f['url'], *backup][mirror % (1 + len(backup))]
        return formats
