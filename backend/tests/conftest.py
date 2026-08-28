"""Shared fixtures.

Auth convention (US-006): one mechanism for all API tests — per-test tmp
sqlite via DATA_DIR env override + module reload (mirrors test_models.py),
plus a seeded AppUser/AuthSession whose cookie is preset on the TestClient so
the auth middleware stays live everywhere. Auth-flow tests use the unseeded
anon_client and drive setup/login themselves.
"""

import hashlib
import importlib
import secrets
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

TEST_PASSWORD = "test-password"
_PASSWORD_HASH: str | None = None


def _password_hash() -> str:
    """bcrypt hashing is ~100ms; compute once per process."""
    global _PASSWORD_HASH
    if _PASSWORD_HASH is None:
        import app.auth as auth_mod

        _PASSWORD_HASH = auth_mod.get_password_hash(TEST_PASSWORD)
    return _PASSWORD_HASH


_CURRENT_DB = None


class StubManager:
    """Sync methods matching DownloadManager's public API; records calls."""

    def __init__(self):
        self.enqueued = []
        self.paused = []
        self.cancelled = []
        self.forgotten = []

    def enqueue(self, job_id):
        self.enqueued.append(job_id)

    def pause(self, job_id):
        self.paused.append(job_id)

    def cancel(self, job_id):
        self.cancelled.append(job_id)

    def forget(self, job_id):
        self.forgotten.append(job_id)

    async def start(self):
        pass

    async def stop(self):
        pass


async def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    import app.config as config

    importlib.reload(config)
    import app.db as db_mod

    importlib.reload(db_mod)
    await db_mod.init_db()
    # Expose the per-test reloaded module for tests that need direct DB access
    global _CURRENT_DB
    _CURRENT_DB = db_mod
    return db_mod


def current_db():
    """The most recently created fresh test DB module (reloaded per test)."""
    return _CURRENT_DB


def _build_app_with_stub(db_mod, monkeypatch):
    async def _override():
        async with db_mod.async_session() as session:
            yield session

    from app.main import create_app

    test_app = create_app()
    test_app.dependency_overrides[db_mod.get_session] = _override

    stub = StubManager()
    # Keep lifespan from spawning real DownloadManager workers bound to the
    # TestClient's loop.
    import app.main as main_mod
    import app.routers.downloads as dl

    monkeypatch.setattr(dl, "get_manager", lambda: stub)
    monkeypatch.setattr(main_mod, "manager", stub)
    return test_app, stub


async def _seed_session(db_mod) -> str:
    token = secrets.token_urlsafe(32)
    async with db_mod.async_session() as s:
        user = db_mod.models.AppUser(password_hash=_password_hash())
        s.add(user)
        await s.flush()
        s.add(
            db_mod.models.AuthSession(
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                user_id=user.id,
                expires_at=db_mod.models.utcnow() + timedelta(days=30),
            )
        )
        await s.commit()
    return token


@pytest.fixture
async def authed_client(tmp_path, monkeypatch):
    """TestClient with a valid session cookie preset; middleware live."""
    db_mod = await _fresh_db(tmp_path, monkeypatch)
    import app.auth as auth_mod

    token = await _seed_session(db_mod)
    test_app, stub = _build_app_with_stub(db_mod, monkeypatch)
    with TestClient(test_app) as c:
        c.cookies.set(auth_mod.SESSION_COOKIE, token)
        yield c, stub
    await db_mod.engine.dispose()


@pytest.fixture
async def anon_client(tmp_path, monkeypatch):
    """Fresh empty DB, no cookie — for setup/login/logout/expiry flows."""
    db_mod = await _fresh_db(tmp_path, monkeypatch)
    test_app, _stub = _build_app_with_stub(db_mod, monkeypatch)
    with TestClient(test_app) as c:
        yield c, db_mod
    await db_mod.engine.dispose()
