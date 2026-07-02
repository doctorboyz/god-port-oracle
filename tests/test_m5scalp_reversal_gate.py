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

import pandas as pd
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


# ---------------------------------------------------------------------------
# Causal proof: generator emits trend_alignment / has_reversal (Task #80, 2026-07-02)
#
# Root cause of the original dead-code bug: the gate (consumer) was wired but
# the generator (producer) never emitted the indicators → gate read None → never blocked.
# These tests prove the producer side is fixed, so the gate has real input to read.
# ---------------------------------------------------------------------------

def _build_strong_bullish_data(n: int = 300):
    import numpy as np
    np.random.seed(123)
    base = 2300.0
    trend = np.linspace(0, 120, n)
    noise = np.random.normal(0, 0.5, n)
    closes = base + trend + noise
    spread = np.random.uniform(0.5, 2.0, n)
    highs = closes + spread
    lows = closes - spread
    opens = closes - np.random.uniform(0, 1, n)
    volumes = np.full(n, 3000.0)
    idx = pd.date_range(
        start=datetime(2026, 4, 29, 8, 0, tzinfo=timezone.utc),
        periods=n, freq="5min",
    )
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows,
         "Close": closes, "Volume": volumes},
        index=idx,
    )


def _build_strong_bearish_data(n: int = 300):
    import numpy as np
    np.random.seed(999)
    base = 2300.0
    up_phase = np.linspace(0, 60, n // 2)
    down_phase = np.linspace(0, -120, n - n // 2)
    trend = np.concatenate([up_phase, down_phase])
    noise = np.random.normal(0, 0.5, n)
    closes = base + trend + noise
    spread = np.random.uniform(0.3, 1.5, n)
    opens = closes + np.random.uniform(-0.3, 0.3, n)
    highs = np.maximum(opens, closes) + spread * 0.3
    lows = np.minimum(opens, closes) - spread * 0.7
    volumes = np.full(n, 4000.0)
    idx = pd.date_range(
        start=datetime(2026, 4, 29, 8, 0, tzinfo=timezone.utc),
        periods=n, freq="5min",
    )
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows,
         "Close": closes, "Volume": volumes},
        index=idx,
    )


class TestGeneratorEmitsReversalIndicators:
    """Causal proof: generator MUST emit trend_alignment + has_reversal on every
    non-HOLD signal. If these keys are missing, the gate is dead code again."""

    def test_bullish_signal_has_trend_alignment_and_has_reversal_keys(self):
        from broky.signals.m5_scalp_generator import generate_m5_scalp_signal
        from shared.models import SignalType
        df = _build_strong_bullish_data(300)
        sig = generate_m5_scalp_signal(
            close=df["Close"], high=df["High"], low=df["Low"], volume=df["Volume"],
            current_price=float(df["Close"].iloc[-1]),
            timestamp=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
            spread=5.0, d1_trend="bullish", h4_trend="bullish",
        )
        if sig.signal_type == SignalType.HOLD:
            pytest.skip(f"synthetic data produced HOLD ({sig.reason}) — fixture tuning needed")
        # The causal claim: keys must be present in indicators
        assert "trend_alignment" in sig.indicators, (
            "generator must emit trend_alignment — gate reads this key; "
            "missing key is the exact root cause of the dead-code bug"
        )
        assert "has_reversal" in sig.indicators, (
            "generator must emit has_reversal — gate reads this key"
        )
        # Aligned BUY vs bullish D1 → trend_alignment must be 1 (aligned)
        assert sig.indicators["trend_alignment"] == 1.0, (
            f"aligned BUY vs bullish D1 must yield trend_alignment=1.0, "
            f"got {sig.indicators['trend_alignment']}"
        )

    def test_counter_trend_signal_emits_negative_alignment(self):
        """Bearish M5 signal vs bullish D1 → trend_alignment == -1 (counter-trend).
        This is the exact scenario the gate exists to catch.

        Note: we use learning_mode=True at the GENERATOR to bypass the confidence
        filter (which would otherwise drop counter-trend signals to HOLD via
        COUNTER_TREND_CONFIDENCE_MULT). The trend_alignment value is computed
        regardless of learning_mode — it depends only on direction vs d1/h4 trend.
        """
        from broky.signals.m5_scalp_generator import generate_m5_scalp_signal
        from shared.models import SignalType
        df = _build_strong_bearish_data(300)
        sig = generate_m5_scalp_signal(
            close=df["Close"], high=df["High"], low=df["Low"], volume=df["Volume"],
            current_price=float(df["Close"].iloc[-1]),
            timestamp=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
            spread=5.0,
            d1_trend="bullish",   # D1 bullish
            h4_trend="bullish",   # H4 bullish
            learning_mode=True,   # bypass confidence filter so SELL survives
        )
        assert sig.signal_type == SignalType.SELL, (
            f"bearish fixture + learning_mode should produce SELL, got {sig.signal_type}"
        )
        assert sig.indicators.get("trend_alignment") == -1.0, (
            f"SELL vs bullish D1/H4 must emit trend_alignment=-1.0 (counter-trend), "
            f"got {sig.indicators.get('trend_alignment')}"
        )

    def test_generator_to_gate_integration_blocks_counter_trend(self):
        """End-to-end causal proof: feed a real generator signal (counter-trend,
        no reversal evidence) into the gate → MUST be blocked.

        This is the causal test that the producer (generator) AND consumer (gate)
        are wired together. Mock-signal tests do NOT catch the dead-code bug
        because the bug was in the producer. This test uses the real generator.

        Setup: generator runs with learning_mode=True (to bypass the confidence
        filter and emit a real counter-trend SELL). The gate then runs with
        learning_mode=False (so it enforces). In production both flags are tied
        to the same env, but for this wiring proof we isolate the two concerns."""
        from broky.signals.m5_scalp_generator import generate_m5_scalp_signal
        df = _build_strong_bearish_data(300)
        sig = generate_m5_scalp_signal(
            close=df["Close"], high=df["High"], low=df["Low"], volume=df["Volume"],
            current_price=float(df["Close"].iloc[-1]),
            timestamp=datetime(2026, 4, 29, 10, 0, tzinfo=timezone.utc),
            spread=5.0, d1_trend="bullish", h4_trend="bullish",
            learning_mode=True,  # generator: emit SELL despite low confidence
        )
        assert sig.signal_type.value == "SELL", (
            "fixture must produce SELL for the gate test to be meaningful"
        )
        assert sig.indicators.get("trend_alignment") == -1.0, (
            "fixture must produce counter-trend alignment for the gate test"
        )
        # If generator emitted has_reversal=1, the gate legitimately allows it.
        if sig.indicators.get("has_reversal") == 1.0:
            pytest.skip("fixture produced a legitimate reversal signal — gate allows it")
        # Gate in NON-learning mode → must enforce
        t = _make_trader(learning_mode=False)
        blocked, reason = t._apply_counter_trend_gate(sig, d1_trend="bullish")
        assert blocked is True, (
            "Real generator signal with trend_alignment=-1, has_reversal=0, "
            "non-learning mode MUST be blocked by the gate — this is the causal "
            "proof that producer + consumer are wired together"
        )
        assert "counter_trend_no_reversal" in reason


if __name__ == "__main__":
    pytest.main([__file__, "-v"])