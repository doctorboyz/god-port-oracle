"""Tests for shared.events — verify event bus pub/sub works correctly."""

from shared.events import EventBus, Event, EventType


class TestEventBus:
    def test_publish_and_subscribe(self):
        bus = EventBus()
        received = []

        def handler(event: Event):
            received.append(event)

        bus.subscribe(EventType.SIGNAL_GENERATED, handler)
        event = Event(type=EventType.SIGNAL_GENERATED, data={"signal": "BUY"})
        bus.publish(event)

        assert len(received) == 1
        assert received[0].data["signal"] == "BUY"

    def test_multiple_handlers(self):
        bus = EventBus()
        results = []

        bus.subscribe(EventType.PRICE_UPDATE, lambda e: results.append("handler_a"))
        bus.subscribe(EventType.PRICE_UPDATE, lambda e: results.append("handler_b"))

        bus.publish(Event(type=EventType.PRICE_UPDATE, data={"price": 1900.0}))
        assert len(results) == 2
        assert "handler_a" in results
        assert "handler_b" in results

    def test_history_records_events(self):
        bus = EventBus()
        bus.publish(Event(type=EventType.SIGNAL_GENERATED, data={"a": 1}))
        bus.publish(Event(type=EventType.TRADE_CLOSED, data={"b": 2}))

        history = bus.history()
        assert len(history) == 2
        assert history[0].type == EventType.SIGNAL_GENERATED
        assert history[1].type == EventType.TRADE_CLOSED

    def test_history_filter_by_type(self):
        bus = EventBus()
        bus.publish(Event(type=EventType.SIGNAL_GENERATED, data={}))
        bus.publish(Event(type=EventType.TRADE_CLOSED, data={}))
        bus.publish(Event(type=EventType.SIGNAL_GENERATED, data={}))

        signal_history = bus.history(EventType.SIGNAL_GENERATED)
        assert len(signal_history) == 2

    def test_handler_failure_does_not_crash_bus(self):
        bus = EventBus()
        good_results = []

        def bad_handler(event: Event):
            raise RuntimeError("boom")

        def good_handler(event: Event):
            good_results.append(event)

        bus.subscribe(EventType.ERROR, bad_handler)
        bus.subscribe(EventType.ERROR, good_handler)

        bus.publish(Event(type=EventType.ERROR, data={"msg": "test"}))
        assert len(good_results) == 1

    def test_clear_history(self):
        bus = EventBus()
        bus.publish(Event(type=EventType.SIGNAL_GENERATED, data={}))
        bus.clear_history()
        assert len(bus.history()) == 0

    def test_event_has_id_and_timestamp(self):
        event = Event(type=EventType.PRICE_UPDATE, data={"price": 1900.0})
        assert event.id is not None
        assert event.timestamp is not None