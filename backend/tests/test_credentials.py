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
    def __call__(self, url, cookiefile=None):
        return {"id": "x"}


class ProbeAuthError(Exception):
    pass


def _authed_probe_fail(url, cookiefile=None):
    raise RuntimeError("This video is only available to registered members; please log in")


def test_save_pasted_cookie_stores_encrypted(authed_client, crypto_tmp, monkeypatch):
    client, _stub = authed_client
    monkeypatch.setattr(cred_mod.ytdlp, "probe", ProbeOk())
    r = client.post(
        "/api/credentials/bilibili",
        json={"cookie_text": "SESSDATA=supersecret123"},
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
        json={"cookie_text": "SESSDATA=bogus"},
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
    client.post("/api/credentials/xhs", json={"cookie_text": "a=1"})
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
    client.post("/api/credentials/tiktok", json={"cookie_text": "b=2"})
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
