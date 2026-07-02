"""Causal proof tests for m5_scalp_trader counter-trend reversal gate (2026-07-02).

Hypothesis
----------
On demo B/C/D, m5_scalp_trader has NO counter-trend rejection gate. The signal
generator (`generate_m5_scalp_signal`) only applies a confidence penalty
(COUNTER_TREND_CONFIDENCE_MULT < 1.0) — it does NOT hard-block. So when
confidence stays above threshold despite the penalty, m5_scalp can still open
counter-trend trades without reversal evidence — violating the CLAUDE.md iron
rule "ไม่แทงสวนเทรนด์" (no counter-trend trades without reversal signal).

Compare with `live_trader.py:1544-1567` (swing) which has an explicit gate:
reject when `trend_alignment == -1 AND not has_reversal AND not learning_mode
AND signal != HOLD`. m5_scalp_trader has zero matches for "reversal" /
"counter_trend" / "trend_alignment" in gate position — only the generator-side
penalty exists.

Impact:
  - m5_scalp trades can open counter-trend without HH/LL + OB/OS + divergence
  - Reversal gate (which the swing trader enforces) is bypassed for m5_scalp
  - Counter-trend trades historically have ~5-8% lower WR (per generator.py:1076)

Fix: Add `_apply_counter_trend_gate(signal, d1_trend, session, candles)` helper
to M5ScalpTrader that mirrors live_trader's gate, and call it in run_once after
the BUY confidence filter (step 7b), before drawdown protection (step 7c).
Reject hard with reason "counter_trend_no_reversal:{dir}_vs_{d1_trend}_d1".
learning_mode bypasses so ML data collection continues.

References
----------
- Bug found during counter-trend audit on 2026-07-02
- Class: metty/execution/m5_scalp_trader.py:M5ScalpTrader
- Sister gate: metty/execution/live_trader.py:1544-1567 (swing trader)
- CLAUDE.md "ไม่แทงสวนเทรนด์" iron rule
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _make_trader(learning_mode: bool = False):
    """Construct an M5ScalpTrader instance without running it."""
    from metty.execution.m5_scalp_trader import M5ScalpTrader
    t = M5ScalpTrader(account="C", dry_run=True)
    if learning_mode:
        t.learning_mode = True
    return t


def _signal(signal_type, trend_alignment, has_reversal, reversal_strength=0.0):
    """Build a minimal Signal with the indicators the gate reads."""
    from shared.models import Signal
    return Signal(
        symbol="XAUUSD",
        signal_type=signal_type,
        confidence=0.75,
        price=2000.0,
        timestamp=datetime.now(timezone.utc),
        timeframe="M5",
        indicators={
            "trend_alignment": trend_alignment,
            "has_reversal": has_reversal,
            "reversal_strength": reversal_strength,
        },
        reason="test",
        regime="trending",
        strategy_id="test",
        weighted_score=-0.5,
    )


class TestCounterTrendGateRejects:
    """Gate must reject trend_alignment == -1 without reversal evidence."""

    def test_rejects_sell_counter_trend_no_reversal(self):
        """SELL with trend_alignment=-1, has_reversal=0, non-learning → blocked."""
        from shared.models import SignalType
        t = _make_trader(learning_mode=False)
        sig = _signal(SignalType.SELL, trend_alignment=-1, has_reversal=0.0)

        blocked, reason = t._apply_counter_trend_gate(sig, d1_trend="bullish")

        assert blocked is True, (
            "counter-trend SELL vs bullish D1 without reversal evidence must be blocked"
        )
        assert "counter_trend_no_reversal" in reason, (
            f"reason must tag counter_trend_no_reversal for triage, got: {reason}"
        )
        assert "SELL" in reason and "bullish" in reason, (
            f"reason must reference direction + d1_trend for diagnostics, got: {reason}"
        )

    def test_rejects_buy_counter_trend_no_reversal(self):
        """BUY with trend_alignment=-1, has_reversal=0, non-learning → blocked."""
        from shared.models import SignalType
        t = _make_trader(learning_mode=False)
        sig = _signal(SignalType.BUY, trend_alignment=-1, has_reversal=0.0)

        blocked, reason = t._apply_counter_trend_gate(sig, d1_trend="bearish")

        assert blocked is True
        assert "counter_trend_no_reversal" in reason
        assert "BUY" in reason and "bearish" in reason


class TestCounterTrendGateAllows:
    """Gate must allow trend-aligned signals AND counter-trend WITH reversal."""

    def test_allows_trend_aligned_sell(self):
        """trend_alignment=1 (aligned) must NOT be blocked."""
        from shared.models import SignalType
        t = _make_trader(learning_mode=False)
        sig = _signal(SignalType.SELL, trend_alignment=1, has_reversal=0.0)

        blocked, _ = t._apply_counter_trend_gate(sig, d1_trend="bearish")
        assert blocked is False, "trend-aligned signal must not hit counter-trend gate"

    def test_allows_trend_aligned_buy(self):
        from shared.models import SignalType
        t = _make_trader(learning_mode=False)
        sig = _signal(SignalType.BUY, trend_alignment=1, has_reversal=0.0)

        blocked, _ = t._apply_counter_trend_gate(sig, d1_trend="bullish")
        assert blocked is False

    def test_allows_counter_trend_with_reversal_evidence(self):
        """trend_alignment=-1 BUT has_reversal=1 → allowed (legitimate reversal)."""
        from shared.models import SignalType
        t = _make_trader(learning_mode=False)
        sig = _signal(SignalType.SELL, trend_alignment=-1, has_reversal=1.0,
                      reversal_strength=0.8)

        blocked, _ = t._apply_counter_trend_gate(sig, d1_trend="bullish")
        assert blocked is False, (
            "counter-trend WITH reversal evidence (HH/LL + OB/OS + divergence) "
            "is a legitimate reversal trade, not a counter-trend violation"
        )

    def test_allows_neutral_trend_alignment(self):
        """trend_alignment=0 (neutral) must NOT be blocked (no counter-trend signal)."""
        from shared.models import SignalType
        t = _make_trader(learning_mode=False)
        sig = _signal(SignalType.SELL, trend_alignment=0, has_reversal=0.0)

        blocked, _ = t._apply_counter_trend_gate(sig, d1_trend="unknown")
        assert blocked is False


class TestCounterTrendGateBypass:
    """learning_mode bypasses the gate so ML outcome data is still collected."""

    def test_learning_mode_bypasses_counter_trend(self):
        """In learning_mode, even trend_alignment=-1 without reversal must pass."""
        from shared.models import SignalType
        t = _make_trader(learning_mode=True)
        sig = _signal(SignalType.SELL, trend_alignment=-1, has_reversal=0.0)

        blocked, _ = t._apply_counter_trend_gate(sig, d1_trend="bullish")
        assert blocked is False, (
            "learning_mode must bypass counter-trend gate to collect ML outcomes"
        )


class TestCounterTrendGateDefensive:
    """Gate must not crash on missing/None indicator values (defensive)."""

    def test_hold_signal_not_blocked(self):
        """HOLD signals must not be blocked (no position to open anyway)."""
        from shared.models import SignalType
        t = _make_trader(learning_mode=False)
        sig = _signal(SignalType.HOLD, trend_alignment=-1, has_reversal=0.0)

        blocked, _ = t._apply_counter_trend_gate(sig, d1_trend="bullish")
        assert blocked is False, "HOLD must never be blocked by counter-trend gate"

    def test_missing_indicators_not_blocked(self):
        """Signal with empty indicators dict must not crash — return not blocked."""
        from shared.models import Signal, SignalType
        t = _make_trader(learning_mode=False)
        sig = Signal(
            symbol="XAUUSD",
            signal_type=SignalType.SELL,
            confidence=0.75,
            price=2000.0,
            timestamp=datetime.now(timezone.utc),
            timeframe="M5",
            indicators={},
            reason="test",
            regime="trending",
            strategy_id="test",
            weighted_score=-0.5,
        )

        blocked, _ = t._apply_counter_trend_gate(sig, d1_trend="bullish")
        assert blocked is False, (
            "missing indicators must not crash or block — defensive default"
        )


class TestGateContractMatchesSwing:
    """Contract must match live_trader's reversal gate — same rejection shape."""

    def test_same_rejection_reason_format_as_swing(self):
        """Both gates must emit 'counter_trend_no_reversal:{dir}_vs_{trend}_d1'
        so triage/reason-grouping stats work across both traders."""
        from shared.models import SignalType
        t = _make_trader(learning_mode=False)
        sig = _signal(SignalType.SELL, trend_alignment=-1, has_reversal=0.0)

        _, reason = t._apply_counter_trend_gate(sig, d1_trend="bullish")

        # Exact format the swing gate uses (live_trader.py:1557)
        assert reason.startswith("counter_trend_no_reversal:SELL_vs_bullish_d1"), (
            f"reason must match swing gate format 'counter_trend_no_reversal:{{dir}}_vs_{{trend}}_d1', "
            f"got: {reason}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])