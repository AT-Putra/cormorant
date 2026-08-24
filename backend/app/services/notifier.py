"""Webhook notifier core (plan step 18) — one channel per NotificationRule.

Pure unit: rules arrive as dicts shaped like models.NotificationRule plus a
decrypted ``token`` when the channel needs one (ntfy Bearer, Telegram bot
token); Discord webhooks embed their token in the URL. No DB, no FastAPI.
send_event returns True only when a delivery was attempted AND accepted;
suppression/skip/failure all return False (failures are logged, never raised).
"""

import logging
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

EVENT_TOGGLES = {
    # event_type -> per-CreatorWatch toggle key (plan step 18)
    "golive": "notify_golive",
    "recording_started": "notify_recording",
    "recording_finished": "notify_recording",
    "post_downloaded": "notify_posts",
}

_TIMEOUT_S = 10.0


def _now() -> datetime:
    """Local wall clock — containers get TZ via env passthrough (plan step 23)."""
    return datetime.now()


def _parse_hhmm(value: str | None) -> tuple[int, int] | None:
    try:
        h, m = value.split(":")
        hour, minute = int(h), int(m)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (ValueError, AttributeError):
        pass
    return None


def in_quiet_hours(
    now: datetime, start: str | None, end: str | None
) -> bool:
    """True when `now` falls inside [start, end). Overnight windows
    (start > end, e.g. 23:00-07:00) wrap midnight. Equal/invalid bounds =
    no suppression."""
    s, e = _parse_hhmm(start), _parse_hhmm(end)
    if s is None or e is None or s == e:
        return False
    t = (now.hour, now.minute)
    if s < e:
        return s <= t < e
    return t >= s or t < e  # overnight wrap


def _default_message(event_type: str, context: dict) -> str:
    parts = [f"[{context.get('platform', '?')}] {event_type.replace('_', ' ')}"]
    for key in ("creator", "title"):
        if context.get(key):
            parts.append(str(context[key]))
    if context.get("url"):
        parts.append(context["url"])
    return "\n".join(parts)


class Notifier:
    def __init__(self, rule: dict) -> None:
        self.rule = rule

    async def send_event(self, event_type: str, context: dict) -> bool:
        """Deliver one event through this rule's channel.

        Order of gates: per-creator filter -> quiet hours -> HTTP send.
        Returns False (and sends nothing) when filtered or suppressed.
        """
        toggle = EVENT_TOGGLES.get(event_type)
        toggles = context.get("creator_toggles") or {}
        if toggle is not None and toggles.get(toggle) is False:
            return False  # creator opted out of this event type

        if in_quiet_hours(
            _now(), self.rule.get("quiet_hours_start"), self.rule.get("quiet_hours_end")
        ):
            log.info(
                "notification suppressed (quiet hours): %s %s",
                event_type,
                context.get("creator", ""),
            )
            return False

        message = context.get("message") or _default_message(event_type, context)
        try:
            ok, status = await self._dispatch(message, context)
        except Exception:
            log.exception("notification delivery failed (%s)", self.rule.get("channel_type"))
            return False
        if not ok:
            log.warning(
                "notification rejected: %s returned %s", self.rule.get("channel_type"), status
            )
        return ok

    async def _dispatch(self, message: str, context: dict) -> tuple[bool, int]:
        kind = self.rule.get("channel_type")
        target = self.rule.get("target") or ""
        title = context.get("title") or message.splitlines()[0]
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            if kind == "ntfy":
                headers = {"Title": title}
                token = self.rule.get("token")
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                resp = await client.post(target, content=message, headers=headers)
            elif kind == "telegram":
                resp = await client.post(
                    f"https://api.telegram.org/bot{self.rule.get('token')}/sendMessage",
                    json={"chat_id": target, "text": message},
                )
            elif kind == "discord":
                resp = await client.post(target, json={"content": message})
            else:
                raise ValueError(f"unknown channel_type: {kind!r}")
            return 200 <= resp.status_code < 300, resp.status_code
