"""US-008 tests: settings store/router + ytdlp version/update (all subprocess mocked)."""

import pytest

from app.routers import settings as settings_mod
from app.services import settings_store as store


def test_get_defaults(authed_client):
    client, _ = authed_client
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["concurrency_cap"] == 3
    assert body["poll_interval_seconds"] == 300
    assert body["space_floor_pct"] == 10
    assert body["folder_template"] == "{platform}/{creator}/{title}"


def test_put_persists(authed_client):
    client, _ = authed_client
    r = client.put("/api/settings", json={"concurrency_cap": 5, "space_floor_pct": 15})
    assert r.status_code == 200, r.text
    assert r.json()["applied_immediately"] is False  # cap applies on restart in v1
    r2 = client.get("/api/settings")
    assert r2.json()["concurrency_cap"] == 5
    assert r2.json()["space_floor_pct"] == 15


def test_put_invalid_rejected(authed_client):
    client, _ = authed_client
    for bad in (
        {"concurrency_cap": 99},
        {"poll_interval_seconds": 10},
        {"space_floor_pct": 90},
        {"folder_template": ""},
        {"bogus_key": 1},
    ):
        r = client.put("/api/settings", json=bad)
        assert r.status_code == 422, f"{bad} should be rejected"


def test_store_level_persistence(authed_client):
    """Save then read through a NEW session — proves DB roundtrip."""
    client, stub = authed_client
    from tests.conftest import current_db

    import asyncio

    async def _check():
        db_mod = current_db()
        async with db_mod.async_session() as s:
            await store.save_settings(s, {"default_quality": "1080p"})
        async with db_mod.async_session() as s:
            fresh = await store.aget_settings(s)
            return fresh.default_quality

    assert asyncio.new_event_loop().run_until_complete(_check()) == "1080p"


def test_ytdlp_version_mocked(authed_client, monkeypatch):
    client, _ = authed_client

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"2026.08.19\n", b"")

    async def fake_exec(*a, **k):
        return FakeProc()

    monkeypatch.setattr(settings_mod.asyncio, "create_subprocess_exec", fake_exec)
    r = client.get("/api/settings/ytdlp/version")
    assert r.status_code == 200
    assert r.json()["version"] == "2026.08.19"


def test_idle_guard_busy_409(authed_client, monkeypatch):
    client, db_mod = authed_client

    async def busy(session):
        return True

    monkeypatch.setattr(settings_mod, "is_engine_busy", busy)
    r = client.post("/api/settings/ytdlp/update")
    assert r.status_code == 409
    assert r.json()["detail"] == "deferred_until_idle"


def test_idle_guard_unit(authed_client):
    client, stub = authed_client
    from tests.conftest import current_db

    import asyncio

    async def _run():
        async with current_db().async_session() as s:
            return await settings_mod.is_engine_busy(s)

    assert asyncio.new_event_loop().run_until_complete(_run()) is False


def _constant(v):
    async def _f():
        return v

    return _f


def test_update_idle_pip_success(authed_client, monkeypatch):
    client, _ = authed_client

    class FakeResult:
        returncode = 0
        stderr = ""

    def fake_run(*a, **k):
        return FakeResult()

    monkeypatch.setattr(settings_mod.subprocess, "run", fake_run)
    # Version probe reports a bump across the pip run.
    versions = iter(["1.0.0", "2.0.0"])
    async def _bump():
        return next(versions)

    monkeypatch.setattr(settings_mod, "_run_fresh_version", _bump)
    # prevent the real SIGTERM task from firing during the test
    monkeypatch.setattr(settings_mod.os, "kill", lambda *a, **k: None)
    r = client.post("/api/settings/ytdlp/update")
    assert r.status_code == 200, r.text
    assert r.json() == {"updated": True, "restarting": True, "version": "2.0.0"}


def test_update_no_change_skips_restart(authed_client, monkeypatch):
    client, _ = authed_client

    class FakeResult:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(settings_mod.subprocess, "run", lambda *a, **k: FakeResult())
    # pip -U found nothing newer: same version before and after.
    monkeypatch.setattr(
        settings_mod, "_run_fresh_version", _constant("9.9.9")
    )
    r = client.post("/api/settings/ytdlp/update")
    assert r.status_code == 200, r.text
    assert r.json() == {"updated": False, "restarting": False, "version": "9.9.9"}


def test_update_pip_failure(authed_client, monkeypatch):
    client, _ = authed_client

    class FakeResult:
        returncode = 1
        stderr = "boom"

    monkeypatch.setattr(settings_mod.subprocess, "run", lambda *a, **k: FakeResult())
    r = client.post("/api/settings/ytdlp/update")
    assert r.status_code == 500
