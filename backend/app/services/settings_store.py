"""Typed settings accessor over AppSetting rows (JSON-encoded values)."""

import json
from dataclasses import dataclass, asdict, fields

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting
from app.services.ytdlp import QUALITY_CHOICES


@dataclass
class SettingsModel:
    folder_template: str = "{platform}/{creator}/{title}"
    concurrency_cap: int = 3
    poll_interval_seconds: int = 300
    space_floor_pct: int = 10
    default_quality: str = "best"


async def aget_settings(session: AsyncSession) -> SettingsModel:
    from sqlalchemy import select

    merged = asdict(SettingsModel())
    rows = (await session.execute(select(AppSetting))).scalars().all()
    for row in rows:
        if row.key in merged:
            try:
                merged[row.key] = json.loads(row.value)
            except (ValueError, TypeError):
                pass
    return SettingsModel(**merged)


async def save_settings(session: AsyncSession, partial: dict) -> SettingsModel:
    """Validate + persist a partial update; returns the new merged settings."""
    current = asdict(await aget_settings(session))
    valid_keys = {f.name: f.type for f in fields(SettingsModel)}
    for key, value in partial.items():
        if key not in valid_keys:
            raise ValueError(f"Unknown setting: {key}")
        current[key] = value
    _validate(current)

    from sqlalchemy import select

    for key, value in partial.items():
        if key not in valid_keys:
            continue
        row = (
            await session.execute(select(AppSetting).where(AppSetting.key == key))
        ).scalar_one_or_none()
        encoded = json.dumps(value)
        if row is None:
            session.add(AppSetting(key=key, value=encoded))
        else:
            row.value = encoded
    await session.commit()
    return await aget_settings(session)


def _validate(s: dict) -> None:
    errors = []
    if not (1 <= s["concurrency_cap"] <= 8):
        errors.append("concurrency_cap must be 1..8")
    if s["poll_interval_seconds"] < 60:
        errors.append("poll_interval_seconds must be >= 60")
    if not (0 <= s["space_floor_pct"] <= 50):
        errors.append("space_floor_pct must be 0..50")
    if not s["folder_template"]:
        errors.append("folder_template must not be empty")
    if s["default_quality"] not in QUALITY_CHOICES:
        errors.append(f"default_quality must be one of {', '.join(QUALITY_CHOICES)}")
    if errors:
        raise ValueError("; ".join(errors))
