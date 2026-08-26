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
    # Was "{platform}/{creator}/{title}", which output_dir cannot render — it
    # supplies platform and creator only, so the moment that default was
    # persisted every download died on KeyError('title'). README documents
    # {platform}/{creator} as the media layout.
    assert body["folder_template"] == "{platform}/{creator}"


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
        {"default_quality": "1080"},   # missing the 'p'
        {"default_quality": "potato"},
    ):
        r = client.put("/api/settings", json=bad)
        assert r.status_code == 422, f"{bad} should be rejected"


def test_default_quality_round_trips(authed_client):
    """The dropdown's value has to survive a save; a silent reset would put
    the UI and the format selector out of step."""
    client, _ = authed_client
    assert client.get("/api/settings").json()["default_quality"] == "best"
    r = client.put("/api/settings", json={"default_quality": "1080p"})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["default_quality"] == "1080p"
    assert client.get("/api/settings").json()["default_quality"] == "1080p"


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


# ---- folder_template must be renderable ---------------------------------------


def test_default_folder_template_actually_renders():
    """The bug this pins: a default that output_dir cannot fill in. Asserting
    the literal is not enough — assert it survives the function that uses it."""
    from app.services.ytdlp import output_dir, DEFAULT_FOLDER_TEMPLATE
    from app.services.settings_store import SettingsModel, folder_template_error

    assert folder_template_error(SettingsModel().folder_template) is None

    class J:
        platform = "bilibili"
        creator = "c1"

    got = output_dir(J(), {"folder_template": SettingsModel().folder_template})
    assert got.parts[-2:] == ("bilibili", "c1")
    assert SettingsModel().folder_template == DEFAULT_FOLDER_TEMPLATE


def test_unknown_placeholder_is_rejected_at_save_time(authed_client):
    """Rejecting here beats raising at download time, where .format() blows up
    after the job is in-flight and the only message is a bare KeyError."""
    client, _ = authed_client
    r = client.put("/api/settings", json={"folder_template": "{platform}/{creator}/{title}"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "{title}" in detail
    assert "{platform}" in detail and "{creator}" in detail  # names what IS available

    # and nothing was persisted
    assert client.get("/api/settings").json()["folder_template"] == "{platform}/{creator}"


def test_malformed_template_is_rejected(authed_client):
    client, _ = authed_client
    r = client.put("/api/settings", json={"folder_template": "{platform}/{"})
    assert r.status_code == 422
    assert "folder_template" in r.json()["detail"]


def test_supported_placeholders_are_accepted(authed_client):
    client, _ = authed_client
    for template in ("{platform}/{creator}", "{creator}", "{platform}", "flat"):
        r = client.put("/api/settings", json={"folder_template": template})
        assert r.status_code == 200, f"{template}: {r.text}"
        assert client.get("/api/settings").json()["folder_template"] == template


def test_output_dir_falls_back_rather_than_raising(caplog):
    """A row persisted before the save-time check still has to go somewhere.
    Raising here fails a job that is already in flight."""
    from app.services.ytdlp import output_dir

    class J:
        platform = "bilibili"
        creator = "c1"

    got = output_dir(J(), {"folder_template": "{platform}/{creator}/{title}"})
    assert got.parts[-2:] == ("bilibili", "c1")


# ---- settings actually reach the engine ---------------------------------------


async def test_saved_folder_template_reaches_output_dir_unquoted(authed_client):
    """AppSetting.value is json.dumps()'d. Reading it raw handed the template
    back wrapped in literal quote characters, which .format() then baked
    straight into the output path."""
    import app.db as db_mod
    from app.services.downloader import DownloadManager
    from app.services.ytdlp import output_dir

    client, _ = authed_client
    assert client.put("/api/settings", json={"folder_template": "{creator}"}).status_code == 200

    async with db_mod.async_session() as s:
        got = await DownloadManager()._setting_str(s, "folder_template", "{platform}/{creator}")
    assert got == "{creator}", f"decoded value still carries JSON syntax: {got!r}"
    assert '"' not in got

    class J:
        platform = "bilibili"
        creator = "c1"

    assert output_dir(J(), {"folder_template": got}).parts[-1] == "c1"


async def test_concurrency_cap_setting_reaches_the_queue(authed_client):
    """get_concurrency() read key "concurrency"; settings_store writes
    "concurrency_cap". Nothing has ever written "concurrency", so the slider
    moved a number the queue never looked at."""
    import app.db as db_mod
    from app.services.downloader import DownloadManager, DEFAULT_CONCURRENCY

    client, _ = authed_client
    target = 7
    assert target != DEFAULT_CONCURRENCY  # otherwise the test proves nothing
    assert client.put("/api/settings", json={"concurrency_cap": target}).status_code == 200

    assert await DownloadManager().get_concurrency() == target
    _ = db_mod


async def test_space_floor_setting_reaches_the_gate(authed_client):
    from app.services.downloader import DownloadManager, DEFAULT_SPACE_FLOOR_PCT

    client, _ = authed_client
    target = 25
    assert target != DEFAULT_SPACE_FLOOR_PCT
    assert client.put("/api/settings", json={"space_floor_pct": target}).status_code == 200

    assert await DownloadManager().get_floor() == float(target)
