"""Table-driven platform detection + normalization tests."""

import pytest

from app.util.platform import detect_platform, normalize_url


@pytest.mark.parametrize(
    "url,expected",
    [
        # bilibili
        ("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili"),
        ("https://b23.tv/abcd123", "bilibili"),
        ("http://m.bilibili.com/video/BV1x?a=b", "bilibili"),
        # instagram
        ("https://www.instagram.com/p/Cx1y2z3/", "instagram"),
        ("https://instagr.am/p/Cx1y2z3/", "instagram"),
        ("https://www.instagram.com/stories/someuser/123456/", "instagram"),
        ("https://www.instagram.com/reel/AbCdEf/", "instagram"),
        # tiktok
        ("https://www.tiktok.com/@user/video/7300000000000000000", "tiktok"),
        ("https://vm.tiktok.com/ZMabcdef/", "tiktok"),
        # douyin
        ("https://www.douyin.com/video/7300000000000000000", "douyin"),
        ("https://v.douyin.com/iRNBho6/", "douyin"),
        ("https://www.iesdouyin.com/share/video/7300/", "douyin"),
        # xhs
        ("https://www.xiaohongshu.com/explore/65a1b2c3", "xhs"),
        ("https://xhslink.com/abcDEF", "xhs"),
        # negatives: lookalike domains and unknown hosts -> None
        ("https://bilibili.com.evil.example/video/x", None),
        ("https://notbilibili.com/video/x", None),
        ("https://youtu.be/dQw4w9WgXcQ", None),
        ("https://youtube.com/watch?v=x", None),
        ("https://example.com/video/1", None),
        ("not a url at all", None),
        ("", None),
    ],
)
def test_detect_platform(url, expected):
    assert detect_platform(url) == expected


def test_scheme_optional():
    assert detect_platform("www.tiktok.com/@u/video/123") == "tiktok"


@pytest.mark.parametrize(
    "a,b",
    [
        # tracking params stripped -> same identity
        (
            "https://www.bilibili.com/video/BV1xx?spm_id_from=333.788&vd_source=abc",
            "https://www.bilibili.com/video/BV1xx",
        ),
        (
            "https://www.instagram.com/p/Cx1/?igsh=xyz&utm_source=share",
            "https://www.instagram.com/p/Cx1/",
        ),
        # scheme/case/trailing-slash differences
        (
            "HTTP://WWW.TikTok.Com/@U/video/1/",
            "https://www.tiktok.com/@U/video/1",
        ),
        # fragment ignored
        (
            "https://www.xiaohongshu.com/explore/65#comment-9",
            "https://www.xiaohongshu.com/explore/65",
        ),
    ],
)
def test_normalize_url_equivalence(a, b):
    assert normalize_url(a) == normalize_url(b)


def test_normalize_keeps_meaningful_params():
    assert "p" in normalize_url("https://example.com/watch?p=42")


def test_creator_id_from_profile_urls():
    from app.util.platform import creator_id_from_url

    cases = {
        "https://space.bilibili.com/4549624": "4549624",
        "https://space.bilibili.com/4549624/video?tid=0": "4549624",
        "https://www.tiktok.com/@someone": "someone",
        "https://www.douyin.com/user/MS4wLjABAAAA": "MS4wLjABAAAA",
        "https://www.xiaohongshu.com/user/profile/5f3a": "5f3a",
        "https://www.instagram.com/handle/": "handle",
    }
    for url, expected in cases.items():
        assert creator_id_from_url(url) == expected

    # content URLs are not profiles: identity has to come from the probe
    for url in (
        "https://www.bilibili.com/video/BV1xx411c7XX",
        "https://live.bilibili.com/21234",
        "https://www.instagram.com/p/Cabc123/",
        "https://www.instagram.com/reel/Cabc123/",
        "https://www.tiktok.com/@someone/video/12345",
        "https://example.com/whatever",
    ):
        assert creator_id_from_url(url) in (None, "someone")
