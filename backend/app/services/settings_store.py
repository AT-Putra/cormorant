"""Typed settings accessor over AppSetting rows (JSON-encoded values)."""

import json
from dataclasses import dataclass, asdict, fields
from string import Formatter

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppSetting
from app.services.ytdlp import FOLDER_FIELDS, QUALITY_CHOICES


def decode_setting(raw: str):
    """One AppSetting.value -> Python.

    Values are json.dumps()'d on the way in, so they must be json.loads()'d on
    the way out or every string setting comes back wearing literal quote
    characters. Anything that will not parse is returned as-is: a row written
    before the JSON encoding existed is a bare string, not corruption.
    """
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


@dataclass
class SettingsModel:
    # Must stay renderable by ytdlp.output_dir, which supplies exactly
    # FOLDER_FIELDS. The old default named {title}, which output_dir does not
    # supply, so the moment this value was persisted every download died on
    # KeyError('title'). README documents {platform}/{creator} as the layout.
    folder_template: str = "{platform}/{creator}"
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
            decoded = decode_setting(row.value)
            if isinstance(decoded, type(merged[row.key])):
                merged[row.key] = decoded
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


def folder_template_error(template: str) -> str | None:
    """Why this template cannot be rendered, or None if it can.

    Checked at save time because the alternative is discovering it at download
    time, where .format() raises between decrypting the cookie file and
    starting the engine — a failed job with a confusing KeyError for a message.
    """
    try:
        parsed = list(Formatter().parse(template))
    except ValueError as exc:  # stray '{' or '}'
        return f"folder_template is not a valid template: {exc}"
    # "{creator[0]}" / "{creator.x}" parse with field_name "creator[0]"; only
    # the root name has to be something output_dir supplies.
    used = {
        name.split("[")[0].split(".")[0]
        for _, name, _, _ in parsed
        if name
    }
    unknown = sorted(used - set(FOLDER_FIELDS))
    if unknown:
        known = ", ".join("{" + f + "}" for f in FOLDER_FIELDS)
        bad = ", ".join("{" + u + "}" for u in unknown)
        return f"folder_template has no {bad} to fill in; available: {known}"
    return None


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
    else:
        problem = folder_template_error(s["folder_template"])
        if problem:
            errors.append(problem)
    if s["default_quality"] not in QUALITY_CHOICES:
        errors.append(f"default_quality must be one of {', '.join(QUALITY_CHOICES)}")
    if errors:
        raise ValueError("; ".join(errors))
