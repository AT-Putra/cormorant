"""Media-volume status + space floor (services/storage, GET /api/storage)."""

import pytest
from sqlalchemy import select

from app.services import storage
from app.services.storage import DiskUsage, SpaceStatus, disk_usage as real_disk_usage

GIB = 1024**3


def status(free_pct: float, floor: float) -> SpaceStatus:
    return SpaceStatus(
        usage=DiskUsage(total=100 * GIB, free=int(free_pct * GIB)), floor_pct=floor
    )


@pytest.mark.parametrize(
    "free, below, room",
    [
        (9.9, True, False),  # past the line: running captures stop
        (10.0, False, False),  # at the floor: nothing stops, nothing new starts
        (11.9, False, False),  # inside the margin
        (12.0, False, True),  # floor + margin: room for a new capture
    ],
)
def test_floor_and_margin_boundaries(free, below, room):
    s = status(free, floor=10)
    assert s.below_floor is below
    assert s.room_to_start is room


def test_a_floor_of_zero_turns_the_gate_off():
    s = status(0.5, floor=0)
    assert s.below_floor is False
    assert s.room_to_start is True


def test_as_dict_reports_the_floor_in_bytes_too():
    d = status(25, floor=10).as_dict()
    assert d["total_bytes"] == 100 * GIB
    assert d["free_bytes"] == 25 * GIB
    assert d["used_bytes"] == 75 * GIB
    assert d["free_pct"] == 25.0
    assert d["floor_bytes"] == 10 * GIB
    assert d["start_pct"] == 12.0
    assert status(25, floor=0).as_dict()["start_pct"] == 0.0


def test_disk_usage_reads_the_real_volume(tmp_path):
    u = real_disk_usage(tmp_path)
    assert u is not None
    assert 0 < u.free <= u.total


def test_disk_usage_of_an_unreadable_path_is_unknown_not_empty(tmp_path):
    assert real_disk_usage(tmp_path / "does-not-exist") is None


def test_storage_endpoint(authed_client):
    client, _ = authed_client
    r = client.get("/api/storage")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["free_pct"] == 50.0  # conftest's roomy_disk
    assert body["floor_pct"] == 10  # the settings default, never saved
    assert body["below_floor"] is False
    assert body["room_to_start"] is True


def test_storage_endpoint_follows_the_saved_floor(authed_client):
    client, _ = authed_client
    assert client.put("/api/settings", json={"space_floor_pct": 50}).status_code == 200
    body = client.get("/api/storage").json()
    assert body["floor_pct"] == 50
    assert body["room_to_start"] is False  # 50% free, needs 52%


def test_storage_endpoint_requires_auth(anon_client):
    client, _ = anon_client
    assert client.get("/api/storage").status_code == 401


async def test_manual_record_is_refused_without_room(authed_client, monkeypatch):
    import app.db as db_mod
    from app.models import LiveRecording

    client, _ = authed_client
    monkeypatch.setattr(
        storage, "disk_usage", lambda path=None: DiskUsage(total=100 * GIB, free=5 * GIB)
    )

    r = client.post(
        "/api/downloads/record-live", json={"url": "https://www.tiktok.com/@someone/live"}
    )

    assert r.status_code == 507
    assert "5.0% free" in r.json()["detail"]
    async with db_mod.async_session() as s:
        assert (await s.execute(select(LiveRecording))).first() is None
