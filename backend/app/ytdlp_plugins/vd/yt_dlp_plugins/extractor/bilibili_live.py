"""One repair to yt-dlp's Bilibili live extractor: the HLS ladder is named
after a container yt-dlp refuses to write.

Every live room answers getRoomPlayInfo with two ladders -- http_stream
(format_name "flv") and http_hls (format_name "fmp4") -- and BiliLiveIE
copies format_name straight into each format's `ext`. "fmp4" is not a file
extension, and it is not in yt-dlp's extension allow-list
(utils._UnsafeExtensionError, GHSA-79w7-vh3h-8g4j), so the moment
process_info builds the output name the download dies with

    ERROR: The extracted extension ('fmp4') is unusual and will be skipped
    for safety reasons.

That would be a curiosity if the HLS entries lost the format sort, but they
win it. yt-dlp demotes HEVC-in-FLV to preference -100 as out-of-spec
(utils.FormatSorter, yt-dlp/yt-dlp#5821), which leaves the HEVC HLS entry as
the only top-codec format standing, and plain `best` lands on it -- measured
on live.bilibili.com/5265, 2026-09-19: eight formats at one qn, and
`bestvideo*+bestaudio/best` chose "fmp4 hevc m3u8_native". So:

- a queued download of the room at "Best available" failed with the message
  above, and
- the watch recorder's yt-dlp engine died the same way before its first
  byte, which is exactly when the supervisor hands the room to the streamlink
  retry. streamlink's bilibili plugin picks its tier server-side and reports
  none of it, so the capture landed at a lower quality with no error to read.

The check fires only at the filename, so the fix is the name: HLS with fMP4
segments is what yt-dlp's own _extract_m3u8_formats labels `ext: mp4`, and
that label also switches on FFmpegFixupM3u8PP, which turns the MPEG-TS that a
live HLS capture is written as into a real MP4 once the capture ends. FLV
stays FLV. Anything else Bilibili might one day name is passed through
untouched, so the allow-list keeps its say over it.

Upstream: yt-dlp/yt-dlp#15859 was closed not_planned, and master still
copies format_name into ext (checked 2026-09-19).

The class MUST keep the upstream name: yt-dlp's plugin loader replaces a
built-in extractor by name, and a renamed subclass would register as an extra
extractor that never wins the URL match.
"""

from yt_dlp.extractor.bilibili import BiliLiveIE as _BiliLiveIE

# Bilibili's format_name -> the extension yt-dlp writes for that container.
_EXT_BY_FORMAT_NAME = {
    'fmp4': 'mp4',
}


class BiliLiveIE(_BiliLiveIE):
    def _parse_formats(self, qn, fmt):
        name = fmt.get('format_name')
        ext = _EXT_BY_FORMAT_NAME.get(name, name)
        for f in super()._parse_formats(qn, fmt):
            f['ext'] = ext
            yield f
