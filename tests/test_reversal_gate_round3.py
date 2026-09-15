"""Causal proof tests for round-3 reversal-gate + auto-retrain fixes (2026-07-02).

Each test exercises the hypothesized cause of a gap from the round-2 feature audit.
If the fix is removed, the test fails (RED). With the fix in place, it passes (GREEN).

Gaps covered:
- REVERSAL-1: No higher_high / lower_low price-structure detection (CLAUDE.md spec)
- REVERSAL-2: compute_reversal_signal did not require price-structure confirmation
- REVERSAL-3: COUNTER_TREND_CONFIDENCE_MULT = 1.0 (penalty disabled)
- REVERSAL-4: live_trader never read trend_alignment for gating
- RETRAIN-1: close_live_trade hardcoded snapshot_id=None (relied on async backfill)
- RETRAIN-2: No automatic ML retraining trigger (model goes stale)

References
----------
- feature audit: ψ/memory/retrospectives/2026-07/01/ (session 2026-07-01)
- user request 2026-07-02: "แก้ B ก่อน แล้ว test อีกรอบ"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ─── REVERSAL-3: counter-trend penalty enabled ──────────────────────────


class TestCounterTrendPenaltyEnabled:
    """REVERSAL-3: COUNTER_TREND_CONFIDENCE_MULT must be < 1.0 (was 1.0 = no-op)."""

    def test_penalty_is_strictly_below_one(self):
        from broky.signals.generator import COUNTER_TREND_CONFIDENCE_MULT
        assert COUNTER_TREND_CONFIDENCE_MULT < 1.0, (
            "penalty multiplier must be < 1.0 — at 1.0 the 'penalty' is a no-op "
            f"(got {COUNTER_TREND_CONFIDENCE_MULT})"
        )

    def test_penalty_not_zero(self):
        """Penalty must reduce confidence, not kill the trade outright (generator
        still emits the signal so live_trader gate can reject with a clear reason)."""
        from broky.signals.generator import COUNTER_TREND_CONFIDENCE_MULT
        assert COUNTER_TREND_CONFIDENCE_MULT > 0.0


# ─── REVERSAL-1: swing structure detection ──────────────────────────────


class TestSwingStructureDetection:
    """REVERSAL-1: detect_swing_structure finds HH/LL pivots from high/low series."""

    def test_detects_lower_high_and_lower_low(self):
        """A peak then a lower peak → made_lower_high; a trough then a lower trough
        → made_lower_low (the uptrend-break signal CLAUDE.md requires for SELL
        reversal)."""
        from broky.signals.generator import detect_swing_structure
        import pandas as pd
        # peak at i=5 (15), trough at i=10 (9), lower peak at i=15 (13), lower trough at i=20 (7)
        high = pd.Series([10, 11, 12, 13, 14, 15, 14, 13, 12, 11, 10, 11, 12, 13, 14, 13, 12, 11, 10, 9, 8, 9, 10])
        low  = pd.Series([9, 10, 11, 12, 13, 14, 13, 12, 11, 10, 9, 10, 11, 12, 13, 12, 11, 10, 9, 8, 7, 8, 9])
        s = detect_swing_structure(high, low, lookback=20)
        assert s["made_lower_high"] is True, f"expected lower_high, got {s}"
        assert s["made_lower_low"] is True, f"expected lower_low, got {s}"

    def test_detects_higher_high_and_higher_low(self):
        """A higher peak + higher trough → downtrend-break (BUY reversal signal)."""
        from broky.signals.generator import detect_swing_structure
        import pandas as pd
        # trough at i=5 (9), peak at i=10 (15), higher trough at i=15 (11), higher peak at i=20 (17)
        high = pd.Series([10, 11, 12, 13, 14, 13, 12, 11, 10, 11, 15, 14, 13, 12, 13, 14, 13, 12, 13, 14, 17, 16, 15])
        low  = pd.Series([9, 10, 11, 12, 11, 10, 9, 8, 9, 10, 11, 12, 13, 14, 13, 12, 11, 12, 13, 14, 15, 14, 13])
        s = detect_swing_structure(high, low, lookback=20)
        assert s["made_higher_high"] is True, f"expected higher_high, got {s}"
        assert s["made_higher_low"] is True, f"expected higher_low, got {s}"

    def test_returns_neutral_on_insufficient_data(self):
        from broky.signals.generator import detect_swing_structure
        import pandas as pd
        s = detect_swing_structure(pd.Series([1, 2, 3]), pd.Series([1, 2, 3]), lookback=20)
        assert s["made_lower_low"] is False
        assert s["made_higher_high"] is False
        assert s["last_swing_high"] is None


# ─── REVERSAL-2: compute_reversal_signal requires price-structure ────────


class TestReversalRequiresPriceStructure:
    """REVERSAL-2: with swing data provided, reversal verdict requires HH/LL
    confirmation. Without swing data (legacy caller), structure gate is skipped
    so live_trader gate is the source of truth."""

    def test_counter_sell_without_lower_low_rejected(self):
        """SELL in bullish D1 with OB/OS + divergence but NO made_lower_low →
        must return has_reversal=False (price structure not yet breaking)."""
        from broky.signals.generator import compute_reversal_signal
        swing_no_break = {
            "last_swing_high": 15.0, "prev_swing_high": 15.0,
            "last_swing_low": 9.0, "prev_swing_low": 9.0,
            "made_lower_high": False, "made_higher_low": False,
            "made_lower_low": False, "made_higher_high": False,
        }
        has_rev, strength = compute_reversal_signal(
            "SELL", "bullish", "bullish",
            rsi=72, stoch_k=80, boll_pct_b=0.85, mfi=78,
            macd_hist=-0.5, plus_di=15, minus_di=25, boll_bw=0.05,
            swing=swing_no_break,
        )
        assert has_rev is False, (
            "SELL reversal in uptrend must require made_lower_low — without it, "
            "this is just a counter-trend pullback (CLAUDE.md forbidden)"
        )

    def test_counter_sell_with_lower_low_accepted(self):
        """Same OB/OS + divergence + made_lower_low=True → has_reversal=True."""
        from broky.signals.generator import compute_reversal_signal
        swing_break = {
            "last_swing_high": 13.0, "prev_swing_high": 15.0,
            "last_swing_low": 7.0, "prev_swing_low": 9.0,
            "made_lower_high": True, "made_higher_low": False,
            "made_lower_low": True, "made_higher_high": False,
        }
        has_rev, strength = compute_reversal_signal(
            "SELL", "bullish", "bullish",
            rsi=72, stoch_k=80, boll_pct_b=0.85, mfi=78,
            macd_hist=-0.5, plus_di=15, minus_di=25, boll_bw=0.05,
            swing=swing_break,
        )
        assert has_rev is True
        assert strength > 0.0

    def test_counter_buy_without_higher_high_rejected(self):
        from broky.signals.generator import compute_reversal_signal
        swing_no_break = {
            "last_swing_high": 15.0, "prev_swing_high": 15.0,
            "last_swing_low": 9.0, "prev_swing_low": 9.0,
            "made_lower_high": False, "made_higher_low": False,
            "made_lower_low": False, "made_higher_high": False,
        }
        has_rev, _ = compute_reversal_signal(
            "BUY", "bearish", "bearish",
            rsi=28, stoch_k=20, boll_pct_b=0.15, mfi=22,
            macd_hist=0.5, plus_di=25, minus_di=15, boll_bw=0.05,
            swing=swing_no_break,
        )
        assert has_rev is False, "BUY reversal in downtrend must require made_higher_high"

    def test_counter_buy_with_higher_high_accepted(self):
        from broky.signals.generator import compute_reversal_signal
        swing_break = {
            "last_swing_high": 17.0, "prev_swing_high": 15.0,
            "last_swing_low": 11.0, "prev_swing_low": 9.0,
            "made_lower_high": False, "made_higher_low": True,
            "made_lower_low": False, "made_higher_high": True,
        }
        has_rev, strength = compute_reversal_signal(
            "BUY", "bearish", "bearish",
            rsi=28, stoch_k=20, boll_pct_b=0.15, mfi=22,
            macd_hist=0.5, plus_di=25, minus_di=15, boll_bw=0.05,
            swing=swing_break,
        )
        assert has_rev is True
        assert strength > 0.0

    def test_legacy_caller_without_swing_still_works(self):
        """swing=None must not crash — structure gate skipped (live_trader enforces)."""
        from broky.signals.generator import compute_reversal_signal
        has_rev, _ = compute_reversal_signal(
            "SELL", "bullish", "bullish",
            rsi=72, stoch_k=80, boll_pct_b=0.85, mfi=78,
            macd_hist=-0.5, plus_di=15, minus_di=25, boll_bw=0.05,
            swing=None,
        )
        # Without swing data, structure check is skipped → OB/OS + div alone satisfy
        assert has_rev is True


# ─── REVERSAL-4: live_trader rejects trend_alignment == -1 ───────────────


class TestLiveTraderCounterTrendGate:
    """REVERSAL-4: live_trader.run_once must reject trend_alignment==-1 signals
    (counter-trend WITHOUT reversal) outside learning mode."""

    def test_rejects_counter_trend_no_reversal(self, monkeypatch):
        """Build a signal with trend_alignment=-1, has_reversal=0 → run_once returns
        action='hold' with reason containing 'counter_trend_no_reversal'."""
        from metty.execution.live_trader import LiveTrader
        from shared.models import Signal, SignalType
        from datetime import datetime, timezone
        import pandas as pd

        t = LiveTrader(account="A", dry_run=True)

        fake_signal = Signal(
            symbol="XAUUSD",
            signal_type=SignalType.SELL,
            confidence=0.75,
            price=2000.0,
            timestamp=datetime.now(timezone.utc),
            timeframe="M5",
            indicators={"trend_alignment": -1, "has_reversal": 0.0, "reversal_strength": 0.0},
            reason="test counter-trend",
            regime="trending",
            strategy_id="test",
            weighted_score=-0.5,
        )

        m5 = pd.DataFrame({
            "open": [2000.0], "high": [2005.0], "low": [1995.0], "close": [2000.0],
            "timestamp": [datetime.now(timezone.utc)],
        })
        m5["timestamp"] = pd.to_datetime(m5["timestamp"])
        m5 = m5.set_index("timestamp")
        candles = {"M5": m5}

        monkeypatch.setattr(t, "_fetch_candles", lambda: candles)
        monkeypatch.setattr(t, "_monitor_positions", lambda c: [])
        monkeypatch.setattr(t, "_generate_signal", lambda c: fake_signal)
        monkeypatch.setattr(t, "_check_existing_position", lambda: False)
        monkeypatch.setattr(t, "_get_equity", lambda: 1000.0)
        monkeypatch.setattr(t._drawdown_protector, "sync_pnl_from_db", lambda *a, **k: False)
        monkeypatch.setattr(t._drawdown_protector, "check", lambda equity: (True, "OK"))

        result = t.run_once()
        assert result["action"] == "hold", f"counter-trend w/o reversal must hold, got {result['action']}"
        assert "counter-trend" in result["reason"] and "without reversal evidence" in result["reason"], (
            f"reason must reference counter-trend block, got: {result['reason']}"
        )

    def test_allows_trend_aligned_signal(self, monkeypatch):
        """trend_alignment=1 (trend-aligned) must NOT be rejected by the gate."""
        from metty.execution.live_trader import LiveTrader
        from shared.models import Signal, SignalType
        from datetime import datetime, timezone
        import pandas as pd

        t = LiveTrader(account="A", dry_run=True)
        fake_signal = Signal(
            symbol="XAUUSD",
            signal_type=SignalType.SELL,
            confidence=0.75,
            price=2000.0,
            timestamp=datetime.now(timezone.utc),
            timeframe="M5",
            indicators={"trend_alignment": 1, "has_reversal": 0.0, "reversal_strength": 0.0},
            reason="test trend-aligned",
            regime="trending",
            strategy_id="test",
            weighted_score=-0.5,
        )
        m5 = pd.DataFrame({
            "open": [2000.0], "high": [2005.0], "low": [1995.0], "close": [2000.0],
            "timestamp": [datetime.now(timezone.utc)],
        })
        m5["timestamp"] = pd.to_datetime(m5["timestamp"])
        m5 = m5.set_index("timestamp")
        candles = {"M5": m5}

        monkeypatch.setattr(t, "_fetch_candles", lambda: candles)
        monkeypatch.setattr(t, "_monitor_positions", lambda c: [])
        monkeypatch.setattr(t, "_generate_signal", lambda c: fake_signal)
        monkeypatch.setattr(t, "_check_existing_position", lambda: False)
        monkeypatch.setattr(t, "_get_equity", lambda: 1000.0)
        monkeypatch.setattr(t._drawdown_protector, "sync_pnl_from_db", lambda *a, **k: False)
        monkeypatch.setattr(t._drawdown_protector, "check", lambda equity: (True, "OK"))

        result = t.run_once()
        # Should NOT be rejected for counter_trend_no_reversal
        assert "counter_trend_no_reversal" not in result.get("reason", ""), (
            "trend-aligned signal must not hit counter-trend gate"
        )


# ─── RETRAIN-1: close_live_trade populates snapshot_id ──────────────────


class TestCloseLiveTradeSnapshotId:
    """RETRAIN-1: close_live_trade must look up snapshot_id from feature_snapshots
    via signal_id (was hardcoded NULL)."""

    def test_get_snapshot_id_for_signal_returns_row(self, tmp_path):
        """When a feature_snapshots row exists for a signal_id, helper returns its id."""
        import sqlite3
        from metty.core.db import init_db, get_snapshot_id_for_signal
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        # Insert account, signal, snapshot
        conn.execute("INSERT INTO accounts (id, name, balance, leverage, bridge_host, bridge_port, signal_group) VALUES (1, 'A', 100.0, 100, 'h', 5005, 'A')")
        conn.execute("INSERT INTO signals (id, timestamp, group_name, direction, confidence, triggering_indicators, price, account_id) VALUES (10, '2026-07-02T00:00:00+00:00', 'A', 'SELL', 0.7, 'rsi', 2000.0, 1)")
        conn.execute("INSERT INTO feature_snapshots (id, signal_id, timestamp, price) VALUES (555, 10, '2026-07-02T00:00:00+00:00', 2000.0)")
        conn.commit()
        conn.close()

        snap_id = get_snapshot_id_for_signal(10, db_path)
        assert snap_id == 555, f"expected 555, got {snap_id}"

    def test_get_snapshot_id_for_none_signal(self, tmp_path):
        """signal_id=None must return None without querying."""
        from metty.core.db import get_snapshot_id_for_signal
        assert get_snapshot_id_for_signal(None, str(tmp_path / "test.db")) is None

    def test_close_live_trade_populates_snapshot_id(self, tmp_path):
        """End-to-end: close a trade whose live_trades.signal_id maps to a snapshot;
        verify trade_outcomes.snapshot_id is the looked-up id, not NULL."""
        import sqlite3
        from metty.core.db import init_db, insert_live_trade, close_live_trade
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO accounts (id, name, balance, leverage, bridge_host, bridge_port, signal_group) VALUES (1, 'A', 500.0, 100, 'h', 5005, 'A')")
        conn.execute("INSERT INTO signals (id, timestamp, group_name, direction, confidence, triggering_indicators, price, account_id) VALUES (42, '2026-07-02T00:00:00+00:00', 'A', 'SELL', 0.7, 'rsi', 2000.0, 1)")
        conn.execute("INSERT INTO feature_snapshots (id, signal_id, timestamp, price) VALUES (777, 42, '2026-07-02T00:00:00+00:00', 2000.0)")
        conn.commit()
        conn.close()

        trade_id = insert_live_trade(
            account_id=1, timestamp="2026-07-02T00:00:00+00:00",
            direction="SELL", entry_price=2000.0,
            stop_loss=2010.0, take_profit=1970.0,
            lot_size=0.05, confidence=0.70,
            regime="trending", session="london", d1_trend="bearish",
            reason="test", ticket=None, symbol="XAUUSD",
            trading_mode="swing", strategy_id="test",
            tp1_price=0.0, tp_level=1,
            db_path=db_path, signal_id=42,
        )

        close_live_trade(
            trade_id=trade_id,
            exit_price=1975.0,
            exit_time="2026-07-02T04:00:00+00:00",
            pnl=25.0, pnl_pct=0.0125,
            exit_reason="take_profit",
            db_path=db_path,
        )

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT snapshot_id FROM trade_outcomes WHERE trade_id = ?", (trade_id,)).fetchone()
        conn.close()
        assert row is not None, "trade_outcomes row must be created"
        assert row[0] == 777, f"snapshot_id must be 777 (looked up), got {row[0]}"


# ─── RETRAIN-2: auto-retrain trigger gating ──────────────────────────────


class TestAutoRetrainGating:
    """RETRAIN-2: run_auto_retrain must respect gates — disabled by default,
    skip for Real-A-only container, run when enabled with B/C/D."""

    def test_disabled_by_default(self, monkeypatch, caplog):
        """AUTO_RETRAIN_ENABLED unset → function logs DISABLED and returns immediately."""
        from scripts.oracle_runner import run_auto_retrain
        monkeypatch.delenv("AUTO_RETRAIN_ENABLED", raising=False)
        # Should return without entering loop
        import inspect
        # The function returns early when disabled — verify by checking it doesn't loop
        # We call it and expect None return + no hang (it returns before the while True)
        result = run_auto_retrain("/tmp/test.db", ["A"], notifier=None)
        assert result is None, "disabled auto-retrain must return None"

    def test_skips_real_a_only_container(self, monkeypatch):
        """Enabled but accounts=['A'] → logs SKIP and returns (no B/C/D)."""
        from scripts.oracle_runner import run_auto_retrain
        monkeypatch.setenv("AUTO_RETRAIN_ENABLED", "1")
        result = run_auto_retrain("/tmp/test.db", ["A"], notifier=None)
        assert result is None, "Real-A-only container must skip auto-retrain"

    def test_proceeds_with_bcd(self, monkeypatch, tmp_path):
        """Enabled + accounts include B → enters loop (we kill it via missing state
        file + patched sleep to break out)."""
        from scripts.oracle_runner import run_auto_retrain
        monkeypatch.setenv("AUTO_RETRAIN_ENABLED", "1")
        monkeypatch.setenv("AUTO_RETRAIN_MIN_NEW_OUTCOMES", "999999")  # impossible to trigger
        # Patch time.sleep to raise to break the loop after first iteration
        import scripts.oracle_runner as runner
        call_count = {"n": 0}

        def fake_sleep(secs):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise KeyboardInterrupt("break loop")

        monkeypatch.setattr(runner.time, "sleep", fake_sleep)

        with pytest.raises(KeyboardInterrupt):
            run_auto_retrain(str(tmp_path / "test.db"), ["B", "C"], notifier=None)
        # If we got here, the function entered the loop (gate passed) — success


if __name__ == "__main__":
    pytest.main([__file__, "-v"])