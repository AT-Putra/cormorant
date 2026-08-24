"""US-011 tests: activity mirror writes, /api/activity API, /api/ws auth+stream."""

import asyncio
from datetime import timedelta

import pytest

from app.auth import SESSION_COOKIE
from tests.conftest import current_db


def _publish(event: dict) -> None:
    from app.services import events

    assert events._subscribers  # activity mirror installed at import time
    events.publish(event)


async def _drain_mirror() -> None:
    """Deterministically wait for mirror writes: poll DB until rows land or timeout.

    (Awaiting _pending alone proved racy across pytest-asyncio loop setups;
    polling the actual DB is the observable contract we care about.)
    """
    import asyncio

    from tests.conftest import current_db

    db = current_db()
    from app.models import ActivityLog

    deadline = asyncio.get_event_loop().time() + 5
    while asyncio.get_event_loop().time() < deadline:
        async with db.async_session() as s:
            n = len((await s.execute(ActivityLog.__table__.select())).fetchall())
        if n >= _expected_rows:
            return
        # also let any not-yet-run tasks get scheduled
        await asyncio.sleep(0.05)
        import app.services.activity as activity_mod

        if activity_mod._pending:
            await asyncio.wait(list(activity_mod._pending), timeout=1)


_expected_rows = 0


async def test_activity_rows_for_job_events(authed_client):
    c, _stub = authed_client
    db = current_db()
    import app.services.activity as activity_mod
    from app.models import ActivityLog

    # Invoke the exact subscriber publish() invokes, then await its write tasks
    # deterministically (task-based drain proved racy across loop setups; the
    # synchronous-subscriber + awaited-task form tests the same contract).
    events = __import__("app.services.events", fromlist=["events"])
    assert events._subscribers
    mirror = next(s for s in events._subscribers if getattr(s, "__name__", "") == "_mirror")
    tasks = []
    loop = asyncio.get_running_loop()
    for etype in ("job.started", "job.done", "job.failed", "job.skipped"):
        event = {"type": etype, "job_id": 7}
        mirror(event)
        tasks.extend(t for t in activity_mod._pending if not t.done())
    if tasks:
        await asyncio.wait(tasks, timeout=5)

    async with db.async_session() as s:
        rows = (await s.execute(ActivityLog.__table__.select())).fetchall()
    types = {r.event_type for r in rows}
    assert {"job.started", "job.done", "job.failed", "job.skipped"} <= types
    done_row = next(r for r in rows if r.event_type == "job.done")
    assert done_row.ref_type == "job" and done_row.ref_id == "7"
    assert done_row.message.startswith("job.done")


async def test_progress_skipped_and_suppression_canonicalized(authed_client):
    c, _stub = authed_client
    db = current_db()
    import app.services.activity as activity_mod
    from app.models import ActivityLog

    events = __import__("app.services.events", fromlist=["events"])
    mirror = next(s for s in events._subscribers if getattr(s, "__name__", "") == "_mirror")
    tasks = []
    loop = asyncio.get_running_loop()
    for event in (
        {"type": "job.progress", "job_id": 3, "percent": 42.0},
        {"type": "notification.suppress_quiet_hours", "creator": "x"},
        {"type": "recording.started", "recording_id": 9},
    ):
        mirror(event)
        tasks.extend(t for t in activity_mod._pending if not t.done())
    if tasks:
        await asyncio.wait(tasks, timeout=5)

    async with db.async_session() as s:
        rows = (await s.execute(ActivityLog.__table__.select())).fetchall()
    types = [r.event_type for r in rows]
    assert "job.progress" not in types
    assert "notification.suppressed" in types
    rec_row = next(r for r in rows if r.event_type == "recording.started")
    assert rec_row.ref_type == "recording" and rec_row.ref_id == "9"


async def test_activity_api_pagination_and_filter(authed_client):
    c, _stub = authed_client
    db = current_db()
    from app.models import ActivityLog, utcnow

    async with db.async_session() as s:
        base = utcnow() - timedelta(minutes=10)
        for i in range(5):
            s.add(
                ActivityLog(
                    ts=base + timedelta(seconds=i),
                    event_type="job.failed" if i % 2 else "job.done",
                    message=f"m{i}",
                )
            )
        await s.commit()

    page1 = c.get("/api/activity", params={"limit": 2}).json()
    assert [row["message"] for row in page1] == ["m4", "m3"]  # newest first

    page2 = c.get("/api/activity", params={"limit": 2, "offset": 2}).json()
    assert [row["message"] for row in page2] == ["m2", "m1"]

    filtered = c.get("/api/activity", params={"event_type": "job.failed"}).json()
    assert len(filtered) == 2
    assert {row["event_type"] for row in filtered} == {"job.failed"}

    # Auth middleware still guards the route.
    c.cookies.delete(SESSION_COOKIE)
    assert c.get("/api/activity").status_code == 401


def test_ws_receives_published_events(authed_client):
    from app.services import events

    c, _stub = authed_client
    cookie = next(iter(c.cookies.jar))
    token = cookie.value

    # websocket_connect bypasses the httpx transport, so the session cookie
    # goes as an explicit header rather than relying on the client jar.
    with c.websocket_connect(
        "/api/ws", headers={"cookie": f"{SESSION_COOKIE}={token}"}
    ) as ws:
        events.publish({"type": "job.started", "job_id": 1})
        events.publish({"type": "job.done", "job_id": 1})
        got = [ws.receive_json(), ws.receive_json()]
    types = [e["type"] for e in got]
    assert "job.started" in types and "job.done" in types


def test_ws_unauthenticated_closed(anon_client):
    c, db_mod = anon_client
    with pytest.raises(Exception):
        with c.websocket_connect("/api/ws") as ws:
            ws.receive_json()
