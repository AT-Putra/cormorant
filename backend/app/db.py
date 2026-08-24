"""Async SQLAlchemy engine + session factory. DB lives on the data volume."""

from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app import models  # noqa: F401  (register mappers for Base.metadata)
from app.config import DATA_DIR

DATABASE_URL = f"sqlite+aiosqlite:///{DATA_DIR / 'app.db'}"

engine: AsyncEngine = create_async_engine(DATABASE_URL)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
        # create_all doesn't run connect-event pragmas on a fresh file in all
        # paths; ensure WAL explicitly.
        await conn.execute(text("PRAGMA journal_mode=WAL"))


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with async_session() as session:
        yield session
