"""Minimal in-process pub/sub. WebSocket broadcast (US-011) subscribes later."""

from collections.abc import Callable
import logging

log = logging.getLogger(__name__)

Subscriber = Callable[[dict], None]
_subscribers: list[Subscriber] = []


def subscribe(fn: Subscriber) -> None:
    _subscribers.append(fn)


def unsubscribe(fn: Subscriber) -> None:
    try:
        _subscribers.remove(fn)
    except ValueError:
        pass


def publish(event: dict) -> None:
    for fn in list(_subscribers):
        try:
            fn(event)
        except Exception:
            log.exception("event subscriber raised")
