"""Roundtrip test: every ontology entity inserts + reads back from a temp DB."""

import importlib
from datetime import timedelta

import pytest


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """Point DATA_DIR at a temp dir, reload config+db, init schema."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    import app.config as config

    importlib.reload(config)
    import app.db as db_mod

    importlib.reload(db_mod)
    await db_mod.init_db()
    yield db_mod
    await db_mod.engine.dispose()


def _rows(db):
    m = db.models
    return [
        m.DownloadJob(
            url="https://example.com/v/1",
            platform="bilibili",
            kind="video",
            title="T",
            creator="c1",
            status="queued",
            progress=0.0,
        ),
        m.LiveRecording(
            room_url="https://example.com/live/1",
            platform="douyin",
            creator="c2",
            origin="watchlist",
        ),
        m.CreatorWatch(platform="tiktok", creator_id="999", display_name="C Three"),
        m.PlatformCredential(platform="instagram", encrypted_blob="enc-blob"),
        m.NotificationRule(
            channel_type="ntfy", target="topic/x", encrypted_config="cfg-blob"
        ),
        m.LibraryItem(
            file_path="/media/bilibili/c1/T.mp4",
            platform="bilibili",
            creator="c1",
            title="T",
            media_type="video",
            size_bytes=1234,
        ),
        m.AppSetting(key="concurrency", value="2"),
        m.AppUser(password_hash="hash"),
    ]


async def test_all_entities_roundtrip(db):
    from sqlalchemy import select

    m = db.models

    async with db.async_session() as session:
        user = m.AppUser(password_hash="hash")
        session.add(user)
        await session.flush()
        session.add(
            m.AuthSession(
                token_hash="tok-hash",
                user_id=user.id,
                expires_at=db.models.utcnow() + timedelta(hours=1),
            )
        )
        session.add_all(_rows(db))
        await session.commit()

        job = await session.get(m.DownloadJob, 1)
        assert job.platform == "bilibili"
        assert job.status == "queued"
        assert (await session.get(m.LiveRecording, 1)).status == "recording"
        assert (await session.get(m.PlatformCredential, "instagram")).encrypted_blob == "enc-blob"
        assert (await session.get(m.NotificationRule, 1)).channel_type == "ntfy"
        lib = await session.get(m.LibraryItem, 1)
        assert lib.media_type == "video" and lib.size_bytes == 1234
        assert (await session.get(m.AppSetting, "concurrency")).value == "2"
        sess = (
            (await session.execute(select(m.AuthSession).where(m.AuthSession.token_hash == "tok-hash")))
            .scalars()
            .one()
        )
        assert sess.expires_at > db.models.utcnow()

        # updated_at set by onupdate on modification
        job.status = "downloading"
        await session.commit()
        assert job.updated_at >= job.created_at


async def test_wal_mode_enabled(db):
    from sqlalchemy import text

    async with db.engine.connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
    assert str(mode).lower() == "wal"


async def test_creator_watch_unique_constraint(db):
    from sqlalchemy.exc import IntegrityError

    async with db.async_session() as session:
        session.add(
            db.models.CreatorWatch(platform="xhs", creator_id="42", display_name="A")
        )
        await session.commit()

    async with db.async_session() as session:
        session.add(
            db.models.CreatorWatch(platform="xhs", creator_id="42", display_name="B")
        )
        with pytest.raises(IntegrityError):
            await session.commit()
