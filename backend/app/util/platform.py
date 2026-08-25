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
    "xhs": ("xiaohongshu.com", "xhslink.com"),
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
_IG_RESERVED = frozenset(
    {"p", "reel", "reels", "tv", "stories", "explore", "accounts", "direct"}
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
    if host.endswith("instagram.com") and len(seg) == 1 and seg[0] not in _IG_RESERVED:
        return seg[0]
    return None


def normalize_url(url: str) -> str:
    """Canonical form for dup comparison: lowercase scheme/host, no fragment,
    trailing slash collapsed, tracking params stripped, meaningful params kept."""
    parts = urlsplit(_ensure_scheme(url))
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
