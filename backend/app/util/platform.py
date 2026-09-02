"""URL -> platform detection + normalization for duplicate checking."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PLATFORMS = ("bilibili", "instagram", "tiktok", "douyin", "xhs")

# Registrable domains (subdomains matched via suffix).
_DOMAINS: dict[str, tuple[str, ...]] = {
    "bilibili": ("bilibili.com", "b23.tv"),
    "instagram": ("instagram.com", "instagr.am"),
    "tiktok": ("tiktok.com",),
    "douyin": ("douyin.com", "iesdouyin.com"),
    # rednote.com is Xiaohongshu's international rebrand, not a mirror: log in
    # on xiaohongshu.com from outside China and it hands the session to
    # rednote.com, leaving xiaohongshu.com looking logged out. Its TLS cert is
    # issued to 行吟信息科技（上海）有限公司, Xiaohongshu's own company.
    "xhs": ("xiaohongshu.com", "xhslink.com", "rednote.com"),
}

# Query params that carry tracking/session noise, never content identity.
_TRACKING_RE = re.compile(
    r"^(utm_.*)$|^(spm.*)$|^(vd_source|share_.*|from|tt_.*|igsh(id)?|fbclid|gclid"
    r"|si|ref(er)?(_src)?$|xsec_token$|xsec_source$|is_from_webapp|sender_device"
    r"|sender_web|web_id|feature|_r|_t|app_platform|msource|vd_sid)$",
    re.IGNORECASE,
)


def _ensure_scheme(url: str) -> str:
    url = url.strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


def _host(url: str) -> str:
    netloc = urlsplit(_ensure_scheme(url)).netloc.lower()
    netloc = netloc.rsplit("@", 1)[-1]  # strip userinfo
    return netloc.split(":", 1)[0]  # strip port


def detect_platform(url: str) -> str | None:
    """Return one of PLATFORMS for a recognized URL, else None."""
    host = _host(url)
    if not host:
        return None
    for platform, domains in _DOMAINS.items():
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return platform
    return None


# Path prefixes that are Instagram *content*, never a profile handle.
# "live" is here for the bare instagram.com/live/ form; the handle-carrying
# /<user>/live/ shape is read below, where the handle is the first segment.
_IG_RESERVED = frozenset(
    {"p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct", "live"}
)


def creator_id_from_url(url: str) -> str | None:
    """Creator id parsed straight out of a profile-shaped URL, else None.

    Kept in lockstep with poller._PROFILE_TEMPLATES: whatever comes out here
    must rebuild the same profile page. Post/video URLs return None so the
    probe result stays the source of identity for them.
    """
    host = _host(url)
    path = urlsplit(_ensure_scheme(url)).path.strip("/")
    if not host or not path:
        return None
    seg = path.split("/")
    if host == "space.bilibili.com" and re.fullmatch(r"\d+", seg[0]):
        return seg[0]
    if host.endswith("tiktok.com") and seg[0].startswith("@") and len(seg[0]) > 1:
        return seg[0][1:]
    if host.endswith("douyin.com") and seg[0] == "user" and len(seg) > 1:
        return seg[1]
    if (
        host.endswith("xiaohongshu.com")
        and seg[:2] == ["user", "profile"]
        and len(seg) > 2
    ):
        return seg[2]
    # /<handle>/ and /<handle>/live/ both state the handle, and both rebuild
    # the same profile page — which is the contract this function has with
    # poller._PROFILE_TEMPLATES. Reading the live form matters because it is
    # the URL a person copies out of a running broadcast, and without it a
    # watch added from one falls through to the probe's uploader_id: Instagram's
    # numeric account id, which formats into instagram.com/<pk>/ and is nobody's
    # profile.
    if (
        host.endswith("instagram.com")
        and seg[0] not in _IG_RESERVED
        and (len(seg) == 1 or (len(seg) == 2 and seg[1] == "live"))
    ):
        return seg[0]
    return None


# Alias host -> the host every extractor and cookie jar is written against.
# yt-dlp's XiaoHongShu extractor matches xiaohongshu.com only, so a rednote.com
# link is the same note that nothing can open.
_HOST_ALIASES = {"rednote.com": "www.xiaohongshu.com", "www.rednote.com": "www.xiaohongshu.com"}


def canonical_url(url: str) -> str:
    """Rewrite an alias host to the canonical one, leaving the rest alone."""
    parts = urlsplit(_ensure_scheme(url))
    host = parts.netloc.lower()
    target = _HOST_ALIASES.get(host)
    return urlunsplit(parts._replace(netloc=target)) if target else url


def normalize_url(url: str) -> str:
    """Canonical form for dup comparison: lowercase scheme/host, no fragment,
    trailing slash collapsed, tracking params stripped, meaningful params kept."""
    parts = urlsplit(_ensure_scheme(canonical_url(url)))
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _TRACKING_RE.match(k.strip())
        ]
    )
    path = parts.path.rstrip("/")
    # Scheme canonicalized: http/https variants are the same content for
    # duplicate detection.
    return urlunsplit(("https", parts.netloc.lower(), path, query, ""))
