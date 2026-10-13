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

The class MUST keep the upstream name, for the reason bilibili_live gives:
yt-dlp's plugin loader replaces a built-in extractor by name.
"""

from yt_dlp.extractor.bilibili import BiliBiliIE as _BiliBiliIE

# The bounded size that finished 8 of 8 in the measurement above.
HTTP_CHUNK_SIZE = 4 * 1024 * 1024


class BiliBiliIE(_BiliBiliIE):
    def extract_formats(self, play_info):
        formats = super().extract_formats(play_info)
        for f in formats:
            f.setdefault('downloader_options', {}).setdefault(
                'http_chunk_size', HTTP_CHUNK_SIZE)
        return formats
