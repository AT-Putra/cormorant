"""Auth: first-launch password, cookie sessions, /api/* gate (plan step 10).

Single-user model: one AppUser row holds the bcrypt password hash. Sessions are
opaque urlsafe tokens in an HttpOnly cookie; the DB stores only the SHA-256 of
the token, so a leaked sessions table cannot be replayed as logins. The static
SPA mount stays ungated — the frontend routes to the login UI itself.
"""

import hashlib
import secrets
from datetime import timedelta

import bcrypt
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware

from app import db as db_mod  # module ref: attrs resolve after test reloads
from app.models import AppUser, AuthSession, utcnow

SESSION_COOKIE = "vd_session"
SESSION_TTL = timedelta(days=30)
# Reachable signed out; every other /api/* path needs a live session.
PUBLIC_API_PATHS = frozenset(
    {"/api/auth/setup", "/api/auth/login", "/api/auth/status", "/api/health"}
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class PasswordBody(BaseModel):
    password: str


class ChangePasswordBody(BaseModel):
    current_password: str
    # Server-side floor mirrors the UI; setup/login stay 4+ by convention too.
    new_password: str = Field(min_length=4)


def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _single_user(session: AsyncSession) -> AppUser | None:
    return (await session.execute(select(AppUser).limit(1))).scalar_one_or_none()


async def _live_session_token(
    session: AsyncSession, token: str | None
) -> AuthSession | None:
    """The session row for this raw token, if present and unexpired."""
    if not token:
        return None
    return (
        await session.execute(
            select(AuthSession).where(
                AuthSession.token_hash == token_hash(token),
                AuthSession.expires_at > utcnow(),
            )
        )
    ).scalar_one_or_none()


@router.post("/setup")
async def setup_password(body: PasswordBody) -> dict:
    async with db_mod.async_session() as session:
        if await _single_user(session):
            raise HTTPException(409, detail="Password already set up")
        session.add(AppUser(password_hash=get_password_hash(body.password)))
        await session.commit()
    return {"ok": True}


@router.post("/login")
async def login(body: PasswordBody, response: Response) -> dict:
    async with db_mod.async_session() as session:
        user = await _single_user(session)
        if not verify_password(body.password, user.password_hash if user else None):
            raise HTTPException(401, detail="Incorrect password")
        token = secrets.token_urlsafe(32)
        session.add(
            AuthSession(
                token_hash=token_hash(token),
                user_id=user.id,
                expires_at=utcnow() + SESSION_TTL,
            )
        )
        await session.commit()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"ok": True}


@router.post("/change-password")
async def change_password(
    body: ChangePasswordBody, request: Request, response: Response
) -> dict:
    # Behind the auth gate: the live session cookie proves who's asking;
    # current_password proves they know the secret (a stolen tab can't rotate it).
    async with db_mod.async_session() as session:
        user = await _single_user(session)
        # verify_password returns False for a missing row/hash → 401 covers both.
        if user is None or not verify_password(
            body.current_password, user.password_hash
        ):
            raise HTTPException(401, detail="Incorrect current password")
        seen_hash = user.password_hash

        # Sign out every device, then mint a FRESH token + cookie for this one:
        # keeping the old token would let a stolen copy survive the rotation,
        # and re-issuing it would desync the browser's cookie expiry from the
        # DB row's.
        await session.execute(delete(AuthSession))
        token = secrets.token_urlsafe(32)
        session.add(
            AuthSession(
                token_hash=token_hash(token),
                user_id=user.id,
                expires_at=utcnow() + SESSION_TTL,
            )
        )
        # Conditional update: rotate only if the hash we just verified is still
        # current. A concurrent rotation wins the write; we 409 without
        # committing our deletes/inserts.
        result = await session.execute(
            update(AppUser)
            .where(AppUser.id == user.id, AppUser.password_hash == seen_hash)
            .values(password_hash=get_password_hash(body.new_password))
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise HTTPException(409, detail="password_changed_elsewhere")
        await session.commit()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"ok": True}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        async with db_mod.async_session() as session:
            await session.execute(
                delete(AuthSession).where(AuthSession.token_hash == token_hash(token))
            )
            await session.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/status")
async def auth_status(request: Request) -> dict:
    async with db_mod.async_session() as session:
        user = await _single_user(session)
        live = await _live_session_token(
            session, request.cookies.get(SESSION_COOKIE)
        )
    return {"needs_setup": user is None, "authenticated": live is not None}


class AuthMiddleware(BaseHTTPMiddleware):
    """401 for any /api/* path outside PUBLIC_API_PATHS without a live session."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api/") or path in PUBLIC_API_PATHS:
            return await call_next(request)
        async with db_mod.async_session() as session:
            live = await _live_session_token(
                session, request.cookies.get(SESSION_COOKIE)
            )
        if live is None:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return await call_next(request)


# ponytail: BaseHTTPMiddleware is fine for the current JSON routes; if a future
# SSE/streaming route stalls behind it, rewrite dispatch as pure ASGI.
