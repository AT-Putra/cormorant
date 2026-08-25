"""ORM models — the spec ontology (plan §2), SQLAlchemy 2.0 style.

Status/kind/scope values are plain str columns; allowed values are validated
later in Pydantic schemas (US-005). JSON-ish fields are Text, json.dumps'd at
the service layer. Timestamps are naive UTC — SQLite's DateTime doesn't round-trip
tzinfo, so storing aware values yields naive reads and mixed comparisons break.
"""

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive UTC 'now' — matches what SQLite round-trips back."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class DownloadJob(Base):
    __tablename__ = "download_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str]
    platform: Mapped[str]
    kind: Mapped[str]  # video|images|story
    title: Mapped[str]
    creator: Mapped[str]
    format_id: Mapped[str | None]
    selected_quality: Mapped[str | None]
    # queued|probing|downloading|paused|paused_space_floor|done|failed|skipped
    status: Mapped[str] = mapped_column(default="queued", index=True)
    progress: Mapped[float] = mapped_column(default=0.0)
    output_path: Mapped[str | None]
    error: Mapped[str | None]
    is_auto: Mapped[bool] = mapped_column(default=False)
    redownload_requested: Mapped[bool] = mapped_column(default=False)
    # First probe of the current/last run; refreshed on each run.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Terminal-status timestamp (done|failed|skipped).
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class LiveRecording(Base):
    __tablename__ = "live_recordings"

    id: Mapped[int] = mapped_column(primary_key=True)
    room_url: Mapped[str]
    platform: Mapped[str]
    creator: Mapped[str]
    origin: Mapped[str]  # watchlist|manual
    # recording|finished|failed|interrupted|ended
    status: Mapped[str] = mapped_column(default="recording", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    output_path: Mapped[str | None]
    error: Mapped[str | None]


class CreatorWatch(Base):
    __tablename__ = "creator_watches"
    __table_args__ = (UniqueConstraint("platform", "creator_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str]
    creator_id: Mapped[str]
    display_name: Mapped[str]
    scope: Mapped[str] = mapped_column(default="both")  # lives|posts|both
    enabled: Mapped[bool] = mapped_column(default=True)
    # Per-creator webhook event toggles (plan step 18).
    notify_golive: Mapped[bool] = mapped_column(default=True)
    notify_recording: Mapped[bool] = mapped_column(default=True)
    notify_posts: Mapped[bool] = mapped_column(default=True)
    last_seen_post_id: Mapped[str | None]  # poller cursor (plan step 15)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlatformCredential(Base):
    __tablename__ = "platform_credentials"

    platform: Mapped[str] = mapped_column(primary_key=True)
    encrypted_blob: Mapped[str] = mapped_column(Text)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class NotificationRule(Base):
    __tablename__ = "notification_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_type: Mapped[str]  # ntfy|telegram|discord
    target: Mapped[str]
    encrypted_config: Mapped[str] = mapped_column(Text)
    quiet_hours_start: Mapped[str | None]  # HH:MM
    quiet_hours_end: Mapped[str | None]  # HH:MM


class LibraryItem(Base):
    __tablename__ = "library_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    file_path: Mapped[str] = mapped_column(unique=True)
    thumbnail_path: Mapped[str | None]
    platform: Mapped[str]
    creator: Mapped[str]
    title: Mapped[str]
    media_type: Mapped[str]  # video|image_set|audio|recording
    size_bytes: Mapped[int]
    duration_seconds: Mapped[float | None]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON-encoded at the service layer


class AppUser(Base):
    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    password_hash: Mapped[str]


class AuthSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    user: Mapped["AppUser"] = relationship()


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    event_type: Mapped[str] = mapped_column(index=True)
    message: Mapped[str]
    ref_type: Mapped[str | None]  # job|recording|watch|None
    # ponytail: Text so numeric row ids and string creator ids share a column;
    # split into typed columns if filtering by ref ever needs joins.
    ref_id: Mapped[str | None]
