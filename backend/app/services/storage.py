"""Media-volume usage and the space floor, read in one place.

The floor used to be read twice with two defaults: the settings page showed
SettingsModel's 10% while the download gate fell back to a 5% of its own for
as long as the value had never been saved -- which on a fresh install is
forever. And it only ever gated auto downloads at the moment they started.
Live recordings, which are what actually fill the volume at 2-3 GB an evening,
never looked at it, so the volume ran out of space with the floor "set" at 10%.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path

# Percentage points above the floor before anything new is let back in. A
# capture stopped AT the floor frees its FLV once the remux lands, which puts
# free space right back above the line; without a margin the next sweep starts
# the same room again and the evening becomes a string of five-minute pieces.
RESUME_MARGIN_PCT = 2.0


def _db():
    """Fresh AsyncSession resolved at call time -- module reloads honored."""
    import app.db

    return app.db.async_session()


def media_root() -> Path:
    """Volume the floor measures. Imported lazily so tests can reload config."""
    from app.config import MEDIA_ROOT

    return Path(MEDIA_ROOT)


@dataclass(frozen=True)
class DiskUsage:
    total: int
    free: int

    @property
    def used(self) -> int:
        return self.total - self.free

    @property
    def free_pct(self) -> float:
        return self.free / self.total * 100 if self.total else 0.0


def disk_usage(path: Path | None = None) -> DiskUsage | None:
    """Bytes on the volume holding `path` (default: the media volume).

    `free` is what an unprivileged writer can still use, which is what an
    engine running as vduser actually runs into. None when the path cannot be
    read at all -- callers treat that as "unknown", never as "empty".
    """
    try:
        u = shutil.disk_usage(path or media_root())
    except OSError:
        return None
    return DiskUsage(total=u.total, free=u.free)


async def floor_pct() -> float:
    """The configured floor, defaults included, from the settings store."""
    from app.services.settings_store import aget_settings

    async with _db() as s:
        return float((await aget_settings(s)).space_floor_pct)


async def room_for_copy(size: int) -> bool:
    """Whether `size` more bytes still leave free space at or above the floor.

    A remux writes a whole second copy of the capture before the first is
    deleted. For a capture stopped AT the floor, or a four-hour one stopped
    near it, that copy is what fills the volume -- and a full volume is where
    the database and the log stop writing. Unknown usage answers yes: the
    remux then fails on its own terms, as it did before this check.
    """
    usage = disk_usage()
    if usage is None:
        return True
    return usage.free - size >= usage.total * await floor_pct() / 100


@dataclass(frozen=True)
class SpaceStatus:
    usage: DiskUsage
    floor_pct: float

    @property
    def below_floor(self) -> bool:
        """Past the line: running captures stop. A floor of 0 turns it off."""
        return self.floor_pct > 0 and self.usage.free_pct < self.floor_pct

    @property
    def start_pct(self) -> float:
        """Free % a new capture needs: floor + margin, or 0 with no floor."""
        return self.floor_pct + RESUME_MARGIN_PCT if self.floor_pct > 0 else 0.0

    @property
    def room_to_start(self) -> bool:
        return self.usage.free_pct >= self.start_pct

    def as_dict(self) -> dict:
        u = self.usage
        return {
            "total_bytes": u.total,
            "used_bytes": u.used,
            "free_bytes": u.free,
            "free_pct": round(u.free_pct, 2),
            "floor_pct": self.floor_pct,
            "floor_bytes": int(u.total * self.floor_pct / 100),
            "start_pct": self.start_pct,
            "below_floor": self.below_floor,
            "room_to_start": self.room_to_start,
        }


async def space_status() -> SpaceStatus | None:
    usage = disk_usage()
    if usage is None:
        return None
    return SpaceStatus(usage=usage, floor_pct=await floor_pct())
