"""US-012 API tests — httpx inside notifier faked; no real sends.

Event-glue tests are async so events.publish schedules the handler on the
running test loop; drain_glue() awaits those tasks to completion.
"""

import asyncio
import json
import sqlite3
from datetime import datetime

import pytest

from app import crypto
from app.services import events
from app.services import notifier as ntf


class FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


class FakeClient:
    calls = []
    status_code = 200
    raise_exc = None

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None, json=None):
        type(self).calls.append({"url": url, "content": content, "json": json})
        if type(self).raise_exc:
            raise type(self).raise_exc("down")
        return FakeResponse(type(self).status_code)


@pytest.fixture()
def crypto_tmp(tmp_path, monkeypatch):
    """Isolated Fernet key so raw-sqlite assertions are deterministic."""
    monkeypatch.setattr("app.config.CONFIG_DIR", tmp_path)
    crypto._reset_for_tests()
    yield
    crypto._reset_for_tests()


@pytest.fixture()
def fake_httpx(monkeypatch):
    FakeClient.calls = []
    FakeClient.status_code = 200
    FakeClient.raise_exc = None
    monkeypatch.setattr(ntf.httpx, "AsyncClient", FakeClient)
    return FakeClient


def freeze_now(monkeypatch, hh, mm=0):
    monkeypatch.setattr(ntf, "_now", lambda: datetime(2026, 8, 23, hh, mm))


def put_config(c, **overrides):
    body = {
        "channel_type": "ntfy",
        "target": "https://ntfy.sh/my-secret-topic-42",
        "token": "supersecrettoken",
        "quiet_hours_start": "23:00",
        "quiet_hours_end": "07:00",
    }
    body.update(overrides)
    r = c.put("/api/notifications/config", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _seed_recording(db_mod, creator="c1", origin="watchlist") -> int:
    from app import models

    async with db_mod.async_session() as s:
        rec = models.LiveRecording(
            room_url="https://www.bilibili.com/live/9",
            platform="bilibili",
            creator=creator,
            origin=origin,
        )
        s.add(rec)
        await s.commit()
        await s.refresh(rec)
        return rec.id


async def _seed_watch(db_mod, creator="c1", **toggles) -> None:
    from app import models

    async with db_mod.async_session() as s:
        s.add(
            models.CreatorWatch(
                platform="bilibili", creator_id="u1", display_name=creator, **toggles
            )
        )
        await s.commit()


async def drain_glue():
    """Await every pending notification-glue task to completion."""
    from app.routers import notifications as notif_mod

    while notif_mod._pending:
        await asyncio.gather(*list(notif_mod._pending))


# 1. config roundtrip ---------------------------------------------------------


def test_config_roundtrip_masks_target_never_token(authed_client, crypto_tmp):
    client, _ = authed_client
    out = put_config(client)

    assert out["configured"] is True
    assert out["channel_type"] == "ntfy"
    assert out["target_masked"].endswith("-42")
    assert "my-secret-topic" not in out["target_masked"]
    assert out["quiet_hours_start"] == "23:00"
    assert "token" not in out and "encrypted_config" not in out


def test_config_blob_encrypted_in_sqlite(authed_client, crypto_tmp):
    client, _ = authed_client
    put_config(client)

    from app.config import DATA_DIR

    con = sqlite3.connect(str(DATA_DIR / "app.db"))
    (blob,) = con.execute("SELECT encrypted_config FROM notification_rules").fetchone()
    con.close()
    assert blob != "supersecrettoken" and "supersecrettoken" not in blob
    assert json.loads(crypto.decrypt_cookie_blob(blob)) == {"token": "supersecrettoken"}


# 2. test-send ------------------------------------------------------------------


def test_send_delivered_true_on_success(
    authed_client, crypto_tmp, fake_httpx, monkeypatch
):
    client, _ = authed_client
    put_config(client)
    freeze_now(monkeypatch, 12, 0)  # outside the stored quiet window
    r = client.post("/api/notifications/test", json={})
    assert r.status_code == 200, r.text
    assert r.json()["delivered"] is True
    assert len(fake_httpx.calls) == 1


def test_send_delivered_false_on_failure_no_raise(
    authed_client, crypto_tmp, fake_httpx, monkeypatch
):
    client, _ = authed_client
    put_config(client)
    freeze_now(monkeypatch, 12, 0)
    fake_httpx.raise_exc = ConnectionError
    r = client.post("/api/notifications/test", json={"message": "ping"})
    assert r.status_code == 200
    assert r.json()["delivered"] is False


def test_send_not_configured_409(authed_client, crypto_tmp):
    client, _ = authed_client
    r = client.post("/api/notifications/test", json={})
    assert r.status_code == 409
    assert "not configured" in r.json()["detail"]


# 3. quiet hours via event glue ---------------------------------------------------


async def test_quiet_hours_suppresses_and_publishes(
    authed_client, crypto_tmp, fake_httpx, monkeypatch
):
    from tests.conftest import current_db

    client, _ = authed_client
    put_config(client)
    freeze_now(monkeypatch, 3, 0)  # inside stored 23:00-07:00 window

    suppressed = []
    events.subscribe(
        lambda e: e["type"].startswith("notification.") and suppressed.append(e)
    )
    try:
        rid = await _seed_recording(current_db())
        events.publish({"type": "recording.started", "recording_id": rid})
        await drain_glue()
    finally:
        events.unsubscribe(suppressed.append)

    assert len(suppressed) == 1
    assert suppressed[0]["type"] == "notification.suppressed"
    assert suppressed[0]["event"] == "recording.started"
    assert fake_httpx.calls == []


async def test_outside_quiet_hours_sends(
    authed_client, crypto_tmp, fake_httpx, monkeypatch
):
    from tests.conftest import current_db

    client, _ = authed_client
    put_config(client)
    freeze_now(monkeypatch, 12, 0)

    suppressed = []
    events.subscribe(
        lambda e: e["type"].startswith("notification.") and suppressed.append(e)
    )
    try:
        rid = await _seed_recording(current_db())
        events.publish({"type": "recording.started", "recording_id": rid})
        await drain_glue()
    finally:
        events.unsubscribe(suppressed.append)

    assert len(fake_httpx.calls) == 1
    assert suppressed == []


async def test_manual_golive_not_notified(authed_client, crypto_tmp, fake_httpx):
    from tests.conftest import current_db

    client, _ = authed_client
    put_config(client)
    rid = await _seed_recording(current_db(), origin="manual")
    events.publish({"type": "recording.started", "recording_id": rid})
    await drain_glue()
    assert fake_httpx.calls == []


# 4. per-creator toggle ------------------------------------------------------------


async def test_creator_toggle_off_blocks_send(authed_client, crypto_tmp, fake_httpx):
    from tests.conftest import current_db

    client, _ = authed_client
    put_config(client)
    await _seed_watch(current_db(), notify_golive=False)
    rid = await _seed_recording(current_db())
    events.publish({"type": "recording.started", "recording_id": rid})
    await drain_glue()
    assert fake_httpx.calls == []


# 4b. cookie health ---------------------------------------------------------------
#
# A dead jar is not tied to any creator -- it degrades every tiktok capture at
# once -- so it carries no per-watch toggle and must reach the user even when
# every creator has notifications turned off.


async def test_stale_credentials_reach_the_channel(
    authed_client, crypto_tmp, fake_httpx
):
    from tests.conftest import current_db

    client, _ = authed_client
    put_config(client)
    await _seed_watch(current_db(), notify_golive=False)
    events.publish(
        {
            "type": "credentials.stale",
            "platform": "tiktok",
            "state": "expired",
            "detail": "the stored TikTok session cookie has expired (3d ago)",
        }
    )
    await drain_glue()
    assert len(fake_httpx.calls) == 1
    body = json.dumps(fake_httpx.calls[0])
    assert "expired" in body
    assert "tiktok" in body


async def test_recovered_credentials_do_not_page_anyone(
    authed_client, crypto_tmp, fake_httpx
):
    """Good news belongs in the activity log, not in someone's phone."""
    client, _ = authed_client
    put_config(client)
    events.publish({"type": "credentials.ok", "platform": "tiktok"})
    await drain_glue()
    assert fake_httpx.calls == []


# 5. delete -----------------------------------------------------------------------


def test_delete_then_send_409(authed_client, crypto_tmp):
    client, _ = authed_client
    put_config(client)
    r = client.delete("/api/notifications/config")
    assert r.status_code == 204
    assert client.get("/api/notifications/config").json() == {
        "configured": False,
        "channel_type": None,
        "target_masked": None,
        "quiet_hours_start": None,
        "quiet_hours_end": None,
    }
    assert client.post("/api/notifications/test", json={}).status_code == 409


def test_bad_channel_rejected(authed_client, crypto_tmp):
    client, _ = authed_client
    r = client.put(
        "/api/notifications/config",
        json={"channel_type": "pigeon", "target": "x"},
    )
    assert r.status_code == 422
