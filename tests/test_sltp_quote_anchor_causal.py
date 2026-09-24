"""Causal proof tests: ISSUE-046 — order SL/TP anchored to stale signal.price.

Hypothesis
----------
The live path recomputes SL/TP for the broker order from `est_fill_price =
signal.price ± spread/2` (the ISSUE-053 estimate). signal.price is an M5
close, up to 5 minutes stale, and the spread/2 correction only fixes the
bid/ask offset — it says nothing about where price has DRIFTED since the
bar closed. On a fast move (news, session open) the absolute SL/TP sent
with the order can sit several dollars away from the true fill, so the
realized risk distance ≠ configured ATR distance:
  - BUY: SL too far below actual ask → risk larger than configured
  - SELL: SL too far above actual bid → same
  - TP may sit where price already passed (instant TP or unreachable)

The mechanism under test is the ANCHOR of calculate_stop_loss /
calculate_take_profit on the order path: it must be the live quote's fill
side (ask for BUY, bid for SELL) — exactly the price the broker fills at —
with the old spread/2 estimate kept only as fallback when the quote is
unavailable.

Causal proof
------------
Drive run_once() through the full happy path into the LIVE execution path
(dry_run=False) with a mocked MT5Bridge that captures the send_order
arguments. signal.price = 2300.0 (stale M5 close) while the live quote is
bid=2296.0 / ask=2297.0 — price drifted $3 since the bar close.

  - BEFORE fix: anchor = 2300.0 + spread/2 → SL ≈ 2260.6 → assertion
    against the ask-anchored expectation (2257.5) FAILS (RED)
  - AFTER fix:  anchor = ask = 2297.0 → SL = 2257.50 → GREEN

Control (guards against false-positive RED and Real-A safety):
  - quote unavailable (None) → legacy spread/2 estimate must be preserved,
    so a bridge hiccup degrades to current behavior, never to a crash or
    to a tighter-than-intended SL.

References
----------
- ISSUE-046: SL/TP anchored to signal.price (stale M5 close), not bid/ask
- ISSUE-053: the existing spread/2 estimate this fix builds on
- ISSUE-054: DB row is already fill-anchored — only the ORDER args lag
- Production: metty/execution/live_trader.py live path (est_fill_price block)
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from broky.indicators.atr import calculate_atr  # noqa: E402
from broky.risk.position_sizing import (  # noqa: E402
    calculate_stop_loss,
    calculate_take_profit,
)
from shared.models import Signal, SignalType, TradingMode  # noqa: E402

# The causal scenario: stale M5 close vs the live quote 3 dollars away.
STALE_SIGNAL_PRICE = 2300.0
LIVE_BID = 2296.0
LIVE_ASK = 2297.0
LIVE_SPREAD_POINTS = 20.0  # points → $0.20 price (XAUUSD point = 0.01)


class _FakeBridge:
    """Captures send_order args; fills at the live quote like MT5 does."""

    instances: list["_FakeBridge"] = []

    def __init__(self, config):
        self.config = config
        self.sent: dict | None = None
        _FakeBridge.instances.append(self)

    async def connect(self):
        return True

    async def disconnect(self):
        return True

    async def send_order(self, symbol, direction, lots, sl, tp):
        self.sent = {
            "symbol": symbol, "direction": direction,
            "lots": lots, "sl": sl, "tp": tp,
        }
        fill = LIVE_ASK if direction == "BUY" else LIVE_BID
        return SimpleNamespace(
            success=True, ticket=12345, price=fill, volume=lots,
            error=None,
        )


def _make_m5(n_bars: int = 50) -> pd.DataFrame:
    """Flat OHLC on a UTC index — deterministic ATR (TR = high-low = 15)."""
    idx = pd.date_range("2026-01-01 00:00", periods=n_bars, freq="5min", tz="UTC")
    return pd.DataFrame(
        {"open": 2300.0, "high": 2310.0, "low": 2295.0, "close": 2305.0, "volume": 100},
        index=idx,
    )


def _make_signal(signal_type: SignalType) -> Signal:
    return Signal(
        symbol="XAUUSD",
        signal_type=signal_type,
        confidence=0.70,
        price=STALE_SIGNAL_PRICE,
        timestamp=pd.Timestamp("2026-01-01 04:00", tz="UTC").to_pydatetime(),
        timeframe="M5",
        indicators={},
        regime="trending",
        trading_mode=TradingMode.SWING,
        strategy_id="mr-bet-B",
    )


def _make_live_trader(tmp_path, monkeypatch) -> "MagicMock":
    """LiveTrader(B) on a real-schema temp DB, driven into the live path."""
    from metty.core.db import init_db
    from metty.execution.live_trader import LiveTrader

    db_path = str(tmp_path / "live.db")
    init_db(db_path)
    # Seed the accounts row insert_live_trade's FK points at (account_id=2).
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO accounts (id, name, broker_login, broker_server, "
        "balance, leverage, bridge_port, signal_group) "
        "VALUES (2, 'demo-b', '1', 'Exness-MT5Trial15', 10000, 100, 8001, 'B')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("MT5_BRIDGE_B_HOST", "localhost")
    monkeypatch.setenv("MT5_BRIDGE_B_PORT", "8001")
    monkeypatch.setenv("MT5_LOGIN_B", "1")
    monkeypatch.setenv("MT5_PASSWORD_B", "x")
    monkeypatch.setenv("MT5_SERVER_B", "Exness-MT5Trial15")
    t = LiveTrader(account="B", dry_run=True, db_path=db_path)
    t.dry_run = False  # route to the live execution path under test
    return t


def _install_live_happy_path(t, monkeypatch, signal: Signal, quote) -> dict:
    """Patch every collaborator so run_once reaches send_order.

    `quote` is what _get_current_quote() returns: (bid, ask) or None.
    Patched with raising=False so the suite runs BEFORE the fix adds the
    method (pre-fix it is simply never called → proves the RED is causal).
    """
    candles = {"M5": _make_m5()}

    monkeypatch.setattr(t, "_fetch_candles", lambda: candles)
    monkeypatch.setattr(t, "_monitor_positions", lambda *a, **kw: [])
    monkeypatch.setattr(t, "_generate_signal", lambda *a, **kw: signal)
    monkeypatch.setattr(t, "_get_equity", lambda: 10000.0)
    monkeypatch.setattr(t, "_get_free_margin", lambda: 10000.0)
    monkeypatch.setattr(t, "_get_current_spread", lambda: LIVE_SPREAD_POINTS)
    monkeypatch.setattr(t, "_get_current_quote", lambda: quote, raising=False)
    monkeypatch.setattr(t, "_get_calendar_context", lambda: (None, None, None))
    monkeypatch.setattr(t, "_check_cooldown", lambda: False)
    monkeypatch.setattr(t, "_check_existing_position", lambda: False)
    monkeypatch.setattr(t, "_ml_enabled", lambda: False)
    monkeypatch.setattr(t, "_record_rejection", lambda *a, **kw: None)

    dd = MagicMock()
    dd.check.return_value = (True, "ok")
    dd.state.daily_trades = 0
    dd.state.weekly_trades = 0
    monkeypatch.setattr(t, "_drawdown_protector", dd)

    cb = MagicMock()
    cb.can_open_trade.return_value = (True, "ok")
    monkeypatch.setattr(t, "circuit_breaker", cb)

    monkeypatch.setattr("metty.execution.live_trader.get_open_trades", lambda *a, **kw: [])
    monkeypatch.setattr("metty.execution.live_trader.should_avoid_trading", lambda *a, **kw: False)
    monkeypatch.setattr("metty.execution.live_trader.get_bridge_config", lambda acct: MagicMock())

    _FakeBridge.instances = []
    monkeypatch.setattr("metty.bridge.client.MT5Bridge", _FakeBridge)
    return {"dd": dd}


def _expected_anchor_sl_tp(t, direction: str, anchor: float, atr: float) -> tuple[float, float]:
    """SL/TP the production formulas yield when anchored at `anchor`."""
    sl = calculate_stop_loss(anchor, atr, direction, t.risk.atr_multiplier, t.risk.spread_buffer)
    tp = calculate_take_profit(anchor, sl, direction, t.risk.risk_reward_ratio)
    return sl, tp


def _atr_of_candles() -> float:
    m5 = _make_m5()
    return float(calculate_atr(m5["high"], m5["low"], m5["close"], period=14).iloc[-1])


class TestOrderSlTpAnchoredToLiveQuote:
    """ISSUE-046: the ORDER's SL/TP must be measured from the fill side."""

    def test_buy_sltp_anchored_to_live_ask(self, tmp_path, monkeypatch):
        """BUY: SL/TP sent with the order must be measured from ASK.

        Pre-fix anchor = stale signal.price ± spread/2 = 2300.10 →
        SL ≈ 2260.6 ≠ ask-anchored 2257.50 → RED.
        """
        t = _make_live_trader(tmp_path, monkeypatch)
        _install_live_happy_path(t, monkeypatch, _make_signal(SignalType.BUY), (LIVE_BID, LIVE_ASK))

        result = t.run_once()

        assert result.get("action") not in ("hold", "skip"), (
            f"happy path must reach execution, got: {result}"
        )
        assert _FakeBridge.instances, "MT5Bridge must have been constructed"
        sent = _FakeBridge.instances[-1].sent
        assert sent, "send_order must have been called"

        atr = _atr_of_candles()
        exp_sl, exp_tp = _expected_anchor_sl_tp(t, "BUY", LIVE_ASK, atr)
        assert sent["sl"] == pytest.approx(exp_sl, abs=0.01), (
            f"ISSUE-046 unfixed: BUY SL anchored at stale signal.price-based estimate "
            f"(got {sent['sl']}, expected ask-anchored {exp_sl:.2f} — ask={LIVE_ASK}, "
            f"signal.price={STALE_SIGNAL_PRICE})"
        )
        assert sent["tp"] == pytest.approx(exp_tp, abs=0.01), (
            f"BUY TP must be ask-anchored too (got {sent['tp']}, expected {exp_tp:.2f})"
        )

    def test_sell_sltp_anchored_to_live_bid(self, tmp_path, monkeypatch):
        """SELL: SL/TP sent with the order must be measured from BID."""
        t = _make_live_trader(tmp_path, monkeypatch)
        _install_live_happy_path(t, monkeypatch, _make_signal(SignalType.SELL), (LIVE_BID, LIVE_ASK))

        result = t.run_once()

        assert result.get("action") not in ("hold", "skip"), (
            f"happy path must reach execution, got: {result}"
        )
        sent = _FakeBridge.instances[-1].sent
        assert sent, "send_order must have been called"

        atr = _atr_of_candles()
        exp_sl, exp_tp = _expected_anchor_sl_tp(t, "SELL", LIVE_BID, atr)
        assert sent["sl"] == pytest.approx(exp_sl, abs=0.01), (
            f"ISSUE-046 unfixed: SELL SL anchored at stale signal.price-based estimate "
            f"(got {sent['sl']}, expected bid-anchored {exp_sl:.2f} — bid={LIVE_BID}, "
            f"signal.price={STALE_SIGNAL_PRICE})"
        )
        assert sent["tp"] == pytest.approx(exp_tp, abs=0.01), (
            f"SELL TP must be bid-anchored too (got {sent['tp']}, expected {exp_tp:.2f})"
        )

    def test_quote_unavailable_falls_back_to_spread_estimate(self, tmp_path, monkeypatch):
        """Control: quote fetch fails → legacy spread/2 estimate preserved.

        A bridge hiccup must degrade to CURRENT behavior (ISSUE-053
        estimate), never crash the cycle or tighten SL. This also protects
        Real-A: the fallback is the exact pre-fix anchor.
        """
        t = _make_live_trader(tmp_path, monkeypatch)
        _install_live_happy_path(t, monkeypatch, _make_signal(SignalType.BUY), None)

        result = t.run_once()

        assert result.get("action") not in ("hold", "skip"), (
            f"fallback path must still execute the order, got: {result}"
        )
        sent = _FakeBridge.instances[-1].sent
        assert sent, "send_order must have been called"

        atr = _atr_of_candles()
        est_fill = STALE_SIGNAL_PRICE + (LIVE_SPREAD_POINTS * 0.01) / 2.0
        exp_sl, exp_tp = _expected_anchor_sl_tp(t, "BUY", est_fill, atr)
        assert sent["sl"] == pytest.approx(exp_sl, abs=0.01), (
            f"quote unavailable must fall back to the spread/2 estimate "
            f"(got {sent['sl']}, expected {exp_sl:.2f})"
        )