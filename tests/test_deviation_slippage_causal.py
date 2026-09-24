"""Causal proof tests: ISSUE-047 — deviation=20 too tight for XAUUSD news moves.

Hypothesis
----------
send_order / close_position / close_position_with_fill submit market
orders with a hardcoded deviation of 20 POINTS = $0.20 on XAUUSD
(point=0.01). deviation is the max price move tolerated between the tick
fetch and execution: XAUUSD routinely moves > $0.20 in that window during
news/session opens — exactly when the signal fires and exactly when risk
is highest. The broker then rejects the order (REQUOTE / PRICE_OFF), so:

  - ENTRY: the signal dies for the cycle (rejected_signals 'order_send'),
    entry window missed (seen live: mr-bet-D 2026-09-23 00:25 UTC)
  - CLOSE: far worse — the close fails silently during a spike and the
    position stays open with unbounded risk while price runs.

The mechanism under test is the deviation VALUE passed as the 8th
positional argument to conn.root.order_send at all three call sites.

Causal proof
------------
Drive send_order and close_position_with_fill against a fake RPyC root
that records every order_send call. The captured deviation argument must
be a widened, env-tunable value — default 100 points ($1.00).

  - BEFORE fix: 20 at every call site → assertions fail (RED)
  - AFTER fix:  default 100, MT5_MAX_DEVIATION_POINTS override honored (GREEN)

References
----------
- ISSUE-047: deviation=20 too tight — rejections during news spread widening
- Live evidence: rejected_signals id 61558 (mr-bet-D 2026-09-23 00:25 UTC)
- Production: metty/bridge/client.py — send_order, close_position,
  close_position_with_fill (hardcoded `20, # deviation`)
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

# 8th positional argument of order_send (0-based index 7):
# (action, symbol, volume, type, price, sl, tp, DEVIATION, magic, ...)
DEVIATION_ARG_INDEX = 7

DEFAULT_DEVIATION_POINTS = 100  # $1.00 on XAUUSD (point = 0.01)


class _FakeRoot:
    """Records order_send calls; returns success like a healthy MT5."""

    def __init__(self):
        self.order_send_calls: list[tuple] = []

    def symbol_info_tick(self, symbol):
        return {
            "time": 0, "bid": 2296.0, "ask": 2297.0, "last": 0,
            "volume": 0, "time_msc": 0, "flags": 0, "volume_real": 0,
        }

    def positions_get(self, ticket=None):
        return [{
            "ticket": ticket, "time": 0, "time_update": 0, "type": 0,
            "magic": 0, "identifier": 0, "reason": 0, "volume": 0.05,
            "price_open": 2300.0, "sl": 0, "tp": 0, "price_current": 2296.0,
            "swap": 0, "profit": 0, "symbol": "XAUUSD", "comment": "",
        }]

    def order_send(self, *args):
        self.order_send_calls.append(args)
        return {
            "retcode": TRADE_RETCODE_DONE, "order": 123,
            "price": 2297.0, "volume": 0.05, "comment": "",
        }


def _make_bridge(monkeypatch) -> tuple:
    """MT5Bridge wired to a fake RPyC connection that records order args."""
    import metty.bridge.client as client_mod

    cfg = AccountConfig(
        name="test", broker_login="1", broker_server="s", balance=10000.0,
        leverage=100,
    )
    bridge = client_mod.MT5Bridge(cfg)
    root = _FakeRoot()
    conn = SimpleNamespace(root=root)
    monkeypatch.setattr(bridge, "_ensure_connected", lambda: conn)
    monkeypatch.setattr(bridge, "_resolve_symbol_name", lambda s: "XAUUSD")
    # Fake root already returns plain dicts — bypass netref conversion.
    monkeypatch.setattr(client_mod, "_netref_to_dict", lambda netref, columns=None: dict(netref))
    monkeypatch.setattr(client_mod, "_netref_to_list", lambda netref, columns=None: [dict(p) for p in netref])
    return bridge, root


class TestOrderDeviationWidened:
    """ISSUE-047: order_send deviation must tolerate news-time price moves."""

    def test_send_order_deviation_default_widened(self, monkeypatch):
        """Default deviation must be 100 points ($1.00), not the legacy 20.

        Pre-fix: hardcoded 20 → captured 20 ≠ 100 → RED.
        """
        monkeypatch.delenv("MT5_MAX_DEVIATION_POINTS", raising=False)
        bridge, root = _make_bridge(monkeypatch)

        result = asyncio.run(bridge.send_order("XAUUSD", "BUY", 0.05))

        assert result.success, f"fake root returns DONE — order must succeed: {result.error}"
        assert root.order_send_calls, "order_send must have been called"
        deviation = root.order_send_calls[0][DEVIATION_ARG_INDEX]
        assert deviation == DEFAULT_DEVIATION_POINTS, (
            f"ISSUE-047 unfixed: order deviation={deviation} points is inside "
            f"normal news-time XAUUSD movement — legacy 20 ($0.20) gets the order "
            f"rejected exactly when risk is highest (expected default "
            f"{DEFAULT_DEVIATION_POINTS} points)"
        )

    def test_send_order_deviation_env_override(self, monkeypatch):
        """MT5_MAX_DEVIATION_POINTS must be honored at call time."""
        monkeypatch.setenv("MT5_MAX_DEVIATION_POINTS", "250")
        bridge, root = _make_bridge(monkeypatch)

        result = asyncio.run(bridge.send_order("XAUUSD", "SELL", 0.05))

        assert result.success
        deviation = root.order_send_calls[0][DEVIATION_ARG_INDEX]
        assert deviation == 250, (
            f"MT5_MAX_DEVIATION_POINTS=250 must reach order_send, got {deviation}"
        )

    def test_close_position_deviation_widened(self, monkeypatch):
        """Close path must use the widened deviation too — a close rejected
        during a spike leaves the position open with unbounded risk."""
        monkeypatch.delenv("MT5_MAX_DEVIATION_POINTS", raising=False)
        bridge, root = _make_bridge(monkeypatch)

        ok, fill = asyncio.run(bridge.close_position_with_fill(ticket=123))

        assert ok is True and fill == pytest.approx(2297.0), (
            f"fake root returns DONE — close must succeed (ok={ok}, fill={fill})"
        )
        assert root.order_send_calls, "close order_send must have been called"
        deviation = root.order_send_calls[0][DEVIATION_ARG_INDEX]
        assert deviation == DEFAULT_DEVIATION_POINTS, (
            f"ISSUE-047 unfixed on close path: deviation={deviation} points — "
            f"a close rejected during a news spike strands the position open"
        )