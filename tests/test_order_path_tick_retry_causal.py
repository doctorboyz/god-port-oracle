"""Causal proof tests: ISSUE-091 — transient symbol_info_tick failure kills the order.

Hypothesis
----------
send_order fetches the tick ONCE (client.py, send_market_order path):
a transient empty tick right after a reconnect (Market Watch sync) makes
_netref_to_dict return {} and the whole signal dies with
"Symbol XAUUSD not found" — no order is ever sent, and the entry window is
missed for the full 5-min cycle. Live evidence: mr-bet-D 2026-09-23 00:25
UTC, rejected_signals id 61558; the NEXT cycle's identical signal executed
fine (trade #125) — proof the first failure was transient, not a bad symbol.

A related hazard in the same mechanism: a tick dict with bid=ask=0 (also
seen transiently) is NOT caught by the current `if not tick:` check — the
order proceeds at price 0.0 and only dies at the broker, if at all.

The mechanism under test is the tick FETCH before order_send. Retrying it
is idempotency-safe: no order has been sent yet (contrast: retrying AFTER
order_send could double-fill).

Causal proof
------------
Drive send_order against a fake RPyC root whose symbol_info_tick returns
a scripted sequence:

  1. [{}, {}, GOOD]      → order MUST execute (pre-fix: dies on attempt 1)
  2. [ZERO, GOOD]        → order MUST price from the GOOD tick
                            (pre-fix: order sent at price 0.0)
  3. [{}, {}, {}, ...]   → bounded: exactly _TICK_FETCH_ATTEMPTS fetches,
                            no order_send, same "not found" failure as today

References
----------
- ISSUE-091: no retry on the order-path tick fetch
- ISSUE-088 (fixed, d50570c): same flaky-bridge family, candle-fetch side
- Live evidence: rejected_signals id 61558 (mr-bet-D 2026-09-23 00:25 UTC)
- Production: metty/bridge/client.py send_order — single tick fetch
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metty.bridge.client import TRADE_RETCODE_DONE  # noqa: E402
from metty.core.models import AccountConfig  # noqa: E402

# order_send positional args: (action, symbol, volume, type, PRICE, sl, tp, ...)
PRICE_ARG_INDEX = 4

GOOD_TICK = {
    "time": 0, "bid": 2296.0, "ask": 2297.0, "last": 0,
    "volume": 0, "time_msc": 0, "flags": 0, "volume_real": 0,
}
ZERO_TICK = {k: 0 for k in GOOD_TICK}
EMPTY_TICK = {}


class _FlakyTickRoot:
    """symbol_info_tick returns a scripted sequence; order_send is recorded."""

    def __init__(self, tick_sequence: list):
        self.tick_sequence = list(tick_sequence)
        self.tick_calls = 0
        self.order_send_calls: list[tuple] = []

    def symbol_info_tick(self, symbol):
        idx = min(self.tick_calls, len(self.tick_sequence) - 1)
        item = self.tick_sequence[idx]
        self.tick_calls += 1
        return item

    def order_send(self, *args):
        self.order_send_calls.append(args)
        return {
            "retcode": TRADE_RETCODE_DONE, "order": 123,
            "price": 2297.0, "volume": 0.05, "comment": "",
        }


def _make_bridge(monkeypatch, tick_sequence) -> tuple:
    import metty.bridge.client as client_mod

    # Keep the suite fast: no real backoff sleeping in tests.
    monkeypatch.setattr(client_mod, "_TICK_RETRY_BACKOFF", (0.0, 0.0), raising=False)

    cfg = AccountConfig(name="test", broker_login="1", broker_server="s", balance=10000.0)
    bridge = client_mod.MT5Bridge(cfg)
    root = _FlakyTickRoot(tick_sequence)
    conn = SimpleNamespace(root=root)
    monkeypatch.setattr(bridge, "_ensure_connected", lambda: conn)
    monkeypatch.setattr(bridge, "_resolve_symbol_name", lambda s: "XAUUSD")
    monkeypatch.setattr(client_mod, "_netref_to_dict", lambda netref, columns=None: dict(netref))
    monkeypatch.setattr(client_mod, "_netref_to_list", lambda netref, columns=None: [dict(p) for p in netref])
    return bridge, root


class TestTransientTickRetry:
    """ISSUE-091: transient tick failure must not kill the order."""

    def test_transient_empty_tick_retried_then_order_sent(self, monkeypatch):
        """Two empty ticks (Market Watch sync) then a good tick → order MUST send.

        Pre-fix: first empty tick → "Symbol not found", order never sent → RED.
        """
        bridge, root = _make_bridge(monkeypatch, [EMPTY_TICK, EMPTY_TICK, GOOD_TICK])

        result = asyncio.run(bridge.send_order("XAUUSD", "BUY", 0.05))

        assert result.success, (
            f"ISSUE-091 unfixed: transient empty tick killed the signal "
            f"(error={result.error!r}) — a good tick was available one fetch later"
        )
        assert root.order_send_calls, "order_send must have been called after retry"
        assert root.tick_calls == 3, (
            f"exactly 3 tick fetches expected (2 empty + 1 good), got {root.tick_calls}"
        )

    def test_zero_bid_ask_tick_not_sent_as_price_zero(self, monkeypatch):
        """A bid=ask=0 tick must be retried, never sent as a $0 order.

        Pre-fix: the zero tick passed the `if not tick:` check and the order
        went out at price 0.0 → RED on the price assertion.
        """
        bridge, root = _make_bridge(monkeypatch, [ZERO_TICK, GOOD_TICK])

        result = asyncio.run(bridge.send_order("XAUUSD", "BUY", 0.05))

        assert result.success, (
            f"order must execute off the good tick (error={result.error!r})"
        )
        assert root.order_send_calls, "order_send must have been called"
        price = root.order_send_calls[0][PRICE_ARG_INDEX]
        assert price == pytest.approx(2297.0), (
            f"ISSUE-091 unfixed: order sent at price {price} from a zero bid/ask tick "
            f"instead of retrying for a usable tick"
        )

    def test_persistent_failure_bounded_and_no_order(self, monkeypatch):
        """Persistent tick failure: bounded attempts, no order, same failure shape.

        Pre-fix: gave up after 1 fetch; post-fix: exactly _TICK_FETCH_ATTEMPTS.
        Guards against an unbounded retry loop on a real outage.
        """
        import metty.bridge.client as client_mod

        bridge, root = _make_bridge(monkeypatch, [EMPTY_TICK] * 10)

        result = asyncio.run(bridge.send_order("XAUUSD", "BUY", 0.05))

        assert not result.success, "persistent outage must still fail cleanly"
        assert "not found" in (result.error or "").lower()
        assert root.order_send_calls == [], "no order may be sent without a usable tick"
        assert root.tick_calls == client_mod._TICK_FETCH_ATTEMPTS, (
            f"retry must be bounded to _TICK_FETCH_ATTEMPTS="
            f"{client_mod._TICK_FETCH_ATTEMPTS}, got {root.tick_calls} fetches"
        )