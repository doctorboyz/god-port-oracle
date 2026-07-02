"""Causal proof tests for scalp_trader counter-trend reversal gate (2026-07-02).

Hypothesis
----------
On demo B/C/D, scalp_trader (M1) has NO counter-trend rejection gate, AND its
signal generator (`broky.signals.scalp_generator.generate_scalp_signal`) does
NOT produce `trend_alignment` or `has_reversal` indicators at all. So even if
a gate were wired in, it would be a no-op (indicator lookup returns None).

scalp_trader does compute a `_d1_proxy` from H1 EMA50 inside its ML filter
block (line ~1040), but ONLY after the gate would run, and only for ML
feature computation — never injected back into signal.indicators.

Compare with swing trader (`live_trader.py:1544-1567`) and m5_scalp_trader
(step 7b1) which both enforce the "ไม่แทงสวนเทรนด์" iron rule.

Fix:
1. Add `_compute_trend_alignment(signal, candles)` helper to ScalpTrader that
   computes d1_proxy from H1 EMA50 and returns `trend_alignment` int via
   `compute_trend_alignment_value(direction, d1_proxy, h4_trend=None,
   has_reversal=False)`. M1 scalp seeks momentum bursts, not reversals —
   has_reversal is always False (M1 too fast for reliable swing-structure
   confirmation). This means ALL counter-trend scalp trades are blocked,
   which is MORE conservative than swing/m5_scalp (which allow reversal
   trades). Appropriate for high-frequency M1.
2. Add `_apply_counter_trend_gate(signal, d1_trend)` helper. scalp_trader has
   no learning_mode attribute — gate never bypasses (scalp is live-only,
   no ML data collection).
3. In run_once, after signal generation, compute trend_alignment, inject into
   signal.indicators, then run gate at step 5b1 (after BUY confidence filter,
   before drawdown protection). Mirrors m5_scalp gate ordering.

References
----------
- Bug found during "make reversal gate complete" request on 2026-07-02
- Class: metty/execution/scalp_trader.py:ScalpTrader
- Sister gates: live_trader.py:1544-1567 (swing), m5_scalp_trader step 7b1
- CLAUDE.md "ไม่แทงสวนเทรนด์" iron rule
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _make_trader():
    """Construct a ScalpTrader instance without running it."""
    from metty.execution.scalp_trader import ScalpTrader
    return ScalpTrader(dry_run=True)


def _signal(signal_type, trend_alignment, has_reversal=0.0):
    """Build a minimal Signal with the indicators the gate reads."""
    from shared.models import Signal
    return Signal(
        symbol="XAUUSD",
        signal_type=signal_type,
        confidence=0.75,
        price=2000.0,
        timestamp=datetime.now(timezone.utc),
        timeframe="M1",
        indicators={
            "trend_alignment": trend_alignment,
            "has_reversal": has_reversal,
        },
        reason="test",
        regime="trending",
        strategy_id="test",
        weighted_score=-0.5,
    )


class TestCounterTrendGateRejects:
    """Gate must reject trend_alignment == -1 (counter-trend without reversal)."""

    def test_rejects_sell_counter_trend(self):
        """SELL with trend_alignment=-1, has_reversal=0 → blocked."""
        from shared.models import SignalType
        t = _make_trader()
        sig = _signal(SignalType.SELL, trend_alignment=-1)

        blocked, reason = t._apply_counter_trend_gate(sig, d1_trend="bullish")

        assert blocked is True, (
            "counter-trend SELL vs bullish D1 must be blocked on scalp"
        )
        assert "counter_trend_no_reversal" in reason
        assert "SELL" in reason and "bullish" in reason

    def test_rejects_buy_counter_trend(self):
        """BUY with trend_alignment=-1, has_reversal=0 → blocked."""
        from shared.models import SignalType
        t = _make_trader()
        sig = _signal(SignalType.BUY, trend_alignment=-1)

        blocked, reason = t._apply_counter_trend_gate(sig, d1_trend="bearish")

        assert blocked is True
        assert "counter_trend_no_reversal" in reason
        assert "BUY" in reason and "bearish" in reason


class TestCounterTrendGateAllows:
    """Gate must allow trend-aligned and neutral signals."""

    def test_allows_trend_aligned_sell(self):
        from shared.models import SignalType
        t = _make_trader()
        sig = _signal(SignalType.SELL, trend_alignment=1)
        blocked, _ = t._apply_counter_trend_gate(sig, d1_trend="bearish")
        assert blocked is False

    def test_allows_trend_aligned_buy(self):
        from shared.models import SignalType
        t = _make_trader()
        sig = _signal(SignalType.BUY, trend_alignment=1)
        blocked, _ = t._apply_counter_trend_gate(sig, d1_trend="bullish")
        assert blocked is False

    def test_allows_neutral_trend_alignment(self):
        """trend_alignment=0 (d1 unknown) → not blocked (no trend to be counter to)."""
        from shared.models import SignalType
        t = _make_trader()
        sig = _signal(SignalType.SELL, trend_alignment=0)
        blocked, _ = t._apply_counter_trend_gate(sig, d1_trend="unknown")
        assert blocked is False


class TestCounterTrendGateDefensive:
    """Gate must not crash on edge inputs."""

    def test_hold_signal_not_blocked(self):
        from shared.models import SignalType
        t = _make_trader()
        sig = _signal(SignalType.HOLD, trend_alignment=-1)
        blocked, _ = t._apply_counter_trend_gate(sig, d1_trend="bullish")
        assert blocked is False

    def test_missing_indicators_not_blocked(self):
        """Empty indicators dict must not crash — defensive default."""
        from shared.models import Signal, SignalType
        t = _make_trader()
        sig = Signal(
            symbol="XAUUSD",
            signal_type=SignalType.SELL,
            confidence=0.75,
            price=2000.0,
            timestamp=datetime.now(timezone.utc),
            timeframe="M1",
            indicators={},
            reason="test",
            regime="trending",
            strategy_id="test",
            weighted_score=-0.5,
        )
        blocked, _ = t._apply_counter_trend_gate(sig, d1_trend="bullish")
        assert blocked is False

    def test_none_signal_not_blocked(self):
        """None signal must not crash — defensive."""
        t = _make_trader()
        blocked, _ = t._apply_counter_trend_gate(None, d1_trend="bullish")
        assert blocked is False


class TestScalpHasNoLearningBypass:
    """scalp_trader has no learning_mode — gate must NOT bypass even if
    a learning_mode attribute is somehow set (scalp is live-only)."""

    def test_gate_blocks_even_with_learning_mode_attr_set(self):
        """Even if someone sets learning_mode=True on scalp_trader, the gate
        must still block counter-trend trades. Scalp has no ML data collection
        path — there is no legitimate reason to bypass the iron rule."""
        from shared.models import SignalType
        t = _make_trader()
        # Simulate someone incorrectly setting learning_mode
        t.learning_mode = True
        sig = _signal(SignalType.SELL, trend_alignment=-1)

        blocked, _ = t._apply_counter_trend_gate(sig, d1_trend="bullish")
        assert blocked is True, (
            "scalp_trader must NOT bypass counter-trend gate even if "
            "learning_mode is set — scalp has no ML data collection path"
        )


class TestTrendAlignmentComputation:
    """_compute_trend_alignment must produce correct value from H1 data."""

    def test_unknown_when_h1_missing(self):
        """No H1 data → trend_alignment=0 (neutral, gate won't block)."""
        from shared.models import SignalType
        t = _make_trader()
        sig = _signal(SignalType.SELL, trend_alignment=99)  # placeholder
        # Empty candles → no H1 → d1_proxy="unknown" → trend_alignment=0
        ta = t._compute_trend_alignment(sig, candles={})
        assert ta == 0, (
            f"missing H1 must yield trend_alignment=0 (neutral), got {ta}"
        )

    def test_bullish_when_price_above_ema50(self):
        """H1 close > EMA50 → d1_proxy='bullish' → SELL is counter-trend (-1)."""
        import pandas as pd
        from shared.models import SignalType
        t = _make_trader()
        # 60 bars rising → price above EMA50 → bullish d1
        h1 = pd.DataFrame({
            "close": [4100.0 + i * 2 for i in range(60)],
            "high": [4105.0 + i * 2 for i in range(60)],
            "low": [4095.0 + i * 2 for i in range(60)],
            "volume": [100.0] * 60,
        })
        sig = _signal(SignalType.SELL, trend_alignment=99)
        ta = t._compute_trend_alignment(sig, candles={"H1": h1})
        assert ta == -1, (
            f"SELL vs bullish H1 (price>EMA50) must be counter-trend (-1), got {ta}"
        )

    def test_aligned_when_buy_in_bullish(self):
        """H1 rising → d1_proxy='bullish' → BUY is trend-aligned (1)."""
        import pandas as pd
        from shared.models import SignalType
        t = _make_trader()
        h1 = pd.DataFrame({
            "close": [4100.0 + i * 2 for i in range(60)],
            "high": [4105.0 + i * 2 for i in range(60)],
            "low": [4095.0 + i * 2 for i in range(60)],
            "volume": [100.0] * 60,
        })
        sig = _signal(SignalType.BUY, trend_alignment=99)
        ta = t._compute_trend_alignment(sig, candles={"H1": h1})
        assert ta == 1, (
            f"BUY vs bullish H1 must be trend-aligned (1), got {ta}"
        )


class TestGateContractMatchesSisters:
    """Reason format must match swing + m5_scalp gates for cross-trader stats."""

    def test_same_reason_format(self):
        from shared.models import SignalType
        t = _make_trader()
        sig = _signal(SignalType.SELL, trend_alignment=-1)
        _, reason = t._apply_counter_trend_gate(sig, d1_trend="bullish")
        assert reason.startswith("counter_trend_no_reversal:SELL_vs_bullish_d1"), (
            f"reason must match sister-gate format, got: {reason}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])