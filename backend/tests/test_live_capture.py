"""Live-capture edge cases: stream-end finalization and reconnect budget.

A live host ending the broadcast raises inside yt-dlp even though the capture
on disk is complete; a mid-stream transport drop also raises but should be
retried. These tests pin both classifications and the .part finalization.
"""

from app.services.downloader import (
    LIVE_MAX_RETRIES,
    _is_reconnectable,
    _is_stream_over,
    manager,
)


class _Job:
    """Minimal stand-in for a DownloadJob row (output_dir reads these)."""

    def __init__(self, platform="bilibili", creator="someone"):
        self.platform = platform
        self.creator = creator
        self.kind = "video"


def test_stream_over_markers_classify_as_finished():
    assert _is_stream_over(Exception("ERROR: [BiliLive] 1542225: Streamer is not live"))
    assert _is_stream_over(Exception("The live event has ended"))
    # A transport drop is not a clean end.
    assert not _is_stream_over(Exception("Connection reset by peer"))


def test_reconnectable_markers_classify_as_retryable():
    assert _is_reconnectable(Exception("796 bytes read, 70469155 more expected"))
    assert _is_reconnectable(Exception("Connection reset by peer"))
    assert _is_reconnectable(Exception("HTTP Error 503: Service Unavailable"))
    # A dead video is not worth reconnecting for.
    assert not _is_reconnectable(Exception("This video may be deleted"))


def test_captured_part_picks_largest_nonempty(tmp_path, monkeypatch):
    import app.services.ytdlp as ytdlp_mod

    monkeypatch.setattr(ytdlp_mod, "output_dir", lambda job, s=None: tmp_path)
    (tmp_path / "empty.mp4.part").write_bytes(b"")
    (tmp_path / "small.mp4.part").write_bytes(b"x" * 10)
    (tmp_path / "big.flv.part").write_bytes(b"x" * 5000)

    got = manager._captured_part(_Job())
    assert got is not None and got.name == "big.flv.part"


def test_captured_part_none_when_nothing_written(tmp_path, monkeypatch):
    import app.services.ytdlp as ytdlp_mod

    monkeypatch.setattr(ytdlp_mod, "output_dir", lambda job, s=None: tmp_path)
    assert manager._captured_part(_Job()) is None


def test_finalize_part_drops_only_the_part_suffix(tmp_path):
    part = tmp_path / "show 2026-08-24 13_56.flv.part"
    part.write_bytes(b"data")

    final = manager._finalize_part(part)

    assert final.name == "show 2026-08-24 13_56.flv"
    assert final.read_bytes() == b"data"
    assert not part.exists()


def test_reconnect_budget_is_bounded():
    """A stuck stream must not re-queue forever."""
    assert 0 < LIVE_MAX_RETRIES <= 100
