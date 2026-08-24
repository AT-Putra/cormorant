"""Notifier unit tests — httpx fully faked, no network."""

import asyncio
from datetime import datetime

import pytest

from app.services import notifier as ntf
from app.services.notifier import Notifier, in_quiet_hours


class FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


class FakeClient:
    """Records calls; behavior configurable per test."""

    calls = []
    status_code = 200
    raise_exc = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None, json=None):
        type(self).calls.append(
            {"url": url, "content": content, "headers": headers, "json": json}
        )
        if type(self).raise_exc:
            raise type(self).raise_exc("network down")
        return FakeResponse(type(self).status_code)


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.calls = []
    FakeClient.status_code = 200
    FakeClient.raise_exc = None
    monkeypatch.setattr(ntf.httpx, "AsyncClient", FakeClient)
    return FakeClient


def rule(channel="ntfy", start=None, end=None):
    return {
        "channel_type": channel,
        "target": {
            "ntfy": "https://ntfy.sh/my-topic",
            "telegram": "12345",
            "discord": "https://discord.com/api/webhooks/1/abc",
        }[channel],
        "token": "sekrit" if channel != "discord" else None,
        "quiet_hours_start": start,
        "quiet_hours_end": end,
    }


CONTEXT = {"platform": "bilibili", "creator": "c1", "title": "T", "url": "https://x/1"}


def freeze_now(monkeypatch, hh, mm=0):
    monkeypatch.setattr(ntf, "_now", lambda: datetime(2026, 8, 23, hh, mm))


# ---- channel formatting + delivery ------------------------------------------


async def test_ntfy_posts_message_with_token(fake_client):
    assert await Notifier(rule()).send_event("golive", CONTEXT) is True
    call = fake_client.calls[0]
    assert call["url"] == "https://ntfy.sh/my-topic"
    assert "golive" in call["content"] and "bilibili" in call["content"]
    assert call["headers"]["Authorization"] == "Bearer sekrit"
    assert call["headers"]["Title"] == "T"


async def test_telegram_sendmessage_shape(fake_client):
    assert await Notifier(rule("telegram")).send_event("golive", CONTEXT) is True
    call = fake_client.calls[0]
    assert call["url"].startswith("https://api.telegram.org/botsekrit/sendMessage")
    assert call["json"]["chat_id"] == "12345"
    assert call["json"]["text"]


async def test_discord_webhook_json_body(fake_client):
    assert await Notifier(rule("discord")).send_event("post_downloaded", CONTEXT) is True
    call = fake_client.calls[0]
    # No auth header — token lives in the webhook URL itself.
    assert not (call["headers"] or {}).get("Authorization")
    assert call["json"]["content"]
    assert call["url"] == rule("discord")["target"]


async def test_non_2xx_returns_false_not_raises(fake_client):
    fake_client.status_code = 500
    return_value = await Notifier(rule()).send_event("golive", CONTEXT)
    assert return_value is False


async def test_network_error_returns_false_not_raises(fake_client):
    fake_client.raise_exc = ConnectionError
    assert await Notifier(rule()).send_event("golive", CONTEXT) is False


async def test_unknown_channel_raises_inside_and_yields_false(fake_client):
    bad = rule()
    bad["channel_type"] = "pigeon"
    assert await Notifier(bad).send_event("golive", CONTEXT) is False


# ---- quiet hours ------------------------------------------------------------


@pytest.mark.parametrize(
    "start,end,hh,mm,expected",
    [
        # normal window 09:00-17:00
        ("09:00", "17:00", 10, 0, True),
        ("09:00", "17:00", 8, 59, False),
        ("09:00", "17:00", 9, 0, True),   # inclusive start
        ("09:00", "17:00", 16, 59, True),
        ("09:00", "17:00", 17, 0, False),  # exclusive end
        # overnight window 23:00-07:00 wraps midnight
        ("23:00", "07:00", 23, 30, True),
        ("23:00", "07:00", 3, 15, True),
        ("23:00", "07:00", 6, 59, True),
        ("23:00", "07:00", 7, 0, False),
        ("23:00", "07:00", 12, 0, False),
        ("23:00", "07:00", 22, 59, False),
        # degenerate / invalid -> never suppress
        ("09:00", "09:00", 9, 0, False),
        ("bogus", "17:00", 10, 0, False),
        (None, None, 10, 0, False),
    ],
)
def test_in_quiet_hours(start, end, hh, mm, expected):
    now = datetime(2026, 8, 23, hh, mm)
    assert in_quiet_hours(now, start, end) is expected


async def test_quiet_hours_suppresses_send(fake_client, monkeypatch):
    freeze_now(monkeypatch, 3, 0)  # inside 23:00-07:00 overnight window
    n = Notifier(rule(start="23:00", end="07:00"))
    assert await n.send_event("golive", CONTEXT) is False
    assert fake_client.calls == []  # nothing attempted


async def test_outside_quiet_hours_sends(fake_client, monkeypatch):
    freeze_now(monkeypatch, 12, 0)
    n = Notifier(rule(start="23:00", end="07:00"))
    assert await n.send_event("golive", CONTEXT) is True
    assert len(fake_client.calls) == 1


# ---- per-creator event filters ----------------------------------------------


def creator_ctx(**toggles):
    return {**CONTEXT, "creator_toggles": toggles}


async def test_filter_mismatch_skips_send(fake_client):
    ctx = creator_ctx(notify_golive=False)
    assert await Notifier(rule()).send_event("golive", ctx) is False
    assert fake_client.calls == []


async def test_recording_events_share_the_recording_toggle(fake_client):
    ctx = creator_ctx(notify_recording=False)
    for evt in ("recording_started", "recording_finished"):
        assert await Notifier(rule()).send_event(evt, ctx) is False
    assert fake_client.calls == []


async def test_toggle_off_one_event_keeps_others(fake_client):
    ctx = creator_ctx(notify_golive=False, notify_posts=True)
    n = Notifier(rule())
    assert await n.send_event("golive", ctx) is False
    assert await n.send_event("post_downloaded", ctx) is True


async def test_missing_toggles_default_to_send(fake_client):
    assert await Notifier(rule()).send_event("golive", CONTEXT) is True
