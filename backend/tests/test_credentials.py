"""US-007 tests: crypto roundtrip + credentials router (mocked probe, no network)."""

import sqlite3
from pathlib import Path

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


class NavOk:
    """Stands in for bilibili's nav endpoint.

    Records the cookiefile CONTENT rather than just its path: the router
    deletes that file the moment validation returns, so a test asserting on
    it afterwards would be reading a deleted path and passing vacuously.
    """

    def __init__(self, *, is_login=True, uname="bili_user_042", vip_status=1, vip_text="年度大会员"):
        self.calls: list[dict] = []
        self.is_login = is_login
        self.uname = uname
        self.vip_status = vip_status
        self.vip_text = vip_text

    def __call__(self, url, cookiefile=None, *, headers=None):
        self.calls.append({
            "url": url,
            "cookiefile": cookiefile,
            "headers": headers,
            "cookie_text": (
                Path(cookiefile).read_text(encoding="utf-8") if cookiefile else None
            ),
        })
        if not self.is_login:
            return {"code": -101, "message": "账号未登录", "data": {"isLogin": False}}
        return {
            "code": 0,
            "message": "0",
            "data": {
                "isLogin": True,
                "uname": self.uname,
                "mid": 407295012,
                "vipStatus": self.vip_status,
                "vipType": 2,
                "vip_label": {"text": self.vip_text},
            },
        }


def _nav_network_down(url, cookiefile=None, *, headers=None):
    raise OSError("Connection reset by peer")


def _netscape(domain: str, name: str = "SESSDATA", value: str = "v") -> str:
    """One valid Netscape cookie line. The router parses what was pasted now,
    so a bare `SESSDATA=x` no longer stands in for a cookies.txt export."""
    TAB = chr(9)
    fields = [domain, "TRUE", "/", "TRUE", "2000000000", name, value]
    return "# Netscape HTTP Cookie File" + chr(10) + TAB.join(fields) + chr(10)


def test_save_pasted_cookie_stores_encrypted(authed_client, crypto_tmp, monkeypatch):
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", ProbeOk())
    monkeypatch.setattr(cred_mod.ytdlp, "fetch_json", NavOk())
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
        "/api/credentials/tiktok",
        json={"cookie_text": _netscape(".tiktok.com", value="bogus")},
    )
    assert r.status_code == 400
    from app.config import DATA_DIR
    import sqlite3 as s3

    con = s3.connect(str(DATA_DIR / "app.db"))
    rows = con.execute("SELECT COUNT(*) FROM platform_credentials").fetchone()[0]
    con.close()
    assert rows == 0


def test_xhs_accepts_a_rednote_export(authed_client, crypto_tmp, monkeypatch):
    """One account, two domains.

    Logging in at xiaohongshu.com from outside China hands the session to
    rednote.com and leaves xiaohongshu.com looking logged out, so a rednote
    export is the only one a user outside China can produce -- and the shape
    check used to answer it with "these cookies are for another site".
    """
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", ProbeOk())

    r = client.post("/api/credentials/xhs", json={"cookie_text": _netscape(".rednote.com")})

    assert r.status_code == 200


def test_xhs_still_rejects_an_unrelated_domain(authed_client, crypto_tmp, monkeypatch):
    """Widening to two domains must not widen to any domain."""
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", ProbeOk())

    r = client.post("/api/credentials/xhs", json={"cookie_text": _netscape(".example.com")})

    assert r.status_code == 400
    assert "xiaohongshu.com or rednote.com" in r.json()["detail"]


def test_mirror_copies_a_session_onto_the_domain_the_extractor_calls():
    """A jar only offers a cookie to the domain its line names, and yt-dlp's
    XiaoHongShu extractor requests xiaohongshu.com -- so an unmirrored rednote
    export is accepted, stored, handed over, and then ignored on every call."""
    out = cred_mod.mirror_cookie_domains("xhs", _netscape(".rednote.com", "web_session", "s1"))
    domains = {d for d, _ in cred_mod._cookie_entries(out)}

    assert domains == {"rednote.com", "xiaohongshu.com"}
    assert out.startswith("# Netscape HTTP Cookie File")
    assert out.count("s1") == 2


def test_mirror_leaves_single_domain_platforms_alone():
    text = _netscape(".bilibili.com")

    assert cred_mod.mirror_cookie_domains("bilibili", text) == text


def test_mirror_does_not_duplicate_an_already_paired_export():
    """A browser logged into both hosts exports both lines already."""
    paired = _netscape(".rednote.com", "web_session", "s1") + _netscape(
        ".xiaohongshu.com", "web_session", "s1"
    ).splitlines()[-1] + chr(10)

    out = cred_mod.mirror_cookie_domains("xhs", paired)

    assert len(cred_mod._cookie_entries(out)) == 2


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
    nav = NavOk()
    monkeypatch.setattr(cred_mod.ytdlp, "probe", probe)
    monkeypatch.setattr(cred_mod.ytdlp, "fetch_json", nav)

    r = client.post(
        "/api/credentials/bilibili", json={"cookie_text": _netscape(".tiktok.com")}
    )
    assert r.status_code == 400
    assert "tiktok.com" in r.json()["detail"]
    assert "bilibili.com" in r.json()["detail"]
    # rejected before any network call, and before the plaintext temp file
    # holding the credentials was ever written
    assert probe.calls == []
    assert nav.calls == []


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
    monkeypatch.setattr(cred_mod.ytdlp, "fetch_json", NavOk())
    text = _netscape("#HttpOnly_.bilibili.com")
    r = client.post("/api/credentials/bilibili", json={"cookie_text": text})
    assert r.status_code == 200, r.text


def test_platforms_without_a_probe_target_skip_the_network(authed_client, crypto_tmp, monkeypatch):
    """douyin/xhs have no durable URL yt-dlp can extract, and instagram's
    only durable one routes to a broken extractor; the structural check is
    the whole check for all three."""
    client, _stub = authed_client
    probe = ProbeOk()
    nav = NavOk()
    monkeypatch.setattr(cred_mod.ytdlp, "probe", probe)
    monkeypatch.setattr(cred_mod.ytdlp, "fetch_json", nav)

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
        assert r.json()["account"] is None  # nothing can identify the session
    assert probe.calls == []
    assert nav.calls == []


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


# ---- bilibili session check --------------------------------------------------


def test_bilibili_revoked_session_is_rejected(authed_client, crypto_tmp, monkeypatch):
    """The bug this check exists for.

    yt-dlp reads the mere PRESENCE of SESSDATA as being logged in, and then
    trusts the watch page's embedded ladder instead of calling the playurl
    API. A revoked SESSDATA therefore yields the logged-out ladder with no
    error anywhere: measured at a 480p ceiling against 1080p for the same
    video with no cookie at all. Storing it as "validated" is the part that
    made it invisible, so validation has to ask bilibili, not a public video.
    """
    client, _stub = authed_client
    probe = ProbeOk()
    nav = NavOk(is_login=False)
    monkeypatch.setattr(cred_mod.ytdlp, "probe", probe)
    monkeypatch.setattr(cred_mod.ytdlp, "fetch_json", nav)

    r = client.post(
        "/api/credentials/bilibili",
        json={"cookie_text": _netscape(".bilibili.com", value="revoked")},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "账号未登录" in detail  # bilibili's own words, not a guess
    assert "export again" in detail

    from app.config import DATA_DIR
    import sqlite3 as s3

    con = s3.connect(str(DATA_DIR / "app.db"))
    rows = con.execute("SELECT COUNT(*) FROM platform_credentials").fetchone()[0]
    con.close()
    assert rows == 0  # a dead credential must not be stored at all


def test_bilibili_records_account_and_vip_tier(authed_client, crypto_tmp, monkeypatch):
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", ProbeOk())
    monkeypatch.setattr(cred_mod.ytdlp, "fetch_json", NavOk())

    r = client.post(
        "/api/credentials/bilibili",
        json={"cookie_text": _netscape(".bilibili.com")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["account"] == "bili_user_042 · 年度大会员"

    listed = client.get("/api/credentials").json()
    assert listed[0]["account_label"] == "bili_user_042 · 年度大会员"


def test_bilibili_without_premium_is_stored_not_rejected(authed_client, crypto_tmp, monkeypatch):
    """A non-premium account is a perfectly good credential — it still unlocks
    1080p and members-only posts. The label is what answers "so why is there
    still no 4K", which a bare "validated" could not."""
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", ProbeOk())
    monkeypatch.setattr(cred_mod.ytdlp, "fetch_json", NavOk(vip_status=0))

    r = client.post(
        "/api/credentials/bilibili",
        json={"cookie_text": _netscape(".bilibili.com")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["account"] == "bili_user_042 · no premium"


def test_bilibili_auth_check_replaces_the_probe(authed_client, crypto_tmp, monkeypatch):
    """bilibili's old target was public content, so a logged-out session
    probed it happily and got stored as valid. The nav check supersedes it;
    running both would only re-add the latency it was meant to remove."""
    client, _stub = authed_client
    probe = ProbeOk()
    nav = NavOk()
    monkeypatch.setattr(cred_mod.ytdlp, "probe", probe)
    monkeypatch.setattr(cred_mod.ytdlp, "fetch_json", nav)

    r = client.post(
        "/api/credentials/bilibili",
        json={"cookie_text": _netscape(".bilibili.com", value="live_session")},
    )
    assert r.status_code == 200, r.text

    assert probe.calls == []
    assert "bilibili" not in cred_mod._PROBE_URLS
    assert len(nav.calls) == 1
    call = nav.calls[0]
    assert call["url"] == cred_mod._BILIBILI_NAV
    # The whole point is asking AS the user: a check that forgot the cookies
    # would answer "not logged in" for a perfectly good export.
    assert call["cookiefile"] is not None
    assert "live_session" in call["cookie_text"]
    assert "bilibili.com" in (call["headers"] or {}).get("Referer", "")


def test_bilibili_network_failure_is_502_not_a_cookie_verdict(
    authed_client, crypto_tmp, monkeypatch
):
    """A dead uplink must not be reported as bad cookies — that sends the user
    off re-exporting a credential that was fine."""
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", ProbeOk())
    monkeypatch.setattr(cred_mod.ytdlp, "fetch_json", _nav_network_down)

    r = client.post(
        "/api/credentials/bilibili",
        json={"cookie_text": _netscape(".bilibili.com")},
    )
    assert r.status_code == 502
    assert "network" in r.json()["detail"].lower()


def test_auth_check_platforms_are_known_platforms():
    """A typo in the _AUTH_CHECKS key would silently disable the check rather
    than fail loudly: .get(platform) just returns None."""
    assert set(cred_mod._AUTH_CHECKS) <= cred_mod.PLATFORMS


# ---- stranded cookie file sweep ----------------------------------------------


def test_sweep_removes_cookiefiles_stranded_by_a_killed_process(tmp_path, monkeypatch):
    """`finally` covers errors and cancellation, not SIGKILL. A live capture
    holds its decrypted cookie file open for hours by design, so a container
    restart mid-recording leaves plaintext credentials in /tmp with nobody
    left who knows the path."""
    monkeypatch.setattr(cred_mod.tempfile, "gettempdir", lambda: str(tmp_path))

    stranded = [
        tmp_path / f"{cred_mod._ENGINE_PREFIX}bilibili_abc.txt",
        tmp_path / f"{cred_mod._ENGINE_PREFIX}tiktok_def.txt",
        tmp_path / f"{cred_mod._VALIDATE_PREFIX}instagram_ghi.txt",
    ]
    for f in stranded:
        f.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    # Must not touch anything that is not ours.
    bystanders = [tmp_path / "important.txt", tmp_path / "vd_auth_notours.log"]
    for f in bystanders:
        f.write_text("keep me", encoding="utf-8")

    assert cred_mod.sweep_stale_cookiefiles() == 3
    assert not any(f.exists() for f in stranded)
    assert all(f.exists() for f in bystanders)

    # Idempotent: a boot with a clean /tmp reports nothing and raises nothing.
    assert cred_mod.sweep_stale_cookiefiles() == 0


def test_sweep_covers_every_prefix_the_module_actually_writes():
    """A new temp-file prefix that does not join _COOKIEFILE_PREFIXES is
    plaintext credentials nothing will ever collect."""
    import re

    src = Path(cred_mod.__file__).read_text(encoding="utf-8")
    used = set(re.findall(r'prefix=f"\{(_[A-Z_]+)\}', src))
    assert used, "prefix= is no longer built from a module constant"
    names = {n for n, v in vars(cred_mod).items() if v in cred_mod._COOKIEFILE_PREFIXES}
    assert used <= names, f"prefix constants missing from the sweep: {used - names}"
