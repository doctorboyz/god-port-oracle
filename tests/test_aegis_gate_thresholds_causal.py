"""Causal proof test: AEGIS counter-trend gate thresholds block valid mean-revert signals (Fix B, 2026-08-25).

Hypothesis
----------
Demo-D has not traded since 2026-07-02 (53+ days). Engine logs show the pattern:
  "counter-trend blocked: H4 bullish overrides D1 bearish"

The h4_override gate at `broky/signals/generator.py:1159-1173` is supposed to allow
a mean-reversion exception when price is at a Bollinger extreme:
  - BUY counter-trend allowed when band_position <= oversold threshold
  - SELL counter-trend allowed when band_position >= overbought threshold

Two bugs prevent the exception from ever firing:

  Bug #1 (threshold mismatch): The gate hardcodes `band_position <= 0.15` and
  `band_position >= 0.85`, but the project's own lowered constants
  `REVERSAL_OS_BOLL = 0.20` and `REVERSAL_OB_BOLL = 0.80` (set 2026-07-09 because
  XAUUSD hits OB/OS at milder levels — see generator.py:107-118) are used
  everywhere else. The gate never got updated. Result: a signal with
  band_position=0.83 (passes REVERSAL_OB_BOLL=0.80, fails hardcoded 0.85) is
  blocked despite the project's stated threshold.

  Bug #2 (trend_mult too aggressive): When the exception DOES fire, the gate
  sets `trend_mult = 0.3`, which trims confidence to 30% of raw. With
  MIN_CONFIDENCE=0.60 (line 73), the signal needs raw_confidence >= 0.857
  to pass. Even max bearish alignment (macd=-1, ema_cross=-1, adx=-1,
  ema_trend=-0.5, volume=-0.5) yields raw_confidence ≈ 0.94; 0.94 * 0.3 = 0.28
  << 0.60 → blocked at the confidence filter. The exception is mathematically
  nearly impossible.

Causal proof
------------
Construct data that produces:
  1. A SELL signal (max bearish indicator alignment)
  2. band_position = 0.83 (passes new REVERSAL_OB_BOLL=0.80, fails old 0.85)
  3. h4_override=True (d1=bearish, h4=bullish → effective_trend=bullish →
     SELL is counter-trend)

The natural Bollinger calculation cannot produce band_position=0.83
simultaneously with bearish M5 momentum — momentum indicators (MACD, EMA
cross) respond faster than the 20-period Bollinger middle, so any drop
large enough to flip them bearish also pushes price below the band middle.
The mean-revert SELL exception is genuinely a rare "top-of-bubble" pattern
(Bollinger lags while momentum flips). To isolate the threshold logic —
which IS the causal mechanism under test — we monkeypatch
`calculate_bollinger` to inject a known band_position.

  - BEFORE fix: gate uses 0.85 → 0.83 < 0.85 → blocked → HOLD
  - AFTER fix: gate uses 0.80 → 0.83 >= 0.80 → exception fires, trend_mult=0.7,
    confidence = 0.94 * 0.7 = 0.66 >= 0.60 → SELL

The second test (TestTrendMultBump) isolates Bug #2: with band_position=0.90
(passes both 0.80 and 0.85 thresholds), the old trend_mult=0.3 yields
confidence 0.94 * 0.3 = 0.28 < 0.60 → HOLD at the confidence filter; the new
trend_mult=0.7 yields 0.66 >= 0.60 → SELL.

References
----------
- Learning: ψ/memory/learnings/2026-08-25_aegis-gate-blocks-demo-d.md
- Production: broky/signals/generator.py:1137-1187 (h4_override gate)
- Production: broky/signals/generator.py:111-118 (REVERSAL_*_BOLL constants)
- IRON LAW check: CLAUDE.md "Uptrend → ยอม SELL ถ้ามี reversal signal ชัดเจน +
  overbought + lower low เกิดขึ้นแล้ว" — the mean-revert SELL at Bollinger
  extreme IS the documented allowed reversal trade, not counter-trend.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from broky.signals import generator as gen_mod
from broky.signals.generator import (
    REVERSAL_OB_BOLL,
    REVERSAL_OS_BOLL,
    generate_signal,
)
from shared.models import SignalType


def _make_max_bearish_data(n: int = 200) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Construct OHLCV that yields max bearish indicator alignment.

    Produces: adx=-1, macd=-1, ema_cross=-1, ema_trend=-0.5, bollinger=0,
    volume=-0.5 → weighted=-0.789, raw_confidence≈0.94.
    """
    np.random.seed(42)
    # Strong sustained downtrend → adx=-1, macd=-1, ema_cross=-1, ema_trend=-0.5
    close = pd.Series(2000 + np.cumsum(np.random.normal(-1.5, 0.3, n)))
    high = close + 1.0
    low = close - 1.0
    volume = pd.Series(np.random.uniform(1000, 5000, n))
    # Drop volume only on the last bar → volume score = -0.5
    # (rolling 20-bar avg still high → ratio ≈ 0.04 → very low → -0.5)
    volume.iloc[-1] = 100
    return close, high, low, volume


def _patch_bollinger(target_band_position: float, current_price: float) -> None:
    """Monkeypatch calculate_bollinger to produce a known band_position.

    band_position = (current - lower) / (upper - lower). We solve for lower
    and upper given current and target position with a fixed band width.
    """
    band_width = 100.0
    lower = current_price - target_band_position * band_width
    upper = current_price + (1.0 - target_band_position) * band_width
    middle = (upper + lower) / 2

    fake = SimpleNamespace(
        upper=pd.Series([upper]),
        middle=pd.Series([middle]),
        lower=pd.Series([lower]),
    )
    # Force the latest Bollinger values to put current_price at target_band_position
    gen_mod.calculate_bollinger = lambda c, period=20, std_dev=2.0: fake  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _pin_session_mult(monkeypatch):
    """Disable session confidence multipliers for the whole module.

    SESSION_CONFIDENCE_MULTIPLIER applies ASIAN ×0.70 based on the current
    WALL-CLOCK session, so confidence-threshold assertions here only held
    outside Asian hours (suite flaky — observed failing at ~07:00 UTC).
    Session-of-day is not the mechanism under test; pin it off.
    """
    monkeypatch.setattr(gen_mod, "SESSION_CONFIDENCE_MULT_DISABLED", True)


@pytest.fixture
def restore_bollinger():
    """Restore the real calculate_bollinger after each test."""
    real = gen_mod.calculate_bollinger
    yield
    gen_mod.calculate_bollinger = real  # type: ignore[assignment]


class TestH4OverrideThresholdUsesConstants:
    """Bug #1: gate hardcodes 0.15/0.85 instead of REVERSAL_OS/OB_BOLL constants."""

    def test_threshold_constants_exist_and_are_lowered(self):
        """Constants must be 0.20/0.80 (lowered 2026-07-09), not 0.15/0.85."""
        assert REVERSAL_OS_BOLL == 0.20, "REVERSAL_OS_BOLL must stay at 0.20 (lowered for XAUUSD)"
        assert REVERSAL_OB_BOLL == 0.80, "REVERSAL_OB_BOLL must stay at 0.80 (lowered for XAUUSD)"

    def test_band_position_083_unblocks_sell_after_fix(self, restore_bollinger):
        """band_position=0.83 (in [0.80, 0.85)) must produce SELL after fix B.

        Before fix: 0.83 < 0.85 (hardcoded) → blocked → HOLD (RED)
        After fix:  0.83 >= 0.80 (REVERSAL_OB_BOLL) → exception → SELL (GREEN)
        """
        close, high, low, volume = _make_max_bearish_data()
        current_price = float(close.iloc[-1])
        _patch_bollinger(target_band_position=0.83, current_price=current_price)

        sig = generate_signal(
            close, high, low, volume,
            d1_trend="bearish",
            h4_trend="bullish",
            strategy_id="test_aegis_causal",
        )

        # The signal MUST be SELL — band_position=0.83 passes the lowered
        # REVERSAL_OB_BOLL=0.80 threshold. With trend_mult=0.7 (fix B),
        # confidence = 0.94 * 0.7 = 0.66 >= MIN_CONFIDENCE=0.60 → SELL.
        assert sig.signal_type == SignalType.SELL, (
            f"Expected SELL after fix B (band_position=0.83 passes REVERSAL_OB_BOLL=0.80, "
            f"trend_mult=0.7 lets conf={sig.confidence:.3f} pass MIN_CONFIDENCE=0.60). "
            f"Got {sig.signal_type.value}. Reason: {sig.reason}"
        )
        # Sanity: confidence must clear MIN_CONFIDENCE
        assert sig.confidence >= 0.60, f"confidence {sig.confidence:.3f} below MIN_CONFIDENCE 0.60"
        # Sanity: trend_mult must be the bumped 0.7, not the old 0.3
        assert sig.trend_mult == pytest.approx(0.7, abs=0.01), (
            f"trend_mult must be 0.7 after fix B, got {sig.trend_mult}"
        )

    def test_band_position_079_still_blocked(self, restore_bollinger):
        """band_position=0.79 must still be BLOCKED (below REVERSAL_OB_BOLL=0.80).

        This guards against over-loosening: the fix must not allow signals
        below the lowered constant.
        """
        close, high, low, volume = _make_max_bearish_data()
        current_price = float(close.iloc[-1])
        _patch_bollinger(target_band_position=0.79, current_price=current_price)

        sig = generate_signal(
            close, high, low, volume,
            d1_trend="bearish",
            h4_trend="bullish",
            strategy_id="test_aegis_causal",
        )

        assert sig.signal_type == SignalType.HOLD, (
            f"band_position=0.79 < REVERSAL_OB_BOLL=0.80 must stay blocked. "
            f"Got {sig.signal_type.value}. Reason: {sig.reason}"
        )


class TestTrendMultBump:
    """Bug #2: trend_mult=0.3 makes the exception mathematically impossible.

    With max bearish alignment, raw_confidence ≈ 0.94. The confidence filter
    (MIN_CONFIDENCE=0.60) rejects any signal with confidence < 0.60.
    Old: 0.94 * 0.3 = 0.28 < 0.60 → rejected. New: 0.94 * 0.7 = 0.66 >= 0.60 → accepted.
    """

    def test_trend_mult_07_passes_confidence_filter(self, restore_bollinger):
        """band_position=0.90 (above both thresholds) must produce SELL.

        Before fix: trend_mult=0.3 → 0.94 * 0.3 = 0.28 < 0.60 → HOLD (RED)
        After fix:  trend_mult=0.7 → 0.94 * 0.7 = 0.66 >= 0.60 → SELL (GREEN)
        """
        close, high, low, volume = _make_max_bearish_data()
        current_price = float(close.iloc[-1])
        # 0.90 passes BOTH the old 0.85 and new 0.80 thresholds — isolates Bug #2
        _patch_bollinger(target_band_position=0.90, current_price=current_price)

        sig = generate_signal(
            close, high, low, volume,
            d1_trend="bearish",
            h4_trend="bullish",
            strategy_id="test_aegis_causal",
        )

        assert sig.signal_type == SignalType.SELL, (
            f"band_position=0.90 passes both thresholds; with trend_mult=0.7 "
            f"confidence={sig.confidence:.3f} must clear 0.60. Got {sig.signal_type.value}. "
            f"Reason: {sig.reason}"
        )
        assert sig.trend_mult == pytest.approx(0.7, abs=0.01), (
            f"trend_mult must be 0.7 after fix B, got {sig.trend_mult}"
        )
        assert sig.confidence >= 0.60, (
            f"confidence {sig.confidence:.3f} must pass MIN_CONFIDENCE=0.60 after trend_mult bump"
        )


class TestIronLawPreserved:
    """Verify fix B does NOT violate the 'ไม่แทงสวนเทรนด์' IRON LAW.

    CLAUDE.md: 'Uptrend → ยอม SELL ถ้ามี reversal signal ชัดเจน + overbought +
    lower low เกิดขึ้นแล้ว'. The mean-revert SELL at Bollinger overbought IS
    the documented allowed reversal trade, not a counter-trend trade.

    The exception must NOT fire outside the Bollinger extreme zone.
    """

    def test_mid_band_counter_trend_still_blocked(self, restore_bollinger):
        """band_position=0.50 (mid-band) must stay blocked — no Bollinger extreme."""
        close, high, low, volume = _make_max_bearish_data()
        current_price = float(close.iloc[-1])
        _patch_bollinger(target_band_position=0.50, current_price=current_price)

        sig = generate_signal(
            close, high, low, volume,
            d1_trend="bearish",
            h4_trend="bullish",
            strategy_id="test_aegis_causal",
        )

        assert sig.signal_type == SignalType.HOLD, (
            f"Mid-band counter-trend SELL must stay blocked — no Bollinger extreme. "
            f"Got {sig.signal_type.value}. Reason: {sig.reason}"
        )

    def test_aligned_sell_unaffected(self, restore_bollinger):
        """d1=bearish, h4=bearish (no override) → SELL is trend-aligned, gate skipped.

        Verifies fix B only affects the h4_override path; aligned trades
        pass through untouched.
        """
        close, high, low, volume = _make_max_bearish_data()
        # Use real Bollinger (no patch) — aligned SELL should not need the gate
        sig = generate_signal(
            close, high, low, volume,
            d1_trend="bearish",
            h4_trend="bearish",
            strategy_id="test_aegis_causal",
        )

        # Aligned bearish signal: no h4_override → gate not entered → SELL passes
        assert sig.signal_type == SignalType.SELL, (
            f"Trend-aligned SELL (d1=h4=bearish) must pass unaffected by gate. "
            f"Got {sig.signal_type.value}. Reason: {sig.reason}"
        )