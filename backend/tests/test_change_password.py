"""Change-password endpoint: verify-current, rotate hash, sessions behavior."""

import asyncio

from sqlalchemy import select

from app.auth import verify_password
from tests.conftest import TEST_PASSWORD, current_db


def _rotate(client, current, new):
    return client.post(
        "/api/auth/change-password",
        json={"current_password": current, "new_password": new},
    )


def _db():
    return current_db()


def _hashes(db_mod):
    async def _check():
        async with db_mod.async_session() as s:
            user = (await s.execute(select(db_mod.models.AppUser))).scalar_one()
            return (
                verify_password(TEST_PASSWORD, user.password_hash),
                verify_password("new-pass-9", user.password_hash),
            )

    return asyncio.run(_check())


def test_wrong_current_rejected(authed_client):
    client, _ = authed_client
    r = _rotate(client, "wrong", "new-pass-9")
    assert r.status_code == 401
    assert r.json()["detail"] == "Incorrect current password"
    # Hash untouched.
    assert _hashes(_db()) == (True, False)


def test_rotate_keeps_this_session_alive(authed_client):
    client, _ = authed_client
    assert _rotate(client, TEST_PASSWORD, "new-pass-9").status_code == 200
    # This session's cookie still works; old password no longer matches.
    assert client.get("/api/auth/status").json()["authenticated"] is True
    assert _hashes(_db()) == (False, True)


def test_other_sessions_die(anon_client):
    """A second session's cookie stops authenticating after a rotation."""
    import hashlib
    import secrets
    from datetime import timedelta

    client, _ = anon_client
    db_mod = _db()
    assert client.post("/api/auth/setup", json={"password": TEST_PASSWORD}).status_code == 200
    assert client.post("/api/auth/login", json={"password": TEST_PASSWORD}).status_code == 200

    # Forge a second live session (another device).
    other_token = secrets.token_urlsafe(32)

    async def _seed():
        async with db_mod.async_session() as s:
            user = (await s.execute(select(db_mod.models.AppUser))).scalar_one()
            s.add(
                db_mod.models.AuthSession(
                    token_hash=hashlib.sha256(other_token.encode()).hexdigest(),
                    user_id=user.id,
                    expires_at=db_mod.models.utcnow() + timedelta(days=30),
                )
            )
            await s.commit()

    asyncio.run(_seed())

    # Rotate via the first session.
    assert _rotate(client, TEST_PASSWORD, "new-pass-9").status_code == 200

    # The forged token is gone from the table.
    async def _gone():
        async with db_mod.async_session() as s:
            rows = (
                (await s.execute(select(db_mod.models.AuthSession))).scalars().all()
            )
            return len(rows)

    n_sessions = asyncio.run(_gone())
    assert n_sessions == 1  # only the rotating device's new session remains
