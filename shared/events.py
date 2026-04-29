"""Simple event bus for inter-module communication.

Broky produces events (signals, trade results) that Metty consumes.
The event bus allows loose coupling between modules.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    SIGNAL_GENERATED = "signal_generated"
    TRADE_OPENED = "trade_opened"
    TRADE_CLOSED = "trade_closed"
    CIRCUIT_BREAKER_TRIGGERED = "circuit_breaker_triggered"
    CIRCUIT_BREAKER_RESET = "circuit_breaker_reset"
    PRICE_UPDATE = "price_update"
    SCALING_ACTION = "scaling_action"
    DAILY_SUMMARY = "daily_summary"
    ERROR = "error"


@dataclass
class Event:
    type: EventType
    data: dict[str, Any]
    id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


Handler = Callable[[Event], None]


class EventBus:
    """In-process event bus for loose coupling between Broky and Metty."""

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[Handler]] = defaultdict(list)
        self._history: list[Event] = []

    def subscribe(self, event_type: EventType, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: Event) -> None:
        self._history.append(event)
        handlers = self._handlers.get(event.type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception:
                logger.exception("Handler %s failed for event %s", handler.__name__, event.type)

    def history(self, event_type: Optional[EventType] = None) -> list[Event]:
        if event_type is None:
            return list(self._history)
        return [e for e in self._history if e.type == event_type]

    def clear_history(self) -> None:
        self._history.clear()