"""Causal tests — tick materialization through the rpyc bridge.

Live incident 2026-11-11 15:33-15:47 UTC: the first farmbet deployment
never resolved a single bet. Two bugs, both proven live against the real
bridge (mt5q at vpsdeluna:5012):

1. rpyc returns each tick as a dict NETREF — every field access is a
   ~50ms round trip to the VPS. `list(remote)` + per-field reads burned
   136 seconds for one 10-minute fetch, so the 60s poll cycle fell
   permanently behind.
2. The server's rpyc config blocks `.get()` on dict netrefs →
   `tick.get("time_msc")` crashed with AttributeError on tick #1.

Fix: TickFeed.ticks_since materializes the WHOLE remote list with one
str() round trip + ast.literal_eval locally (0.3s end-to-end), and the
engine reads fields with plain __getitem__ on local dicts.

These tests fail iff the regression returns: if ticks_since reverts to
returning netrefs (no bulk materialization), the .get() call on the
result raises (the exact live crash), and the per-element access counter
proves the netref's fields were never touched one-by-one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.bet_engine import TickFeed  # noqa: E402


class _NetrefDict:
    """Simulates an rpyc dict netref: __getitem__ goes over the wire and
    .get() is BLOCKED by the server's rpyc config (live crash)."""

    def __init__(self, data: dict, counter: list) -> None:
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_counter", counter)

    def __getitem__(self, key):
        self._counter[0] += 1  # each field access = one network round trip
        return self._data[key]

    def get(self, key):  # pragma: no cover - the exact live failure
        raise AttributeError("cannot access 'get'")


class _NetrefList:
    """Simulates an rpyc list netref: iterating/len are remote calls, and
    str() renders the entire list SERVER-side in one round trip."""

    def __init__(self, items: list[dict], counter: list) -> None:
        self._items = [_NetrefDict(d, counter) for d in items]
        self._plain = items
        self._counter = counter

    def __len__(self):
        self._counter[0] += 1
        return len(self._items)

    def __bool__(self):
        return bool(self._items)

    def __str__(self):
        # one round trip: the server renders the whole list to its repr
        self._counter[0] += 1
        return str(self._plain)


class _FakeRoot:
    def __init__(self, payload) -> None:
        self._payload = payload

    def initialize(self):
        return True

    def symbol_select(self, symbol, enable=True):
        return True

    def symbol_info(self, symbol):
        return {"name": symbol}

    def copy_ticks_from(self, symbol, from_epoch, count, flags):
        return self._payload


class _FakeConn:
    def __init__(self, payload) -> None:
        self.root = _FakeRoot(payload)

    def close(self):
        pass


@pytest.fixture()
def netref_feed(monkeypatch):
    """A TickFeed whose rpyc.connect returns netref-style tick payloads."""
    ticks = [
        {"time": 1794411635, "bid": 4005.956, "ask": 4006.124,
         "time_msc": 1794411635738},
        {"time": 1794411636, "bid": 4005.900, "ask": 4006.100,
         "time_msc": 1794411636891},
    ]
    counter = [0]
    payload = _NetrefList(ticks, counter)

    import rpyc  # noqa: F401  (imported by TickFeed._connect)

    monkeypatch.setattr("rpyc.connect", lambda *a, **k: _FakeConn(payload))
    return TickFeed(), counter, ticks


class TestTicksSinceMaterialization:
    def test_returns_plain_local_dicts(self, netref_feed):
        feed, _counter, _ticks = netref_feed
        result = feed.ticks_since(1794411000)
        assert result is not None and len(result) == 2
        # bulk materialized: plain dicts, not netref stand-ins
        for tick in result:
            assert type(tick) is dict

    def test_get_works_on_materialized_ticks(self, netref_feed):
        """The live crash: tick.get('time_msc') raised on netrefs. On the
        materialized result it must work (regression = AttributeError)."""
        feed, _counter, _ticks = netref_feed
        result = feed.ticks_since(1794411000)
        assert result[0].get("time_msc") == 1794411635738

    def test_no_per_field_round_trips(self, netref_feed):
        """The netref grind: per-field reads would push the round-trip
        counter to O(n_ticks x fields). The bulk str() path leaves it at a
        handful of calls regardless of tick count."""
        feed, counter, ticks = netref_feed
        result = feed.ticks_since(1794411000)
        assert result is not None
        # 1 (str/list render) + at most a few (len/bool/connect) — NOT
        # n_ticks x per-field accesses
        assert counter[0] <= 3, (
            f"ticks_since made {counter[0]} remote round trips for "
            f"{len(ticks)} ticks — per-field netref access is back"
        )

    def test_empty_remote_list_becomes_empty_local(self, netref_feed):
        feed, _counter, _ticks = netref_feed
        feed._conn = None  # force reconnect
        assert feed.ticks_since(1794411000) is not None
        # (payload here is non-empty; the empty case is covered by the
        # `if not ticks: return []` branch — assert [] stays [], not None)
        class _EmptyList(_NetrefList):
            def __init__(self, counter):
                super().__init__([], counter)

            def __bool__(self):
                return False

        empty_counter = [0]
        feed2 = TickFeed()
        import rpyc
        rpyc.connect = lambda *a, **k: _FakeConn(_EmptyList(empty_counter))
        assert feed2.ticks_since(0) == []
