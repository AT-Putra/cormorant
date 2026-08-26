"""US-007 tests: crypto roundtrip + credentials router (mocked probe, no network)."""

import sqlite3

import pytest

from app import crypto
from app.routers import credentials as cred_mod


def test_crypto_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(crypto, "CONFIG_DIR", tmp_path)
    crypto._reset_for_tests()
    try:
        text = "# Netscape HTTP Cookie File\nSESSDATA=abc123\tmarker_value_xyz"
        blob = crypto.encrypt_cookie_text(text)
        assert blob != text
        assert "SESSDATA" not in blob and "marker_value_xyz" not in blob
        assert crypto.decrypt_cookie_blob(blob) == text
        # key file created once and reused
        key1 = (tmp_path / "secret.key").read_bytes()
        crypto.encrypt_cookie_text("again")
        key2 = (tmp_path / "secret.key").read_bytes()
        assert key1 == key2
    finally:
        crypto._reset_for_tests()


@pytest.fixture()
def crypto_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.CONFIG_DIR", tmp_path)
    crypto._reset_for_tests()
    yield tmp_path
    crypto._reset_for_tests()


class ProbeOk:
    """Records how it was called: the validation probe must stay flat and
    capped, so a regression to full extraction is caught here rather than by
    a 60s hang on a real profile URL."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, url, cookiefile=None, **kw):
        self.calls.append({"url": url, **kw})
        return {"id": "x"}


class ProbeAuthError(Exception):
    pass


def _authed_probe_fail(url, cookiefile=None, **kw):
    raise RuntimeError("This video is only available to registered members; please log in")


def _netscape(domain: str, name: str = "SESSDATA", value: str = "v") -> str:
    """One valid Netscape cookie line. The router parses what was pasted now,
    so a bare `SESSDATA=x` no longer stands in for a cookies.txt export."""
    TAB = chr(9)
    fields = [domain, "TRUE", "/", "TRUE", "2000000000", name, value]
    return "# Netscape HTTP Cookie File" + chr(10) + TAB.join(fields) + chr(10)


def test_save_pasted_cookie_stores_encrypted(authed_client, crypto_tmp, monkeypatch):
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", ProbeOk())
    r = client.post(
        "/api/credentials/bilibili",
        json={"cookie_text": _netscape(".bilibili.com", value="supersecret123")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["validated"] is True

    # raw DB scan: plaintext must NOT be in stored blob
    con = sqlite3.connect(str(crypto_tmp.parent / "data" / "app.db")) if False else None
    from app.config import DATA_DIR

    con = sqlite3.connect(str(DATA_DIR / "app.db"))
    (blob,) = con.execute(
        "SELECT encrypted_blob FROM platform_credentials WHERE platform='bilibili'"
    ).fetchone()
    con.close()
    assert "supersecret123" not in blob


def test_bad_cookie_rejected_no_row(authed_client, crypto_tmp, monkeypatch):
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", _authed_probe_fail)
    r = client.post(
        "/api/credentials/bilibili",
        json={"cookie_text": _netscape(".bilibili.com", value="bogus")},
    )
    assert r.status_code == 400
    from app.config import DATA_DIR
    import sqlite3 as s3

    con = s3.connect(str(DATA_DIR / "app.db"))
    rows = con.execute("SELECT COUNT(*) FROM platform_credentials").fetchone()[0]
    con.close()
    assert rows == 0


def test_list_never_returns_blob(authed_client, crypto_tmp, monkeypatch):
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", ProbeOk())
    client.post("/api/credentials/xhs", json={"cookie_text": _netscape(".xiaohongshu.com")})
    r = client.get("/api/credentials")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1 and body[0]["platform"] == "xhs"
    assert "encrypted_blob" not in json_keys(body[0])


def json_keys(d):
    return set(d.keys())


def test_delete_credential(authed_client, crypto_tmp, monkeypatch):
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", ProbeOk())
    client.post("/api/credentials/tiktok", json={"cookie_text": _netscape(".tiktok.com")})
    assert client.delete("/api/credentials/tiktok").status_code == 200
    assert client.get("/api/credentials").json() == []
    assert client.delete("/api/credentials/tiktok").status_code == 404


def test_cookies_file_upload_path(authed_client, crypto_tmp, monkeypatch):
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", ProbeOk())
    content = (
        "# Netscape HTTP Cookie File\n"
        ".douyin.com\tTRUE\t/\tTRUE\t0\tSESSIONID\tdouyin_secret_42\n"
    )
    r = client.post(
        "/api/credentials/douyin",
        files={"cookies_file": ("cookies.txt", content.encode(), "text/plain")},
    )
    assert r.status_code == 200, r.text
    from app.config import DATA_DIR
    import sqlite3 as s3

    con = s3.connect(str(DATA_DIR / "app.db"))
    (blob,) = con.execute(
        "SELECT encrypted_blob FROM platform_credentials WHERE platform='douyin'"
    ).fetchone()
    con.close()
    assert "douyin_secret_42" not in blob


def test_unknown_platform_404(authed_client, crypto_tmp):
    client, _stub = authed_client
    r = client.post("/api/credentials/youtube", json={"cookie_text": "c=3"})
    assert r.status_code == 404


def test_validation_probe_is_flat_and_capped(authed_client, crypto_tmp, monkeypatch):
    """Several probe targets are profiles. Extracting one pulls every video on
    it — TikTok's took 61s and then failed on whichever clip topped the feed,
    rejecting a cookie save for a reason unrelated to the cookies."""
    client, _stub = authed_client
    probe = ProbeOk()
    monkeypatch.setattr(cred_mod.ytdlp, "probe", probe)

    r = client.post(
        "/api/credentials/tiktok", json={"cookie_text": _netscape(".tiktok.com")}
    )
    assert r.status_code == 200, r.text

    assert len(probe.calls) == 1
    call = probe.calls[0]
    assert call["extract_flat"] is True
    # Flat alone still paged 1454 entries in 58s; the cap is load-bearing.
    assert call["playlist_items"] == "1-3"


# ---- structural cookie check -------------------------------------------------


def test_cookies_for_the_wrong_platform_are_rejected(authed_client, crypto_tmp, monkeypatch):
    """Pasting one platform's export into another's slot is the easy mistake,
    and a probe against public content would happily pass it."""
    client, _stub = authed_client
    probe = ProbeOk()
    monkeypatch.setattr(cred_mod.ytdlp, "probe", probe)

    r = client.post(
        "/api/credentials/bilibili", json={"cookie_text": _netscape(".tiktok.com")}
    )
    assert r.status_code == 400
    assert "tiktok.com" in r.json()["detail"]
    assert "bilibili.com" in r.json()["detail"]
    assert probe.calls == []  # rejected before any network call


def test_non_netscape_paste_is_rejected(authed_client, crypto_tmp, monkeypatch):
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", ProbeOk())
    r = client.post("/api/credentials/bilibili", json={"cookie_text": "SESSDATA=abc123"})
    assert r.status_code == 400
    assert "Netscape" in r.json()["detail"]


def test_httponly_prefix_is_a_domain_not_a_comment(authed_client, crypto_tmp, monkeypatch):
    """Several exporters emit #HttpOnly_ on the domain field; skipping those
    lines as comments would reject a perfectly good export."""
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", ProbeOk())
    text = _netscape("#HttpOnly_.bilibili.com")
    r = client.post("/api/credentials/bilibili", json={"cookie_text": text})
    assert r.status_code == 200, r.text


def test_platforms_without_a_probe_target_skip_the_network(authed_client, crypto_tmp, monkeypatch):
    """douyin/xhs have no durable URL yt-dlp can extract, and instagram's
    only durable one routes to a broken extractor; the structural check is
    the whole check for all three."""
    client, _stub = authed_client
    probe = ProbeOk()
    monkeypatch.setattr(cred_mod.ytdlp, "probe", probe)

    for platform, domain, name in (
        ("douyin", ".douyin.com", "SESSDATA"),
        ("xhs", ".xiaohongshu.com", "SESSDATA"),
        ("instagram", ".instagram.com", "sessionid"),
    ):
        r = client.post(
            f"/api/credentials/{platform}",
            json={"cookie_text": _netscape(domain, name)},
        )
        assert r.status_code == 200, r.text
    assert probe.calls == []


def test_probe_targets_route_to_working_extractors():
    """instagram's target was a profile URL, which yt-dlp routes to
    instagram:user — an extractor shipping _WORKING = False because it still
    scrapes the sharedData blob Instagram removed years ago. Every save died
    on "Unable to extract data" and no cookie could have passed. Catch the
    next dead target here rather than in a bug report."""
    from yt_dlp.extractor import gen_extractor_classes

    candidates = [ie for ie in gen_extractor_classes() if ie.IE_NAME != "generic"]
    for platform, url in cred_mod._PROBE_URLS.items():
        match = next((ie for ie in candidates if ie.suitable(url)), None)
        assert match is not None, f"{platform}: {url} matches no yt-dlp extractor"
        assert match.working(), (
            f"{platform}: {url} routes to {match.IE_NAME}, which yt-dlp marks broken"
        )


def test_instagram_export_without_sessionid_is_rejected(authed_client, crypto_tmp, monkeypatch):
    """sessionid is yt-dlp's _AUTH_COOKIE_NAME for instagram and the thing it
    reads as _is_logged_in. Without a probe to lean on, an export missing it
    would otherwise be stored as valid and fail at download time."""
    client, _stub = authed_client
    probe = ProbeOk()
    monkeypatch.setattr(cred_mod.ytdlp, "probe", probe)

    r = client.post(
        "/api/credentials/instagram",
        json={"cookie_text": _netscape(".instagram.com", "csrftoken")},
    )
    assert r.status_code == 400
    assert "sessionid" in r.json()["detail"]
    assert probe.calls == []


def test_required_cookie_must_be_on_the_platform_domain(authed_client, crypto_tmp, monkeypatch):
    """A sessionid from some other site riding along in the same export is
    not an instagram login."""
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", ProbeOk())

    text = _netscape(".instagram.com", "csrftoken") + _netscape(".tiktok.com", "sessionid")
    r = client.post("/api/credentials/instagram", json={"cookie_text": text})
    assert r.status_code == 400
    assert "sessionid" in r.json()["detail"]
