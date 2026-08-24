"""US-006 auth gate + session flows, against the anon_client (fresh DB).

Covers: ungated 401s on fresh DB, setup-once, login cookie issuance, logout
invalidation, and expiry (row inserted with past expires_at directly).
"""

from datetime import timedelta

import pytest

from tests.conftest import TEST_PASSWORD


def _login(c):
    return c.post("/api/auth/login", json={"password": TEST_PASSWORD})


def test_fresh_db_gates_api_and_needs_setup(anon_client):
    c, _db = anon_client
    r = c.get("/api/downloads")
    assert r.status_code == 401
    assert r.json()["detail"] == "Not authenticated"

    r = c.get("/api/auth/status")
    assert r.status_code == 200
    assert r.json() == {"needs_setup": True, "authenticated": False}

    # Public paths stay reachable; SPA mount stays ungated.
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/auth/status").status_code == 200
    assert c.get("/").status_code != 401


def test_setup_once_then_409(anon_client):
    c, db_mod = anon_client
    assert c.get("/api/auth/status").json()["needs_setup"] is True

    r = c.post("/api/auth/setup", json={"password": TEST_PASSWORD})
    assert r.status_code == 200

    async def _user_count():
        from sqlalchemy import select

        import app.models as models

        async with db_mod.async_session() as s:
            rows = (await s.execute(select(models.AppUser))).scalars().all()
        return rows

    rows = __import__("asyncio").run(_user_count())
    assert len(rows) == 1
    assert rows[0].password_hash.startswith("$2")

    assert c.get("/api/auth/status").json()["needs_setup"] is False

    # Second setup rejected.
    r = c.post("/api/auth/setup", json={"password": "another"})
    assert r.status_code == 409


def test_login_flow_and_cookie_grants_access(anon_client):
    c, _db = anon_client
    c.post("/api/auth/setup", json={"password": TEST_PASSWORD})

    r = c.post("/api/auth/login", json={"password": "wrong-password"})
    assert r.status_code == 401
    assert "vd_session" not in c.cookies

    r = _login(c)
    assert r.status_code == 200
    assert "vd_session" in c.cookies

    r = c.get("/api/auth/status")
    assert r.json() == {"needs_setup": False, "authenticated": True}

    # Cookie grants access to protected API.
    assert c.get("/api/downloads").status_code == 200


def test_logout_invalidates_session(anon_client):
    c, db_mod = anon_client
    c.post("/api/auth/setup", json={"password": TEST_PASSWORD})
    assert _login(c).status_code == 200
    assert c.get("/api/downloads").status_code == 200

    assert c.post("/api/auth/logout").status_code == 200

    # Same cookie replayed -> rejected; status reports logged out.
    r = c.get("/api/downloads")
    assert r.status_code == 401
    assert c.get("/api/auth/status").json()["authenticated"] is False

    async def _session_rows():
        from sqlalchemy import select

        import app.models as models

        async with db_mod.async_session() as s:
            return (await s.execute(select(models.AuthSession))).scalars().all()

    import asyncio

    assert asyncio.run(_session_rows()) == []


def test_expired_session_rejected(anon_client):
    c, db_mod = anon_client
    c.post("/api/auth/setup", json={"password": TEST_PASSWORD})

    import asyncio
    import hashlib

    import app.auth as auth_mod
    import app.models as models

    async def _insert_expired_session():
        from sqlalchemy import select

        async with db_mod.async_session() as s:
            user = (await s.execute(select(models.AppUser))).scalars().one()
            s.add(
                models.AuthSession(
                    token_hash=hashlib.sha256(b"expired-token").hexdigest(),
                    user_id=user.id,
                    expires_at=models.utcnow() - timedelta(days=1),
                )
            )
            await s.commit()

    asyncio.run(_insert_expired_session())

    c.cookies.set(auth_mod.SESSION_COOKIE, "expired-token")
    r = c.get("/api/downloads")
    assert r.status_code == 401
    assert c.get("/api/auth/status").json()["authenticated"] is False


def test_middleware_covers_action_routes(anon_client):
    c, _db = anon_client
    # Detail + action routes are guarded too, not just the collection.
    assert c.get("/api/downloads/999").status_code == 401
    assert c.post("/api/downloads/999/pause").status_code == 401
