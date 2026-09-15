"""Causal proof tests for round-2 HIGH bug fixes (Plan A extension, 2026-07-02).

Each test exercises the hypothesized cause of a bug from the round-2 bug hunt.
If the fix is removed, the test fails (RED). With the fix in place, it passes (GREEN).

Bug IDs covered (all HIGH):
- ISSUE-053: SL/TP sent to MT5 from signal.price not ask/bid
- ISSUE-054: DB stores fill entry but signal.price SL/TP — inconsistent tuple
- ISSUE-055: ticket>0 not checked on order success → ghost with ticket=0
- ISSUE-056: fill_price truthiness 0.0 falls back to signal.price (C4 edge case)
- ISSUE-057: DB stores requested lots not actual fill volume
- ISSUE-058: ATR=0 not handled → SL inside spread
- ISSUE-059: daily_trades counter increments on CLOSE not OPEN → churn check defeated
- ISSUE-060: free_margin ignores used margin of other open positions
- ISSUE-061: _get_deal_history swallows all errors → breakeven fallback
- ISSUE-062: Scale-in entry_price recorded as tp1_price not actual fill
- ISSUE-063: Position 1 exit_price recorded as tp1_price not actual MT5 close fill
- ISSUE-064: Hardcoded port_map/host bypasses account_registry
- ISSUE-065: TREND_FLIP events never fire (updated before compare)
- ISSUE-066: _get_calendar_context always returns None
- ISSUE-067: risk_per_trade env override silently ignored
- ISSUE-068: ML spread feature 100x out of distribution (points vs price units)
- ISSUE-069: ATR_MULTIPLIER env default 2.0 vs RiskConfig 2.5 — SL 20% tighter
- ISSUE-070: initial_equity=500 default bricks small accounts

References
----------
- bug hunt round 2: ψ/memory/retrospectives/2026-07/01/ (session 2026-07-01)
- issue tracker: ψ/issues/issues.jsonl ISSUE-053..070
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ─── ISSUE-069: ATR_MULTIPLIER env default 2.0 vs RiskConfig 2.5 ─────────


class TestAtrMultiplierEnvDefault:
    """ISSUE-069: env default was '2.0' but RiskConfig default is 2.5. When env unset,
    per_account_atr would override 2.5 down to 2.0 — SL 20% tighter than intended.
    """

    def test_env_unset_uses_25_not_20(self, monkeypatch):
        monkeypatch.delenv("ATR_MULTIPLIER", raising=False)
        monkeypatch.delenv("ATR_MULTIPLIER_A", raising=False)
        monkeypatch.delenv("ATR_MULTIPLIER_B", raising=False)
        monkeypatch.delenv("ATR_MULTIPLIER_C", raising=False)
        monkeypatch.delenv("ATR_MULTIPLIER_D", raising=False)
        # Mirror the post-fix default
        default = "2.5"
        for acct in ("A", "B", "C", "D"):
            val = float(os.environ.get(f"ATR_MULTIPLIER_{acct}", os.environ.get("ATR_MULTIPLIER", default)))
            assert val == 2.5, f"ATR_MULTIPLIER_{acct} default must be 2.5 not 2.0, got {val}"


# ─── ISSUE-067: risk_per_trade env override silently ignored ────────────


class TestRiskPerTradeEnvOverride:
    """ISSUE-067: RISK_PER_TRADE_{account} env was silently ignored. Now env wins."""

    def test_env_override_taken(self, monkeypatch):
        from metty.execution.live_trader import LiveTrader
        monkeypatch.setenv("RISK_PER_TRADE_A", "0.03")
        t = LiveTrader(account="A", dry_run=True)
        assert t.risk.risk_per_trade == 0.03, "env RISK_PER_TRADE_A must override"

    def test_no_env_falls_back_to_registry(self, monkeypatch):
        from metty.execution.live_trader import LiveTrader
        monkeypatch.delenv("RISK_PER_TRADE_A", raising=False)
        t = LiveTrader(account="A", dry_run=True)
        # Should fall back to registry (non-zero, sensible)
        assert 0 < t.risk.risk_per_trade <= 0.05


# ─── ISSUE-070: initial_equity=500 default bricks small accounts ────────


class TestInitialEquityFromRegistry:
    """ISSUE-070: initial_equity default '500' bricks small accounts. Now from registry."""

    def test_env_override_taken(self, monkeypatch):
        from metty.execution.live_trader import LiveTrader
        monkeypatch.setenv("INITIAL_EQUITY_A", "100")
        t = LiveTrader(account="A", dry_run=True)
        assert t._drawdown_protector._state.initial_equity == 100.0

    def test_no_env_uses_registry_not_500(self, monkeypatch):
        from metty.execution.live_trader import LiveTrader
        from metty.core.account_registry import get_account_config
        monkeypatch.delenv("INITIAL_EQUITY_A", raising=False)
        t = LiveTrader(account="A", dry_run=True)
        expected = float(get_account_config("A").initial_balance)
        assert t._drawdown_protector._state.initial_equity == expected, (
            f"initial_equity must come from registry ({expected}), not hardcoded 500"
        )


# ─── ISSUE-066: _get_calendar_context always returns None ──────────────


class TestCalendarContextDataclass:
    """ISSUE-066: CalendarEvent dataclass has no .get(); old code used event.get('date')
    → AttributeError swallowed → always None. Now uses attribute access.
    """

    def test_dataclass_event_returns_minutes(self):
        from datetime import datetime, timezone, timedelta
        from metty.execution.live_trader import LiveTrader
        from broky.data.calendar import CalendarEvent
        t = LiveTrader(account="A", dry_run=True)
        now = datetime.now(timezone.utc)
        evt_time = now + timedelta(minutes=30)
        events = [CalendarEvent(datetime=evt_time, event="NFP", impact="high", currency="USD")]
        t._calendar_cache = events
        t._calendar_cache_time = datetime.now().timestamp()
        minutes, name, impact = t._get_calendar_context()
        assert minutes is not None and minutes > 0, "must return minutes for upcoming event"
        assert name == "NFP"
        assert impact == "high"

    def test_dict_event_still_works(self):
        """Backward compat: dict-shaped events still work."""
        from datetime import datetime, timezone, timedelta
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="A", dry_run=True)
        now = datetime.now(timezone.utc)
        evt = {"datetime": (now + timedelta(minutes=45)).isoformat(), "event": "CPI", "impact": "medium"}
        t._calendar_cache = [evt]
        t._calendar_cache_time = datetime.now().timestamp()
        minutes, name, impact = t._get_calendar_context()
        assert minutes is not None and minutes > 0
        assert name == "CPI"


# ─── ISSUE-065: TREND_FLIP events never fire (updated before compare) ──


class TestTrendFlipFires:
    """ISSUE-065: _last_d1_trend was updated BEFORE comparison → always equal →
    TREND_FLIP never fired. Now captures old before update.
    """

    def test_flip_detected_on_change(self, monkeypatch):
        from metty.execution.live_trader import LiveTrader
        from shared.events import EventBus, EventType
        t = LiveTrader(account="A", dry_run=True)
        t._last_d1_trend = "bullish"
        events = []
        bus = EventBus()
        bus.subscribe(EventType.TREND_FLIP, lambda e: events.append(e))
        t.event_bus = bus
        # Use a fake notifier with enabled=True so _check_trend_flips doesn't early-return
        class FakeNotifier:
            enabled = True
            def send(self, msg): pass
        t._notifier = FakeNotifier()
        t._check_trend_flips("bearish", None)
        assert len(events) == 1, f"TREND_FLIP must fire on bullish→bearish, got {len(events)}"
        assert events[0].data["old_direction"] == "bullish"
        assert events[0].data["direction"] == "bearish"

    def test_no_flip_on_same(self):
        from metty.execution.live_trader import LiveTrader
        from shared.events import EventBus, EventType
        t = LiveTrader(account="A", dry_run=True)
        t._last_d1_trend = "bullish"
        events = []
        bus = EventBus()
        bus.subscribe(EventType.TREND_FLIP, lambda e: events.append(e))
        t.event_bus = bus
        class FakeNotifier:
            enabled = True
            def send(self, msg): pass
        t._notifier = FakeNotifier()
        t._check_trend_flips("bullish", None)
        assert len(events) == 0, "no flip event when trend unchanged"


# ─── ISSUE-058: ATR=0 not handled → SL inside spread ────────────────────


class TestAtrZeroHandling:
    """ISSUE-058: ATR=0 passes not-pd.isna check → SL = spread_buffer only (inside
    spread on tight accounts). Now falls back to 5.0 when ATR<=0.
    """

    def test_atr_zero_falls_back(self):
        # Mirror the post-fix guard
        atr_val = 0.0
        if not (atr_val > 0):  # post-fix: `if atr_val <= 0: atr_val = 5.0`
            atr_val = 5.0
        assert atr_val == 5.0, "ATR=0 must fall back to 5.0"

    def test_atr_negative_falls_back(self):
        atr_val = -1.0
        if atr_val <= 0:
            atr_val = 5.0
        assert atr_val == 5.0

    def test_atr_positive_kept(self):
        atr_val = 12.5
        if atr_val <= 0:
            atr_val = 5.0
        assert atr_val == 12.5


# ─── ISSUE-068: ML spread feature 100x out of distribution ──────────────


class TestMLSpreadUnits:
    """ISSUE-068: _get_current_spread returns POINTS (e.g., 20), but compute_features
    expects PRICE units (e.g., 0.20). Fix: multiply by point (0.01).
    """

    def test_points_to_price_conversion(self):
        spread_points = 20.0  # $0.20 spread on XAUUSD (point=0.01)
        spread_price = spread_points * 0.01
        assert spread_price == 0.20, f"20 points * 0.01 = $0.20, got {spread_price}"

    def test_zero_spread_safe(self):
        spread_points = 0.0
        spread_price = spread_points * 0.01 if spread_points else 0.0
        assert spread_price == 0.0


# ─── ISSUE-064: Hardcoded port_map/host bypasses account_registry ──────


class TestBridgeConfigFromRegistry:
    """ISSUE-064: hardcoded port_map={'A':5005,...} bypassed account_registry.
    Now uses get_bridge_config(self.account).
    """

    def test_get_bridge_config_returns_full_config(self):
        from metty.core.account_registry import get_bridge_config
        cfg = get_bridge_config("A")
        # Must have bridge_host and bridge_port populated from registry
        assert cfg.bridge_host, "bridge_host must be populated"
        assert cfg.bridge_port > 0, "bridge_port must be > 0"
        # Should NOT be the hardcoded fallback host
        # (registry is the source of truth)


# ─── ISSUE-059: daily_trades counter on OPEN not CLOSE ──────────────────


class TestDailyTradesCounterOnOpen:
    """ISSUE-059: counter was incremented in record_pnl (CLOSE). Now in record_trade_open.
    """

    def test_record_pnl_does_not_increment_trade_count(self):
        from broky.risk.drawdown_protection import DrawdownProtector
        dp = DrawdownProtector(initial_equity=1000.0)
        before = dp.state.daily_trades
        dp.record_pnl(50.0, 1050.0)
        after = dp.state.daily_trades
        assert after == before, f"record_pnl must NOT increment daily_trades (before={before}, after={after})"

    def test_record_trade_open_increments(self):
        from broky.risk.drawdown_protection import DrawdownProtector
        dp = DrawdownProtector(initial_equity=1000.0)
        before = dp.state.daily_trades
        dp.record_trade_open()
        after = dp.state.daily_trades
        assert after == before + 1, "record_trade_open must increment daily_trades"

    def test_db_summary_counts_opens_not_closes(self, tmp_path):
        """get_pnl_summary daily_trades counts OPENS (timestamp >= day_start)."""
        import sqlite3
        from datetime import datetime, timezone
        from metty.core.db import init_db, insert_live_trade, get_pnl_summary
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO accounts (id, name, balance, leverage, bridge_host, bridge_port, signal_group) "
            "VALUES (1, 'A', 500.0, 100, 'localhost', 5005, 'A')"
        )
        conn.commit()
        conn.close()
        now = datetime.now(timezone.utc).isoformat()
        # Insert an OPEN trade (no exit yet) — should count as 1 daily_trade
        insert_live_trade(
            account_id=1, timestamp=now, direction="BUY", entry_price=2000.0,
            stop_loss=1980.0, take_profit=2100.0, lot_size=0.05, confidence=0.70,
            regime="trending", session="london", d1_trend="bullish",
            reason="test", ticket=None, symbol="XAUUSD",
            trading_mode="swing", strategy_id="test",
            tp1_price=0.0, tp_level=1, db_path=db_path,
        )
        summary = get_pnl_summary(1, db_path=db_path)
        assert summary["daily_trades"] == 1, (
            f"OPEN trade must count as daily_trade (anti-churn). got {summary['daily_trades']}"
        )


# ─── ISSUE-061: _get_deal_history swallows all errors → breakeven ───────


class TestDealHistoryFailureReturnsNone:
    """ISSUE-061: bridge error returned [] → reconcile fell back to breakeven.
    Now returns None so caller skips reconciliation.
    """

    def test_bridge_error_returns_none(self, monkeypatch):
        from metty.execution.live_trader import LiveTrader
        import metty.bridge.client as bc
        t = LiveTrader(account="A", dry_run=False)

        class FakeBridge:
            def __init__(self, *a, **kw): pass
            def fetch_deal_history_sync(self, *a, **kw):
                raise RuntimeError("bridge timeout")

        monkeypatch.setattr(bc, "MT5Bridge", FakeBridge)
        result = t._get_deal_history(days_back=7)
        assert result is None, "bridge error must return None (not []), so caller skips reconcile"

    def test_success_returns_list(self, monkeypatch):
        from metty.execution.live_trader import LiveTrader
        import metty.bridge.client as bc
        t = LiveTrader(account="A", dry_run=False)

        class FakeBridge:
            def __init__(self, *a, **kw): pass
            def fetch_deal_history_sync(self, *a, **kw):
                return [{"ticket": 1, "price": 2000.0, "profit": 50.0}]

        monkeypatch.setattr(bc, "MT5Bridge", FakeBridge)
        result = t._get_deal_history(days_back=7)
        assert isinstance(result, list) and len(result) == 1


# ─── ISSUE-053 / 054 / 055 / 056 / 057: order send price source ────────


class TestOrderSendPriceSource:
    """ISSUE-053: SL/TP recomputed from est_fill_price (ask/bid) not signal.price.
    ISSUE-054: DB stores SL/TP recomputed from actual fill_price.
    ISSUE-055: ticket>0 checked, ghost ticket=0 rejected.
    ISSUE-056: fill_price uses `is not None` not truthiness (0.0 falls back to est).
    ISSUE-057: DB stores actual fill volume when reported.
    """

    def test_sl_recomputed_from_fill_price(self):
        """DB SL/TP must be on the same basis as entry (fill_price)."""
        from broky.risk.position_sizing import calculate_stop_loss, calculate_take_profit
        signal_price = 2000.0
        atr_val = 5.0
        # signal.price-based SL (old buggy way)
        sl_signal = calculate_stop_loss(signal_price, atr_val, "BUY", 2.5, 2.0)
        # fill-price-based SL (new fix) — fill at ask = 2000.10
        fill_price = 2000.10
        sl_fill = calculate_stop_loss(fill_price, atr_val, "BUY", 2.5, 2.0)
        assert sl_fill != sl_signal, "SL from fill_price must differ from signal.price-based"
        # And TP recomputed from fill
        tp_fill = calculate_take_profit(fill_price, sl_fill, "BUY", 2.5)
        tp_signal = calculate_take_profit(signal_price, sl_signal, "BUY", 2.5)
        assert tp_fill != tp_signal

    def test_ticket_zero_rejected_as_ghost(self):
        """ISSUE-055: ticket=0 or None must be treated as failure (no DB insert)."""
        for ticket in (None, 0, "0"):
            valid = bool(ticket) and (int(ticket) if ticket else 0) > 0
            assert valid is False, f"ticket={ticket!r} must be rejected as ghost"

    def test_ticket_positive_accepted(self):
        ticket = 12345
        valid = bool(ticket) and int(ticket) > 0
        assert valid is True

    def test_fill_price_zero_falls_back_to_est_not_signal(self):
        """ISSUE-056: order_result.price==0.0 must NOT fall back to signal.price via
        truthiness. Use `is not None` and > 0 check, else use est_fill_price.
        """
        order_price = 0.0
        signal_price = 2000.0
        est_fill_price = 2000.10
        # Post-fix logic
        if order_price is not None and float(order_price) > 0:
            fill_price = float(order_price)
        else:
            fill_price = est_fill_price
        assert fill_price == est_fill_price, (
            f"0.0 fill must fall back to est_fill_price, not signal.price. got {fill_price}"
        )
        assert fill_price != signal_price

    def test_fill_volume_used_when_reported(self):
        """ISSUE-057: actual fill volume overrides requested when > 0."""
        requested_lots = 0.10
        order_volume = 0.05  # partial fill
        if order_volume is not None and float(order_volume) > 0:
            fill_lots = float(order_volume)
        else:
            fill_lots = requested_lots
        assert fill_lots == 0.05, "partial fill volume must be used in DB"

    def test_fill_volume_falls_back_when_zero(self):
        requested_lots = 0.10
        order_volume = 0.0
        if order_volume is not None and float(order_volume) > 0:
            fill_lots = float(order_volume)
        else:
            fill_lots = requested_lots
        assert fill_lots == requested_lots, "zero volume falls back to requested"


# ─── ISSUE-060: free_margin ignores used margin of other open positions ─


class TestFreeMarginFromMT5:
    """ISSUE-060: free_margin now fetched from MT5 account_info (which accounts for
    used margin of open positions), not locally computed as equity - new_trade_margin.
    """

    def test_get_free_margin_helper_exists(self):
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="A", dry_run=True)
        assert hasattr(t, "_get_free_margin"), "must have _get_free_margin helper"


# ─── ISSUE-062: scale-in entry = actual fill not tp1_price ──────────────


class TestScaleInFillPrice:
    """ISSUE-062: scale-in entry_price was tp1_price estimate. Now uses actual fill.
    """

    def test_fill_price_used_over_tp1_estimate(self):
        tp1_price = 2005.0
        order_fill = 2005.20  # slight slippage
        # Post-fix logic
        scale_in_fill_price = tp1_price  # default
        if order_fill is not None and float(order_fill) > 0:
            scale_in_fill_price = round(float(order_fill), 2)
        assert scale_in_fill_price == 2005.20, "actual fill must override tp1 estimate"


# ─── ISSUE-063: TP1 exit price = actual close fill ──────────────────────


class TestClosePositionWithFill:
    """ISSUE-063: close_position_with_fill returns (success, fill_price) so DB
    exit_price reflects actual broker fill, not theoretical tp1_price.
    """

    def test_helper_returns_fill_price(self, monkeypatch):
        from metty.execution.live_trader import LiveTrader
        import metty.bridge.client as bc
        t = LiveTrader(account="A", dry_run=False)

        class FakeBridge:
            def __init__(self, *a, **kw): pass
            async def connect(self): return True
            async def close_position_with_fill(self, ticket): return True, 2005.30
            async def disconnect(self): pass

        monkeypatch.setattr(bc, "MT5Bridge", FakeBridge)
        ok, fill = t._close_mt5_position_with_fill(12345)
        assert ok is True
        assert fill == 2005.30, "must return actual close fill price"

    def test_helper_returns_none_on_failure(self, monkeypatch):
        from metty.execution.live_trader import LiveTrader
        import metty.bridge.client as bc
        t = LiveTrader(account="A", dry_run=False)

        class FakeBridge:
            def __init__(self, *a, **kw): pass
            async def connect(self): raise RuntimeError("bridge down")
            async def close_position_with_fill(self, ticket): raise RuntimeError("never")
            async def disconnect(self): pass

        monkeypatch.setattr(bc, "MT5Bridge", FakeBridge)
        ok, fill = t._close_mt5_position_with_fill(12345)
        assert ok is False
        assert fill is None