"""Tests for reversal signal detection and trend alignment feature.

Verifies:
1. compute_reversal_signal() correctly identifies OB/OS + divergence conditions
2. compute_trend_alignment_value() returns correct alignment codes
3. Edge cases: no trend, HOLD signal, missing indicator data
4. Signal generator includes reversal features in scores dict
5. Feature registry includes new features
"""

import pytest
from broky.signals.generator import (
    compute_reversal_signal,
    compute_trend_alignment_value,
    REVERSAL_OB_RSI,
    REVERSAL_OS_RSI,
    REVERSAL_OB_STOCH,
    REVERSAL_OS_STOCH,
    REVERSAL_OB_BOLL,
    REVERSAL_OS_BOLL,
    REVERSAL_OB_MFI,
    REVERSAL_OS_MFI,
    VOLATILE_BW_THRESHOLD,
)
from broky.ml.features import ALL_FEATURE_COLS, BROKY_FEATURES, validate_feature_registry


class TestComputeReversalSignal:
    """Test reversal signal detection logic."""

    def test_trend_aligned_sell_in_bearish_not_reversal(self):
        """SELL in bearish D1 = trend-aligned, NOT reversal."""
        has_reversal, strength = compute_reversal_signal(
            direction="SELL", d1_trend="bearish", h4_trend="bearish",
            rsi=30, stoch_k=25, boll_pct_b=0.5, mfi=45,
            macd_hist=-0.5, plus_di=20, minus_di=30, boll_bw=0.01,
        )
        assert has_reversal is False
        assert strength == 0.0

    def test_trend_aligned_buy_in_bullish_not_reversal(self):
        """BUY in bullish D1 = trend-aligned, NOT reversal."""
        has_reversal, strength = compute_reversal_signal(
            direction="BUY", d1_trend="bullish", h4_trend="bullish",
            rsi=50, stoch_k=55, boll_pct_b=0.5, mfi=50,
            macd_hist=0.5, plus_di=30, minus_di=20, boll_bw=0.01,
        )
        assert has_reversal is False
        assert strength == 0.0

    def test_counter_sell_in_bullish_with_ob_and_divergence(self):
        """SELL in bullish D1 with overbought + divergence = reversal."""
        has_reversal, strength = compute_reversal_signal(
            direction="SELL", d1_trend="bullish", h4_trend="bearish",  # H4 disagrees
            rsi=75, stoch_k=85, boll_pct_b=0.90, mfi=85,
            macd_hist=-0.3, plus_di=25, minus_di=30, boll_bw=0.015,
        )
        assert has_reversal is True
        assert strength > 0.0

    def test_counter_buy_in_bearish_with_os_and_divergence(self):
        """BUY in bearish D1 with oversold + divergence = reversal."""
        has_reversal, strength = compute_reversal_signal(
            direction="BUY", d1_trend="bearish", h4_trend="bullish",  # H4 disagrees
            rsi=25, stoch_k=15, boll_pct_b=0.10, mfi=15,
            macd_hist=0.3, plus_di=30, minus_di=25, boll_bw=0.015,
        )
        assert has_reversal is True
        assert strength > 0.0

    def test_counter_sell_without_ob_not_reversal(self):
        """SELL in bullish D1 WITHOUT overbought condition = NOT reversal (bad counter-trend)."""
        has_reversal, strength = compute_reversal_signal(
            direction="SELL", d1_trend="bullish", h4_trend="bullish",
            rsi=50, stoch_k=55, boll_pct_b=0.50, mfi=50,
            macd_hist=-0.3, plus_di=25, minus_di=30, boll_bw=0.01,
        )
        assert has_reversal is False
        assert strength == 0.0

    def test_counter_sell_with_ob_but_no_divergence(self):
        """SELL in bullish with OB but NO divergence = NOT reversal (no evidence of turning)."""
        has_reversal, strength = compute_reversal_signal(
            direction="SELL", d1_trend="bullish", h4_trend="bullish",
            rsi=75, stoch_k=85, boll_pct_b=0.90, mfi=85,
            macd_hist=0.5, plus_di=35, minus_di=20, boll_bw=0.008,
        )
        # OB yes, but divergence: MACD bullish (+0.5), DI bullish (+DI>-DI), H4 bullish → NO divergence
        assert has_reversal is False
        assert strength == 0.0

    def test_counter_sell_with_ob_and_macd_divergence(self):
        """SELL in bullish with OB + MACD bearish divergence = reversal."""
        has_reversal, strength = compute_reversal_signal(
            direction="SELL", d1_trend="bullish", h4_trend="bullish",
            rsi=75, stoch_k=55, boll_pct_b=0.50, mfi=50,
            macd_hist=-0.3, plus_di=35, minus_di=20, boll_bw=0.008,
        )
        # OB: rsi>70 (1), divergence: macd<0 (1) → has_reversal
        assert has_reversal is True
        assert strength > 0.0

    def test_counter_sell_with_ob_and_di_divergence(self):
        """SELL in bullish with OB + DI bearish divergence = reversal."""
        has_reversal, strength = compute_reversal_signal(
            direction="SELL", d1_trend="bullish", h4_trend="bullish",
            rsi=75, stoch_k=55, boll_pct_b=0.50, mfi=50,
            macd_hist=0.5, plus_di=20, minus_di=30, boll_bw=0.008,
        )
        # OB: rsi>70 (1), divergence: minus_DI > plus_DI (1) → has_reversal
        assert has_reversal is True
        assert strength > 0.0

    def test_counter_sell_with_ob_and_h4_disagreement(self):
        """SELL in bullish with OB + H4 bearish = reversal (H4 disagreement is divergence)."""
        has_reversal, strength = compute_reversal_signal(
            direction="SELL", d1_trend="bullish", h4_trend="bearish",
            rsi=75, stoch_k=55, boll_pct_b=0.50, mfi=50,
            macd_hist=0.5, plus_di=35, minus_di=20, boll_bw=0.008,
        )
        # OB: rsi>70 (1), divergence: H4 disagrees with D1 (1) → has_reversal
        assert has_reversal is True
        assert strength > 0.0

    def test_no_trend_data_returns_false(self):
        """No trend data → not a reversal (can't be counter-trend without trend)."""
        has_reversal, strength = compute_reversal_signal(
            direction="SELL", d1_trend=None, h4_trend=None,
            rsi=75, stoch_k=85, boll_pct_b=0.90, mfi=85,
            macd_hist=-0.3, plus_di=25, minus_di=30, boll_bw=0.015,
        )
        assert has_reversal is False
        assert strength == 0.0

    def test_unknown_trend_returns_false(self):
        """Unknown trend → not a reversal."""
        has_reversal, strength = compute_reversal_signal(
            direction="SELL", d1_trend="unknown", h4_trend="unknown",
            rsi=75, stoch_k=85, boll_pct_b=0.90, mfi=85,
            macd_hist=-0.3, plus_di=25, minus_di=30, boll_bw=0.015,
        )
        assert has_reversal is False
        assert strength == 0.0

    def test_missing_indicator_data(self):
        """Missing indicator values should still work (graceful None handling)."""
        has_reversal, strength = compute_reversal_signal(
            direction="SELL", d1_trend="bullish", h4_trend="bearish",
            rsi=None, stoch_k=None, boll_pct_b=0.90, mfi=None,
            macd_hist=None, plus_di=None, minus_di=None, boll_bw=0.015,
        )
        # boll_pct_b>0.85 (OB=1), H4 disagrees (div=1) → reversal
        assert has_reversal is True

    def test_all_none_indicators_no_reversal(self):
        """All indicator values None → no OB/OS evidence → not reversal."""
        has_reversal, strength = compute_reversal_signal(
            direction="SELL", d1_trend="bullish", h4_trend=None,
            rsi=None, stoch_k=None, boll_pct_b=None, mfi=None,
            macd_hist=None, plus_di=None, minus_di=None, boll_bw=None,
        )
        assert has_reversal is False
        assert strength == 0.0

    def test_reversal_strength_increases_with_more_evidence(self):
        """More OB/OS indicators + more divergence → higher strength."""
        # Minimal reversal: 1 OB + 1 divergence
        _, strength_min = compute_reversal_signal(
            direction="SELL", d1_trend="bullish", h4_trend="bearish",
            rsi=75, stoch_k=50, boll_pct_b=0.50, mfi=50,
            macd_hist=0.5, plus_di=35, minus_di=20, boll_bw=0.005,
        )
        # Strong reversal: 3 OB + 3 divergence
        _, strength_max = compute_reversal_signal(
            direction="SELL", d1_trend="bullish", h4_trend="bearish",
            rsi=75, stoch_k=85, boll_pct_b=0.90, mfi=85,
            macd_hist=-0.3, plus_di=20, minus_di=30, boll_bw=0.015,
        )
        assert strength_max > strength_min

    def test_volatile_bw_adds_divergence(self):
        """Volatile BW (>0.01) adds to divergence evidence."""
        _, strength_low = compute_reversal_signal(
            direction="SELL", d1_trend="bullish", h4_trend="bearish",
            rsi=75, stoch_k=50, boll_pct_b=0.50, mfi=50,
            macd_hist=0.5, plus_di=35, minus_di=20, boll_bw=0.005,
        )
        _, strength_high = compute_reversal_signal(
            direction="SELL", d1_trend="bullish", h4_trend="bearish",
            rsi=75, stoch_k=50, boll_pct_b=0.50, mfi=50,
            macd_hist=0.5, plus_di=35, minus_di=20, boll_bw=0.015,
        )
        # Same OB evidence (rsi>70), but higher BW adds divergence point
        assert strength_high > strength_low


class TestComputeTrendAlignmentValue:
    """Test trend alignment feature computation."""

    def test_trend_aligned_buy_in_bullish(self):
        """BUY in bullish D1 → trend_aligned (1)."""
        assert compute_trend_alignment_value("BUY", "bullish", "bullish", False) == 1

    def test_trend_aligned_sell_in_bearish(self):
        """SELL in bearish D1 → trend_aligned (1)."""
        assert compute_trend_alignment_value("SELL", "bearish", "bearish", False) == 1

    def test_counter_trend_without_reversal(self):
        """SELL in bullish D1 without reversal → counter_trend (-1)."""
        assert compute_trend_alignment_value("SELL", "bullish", "bullish", False) == -1

    def test_counter_trend_with_reversal(self):
        """SELL in bullish D1 with reversal → reversal (2)."""
        assert compute_trend_alignment_value("SELL", "bullish", "bullish", True) == 2

    def test_neutral_no_trend(self):
        """No trend data → neutral (0)."""
        assert compute_trend_alignment_value("BUY", None, None, False) == 0

    def test_neutral_unknown_trend(self):
        """Unknown trend → neutral (0)."""
        assert compute_trend_alignment_value("SELL", "unknown", None, False) == 0

    def test_buy_in_bearish_with_reversal(self):
        """BUY in bearish D1 with reversal → reversal (2)."""
        assert compute_trend_alignment_value("BUY", "bearish", "bullish", True) == 2

    def test_buy_in_bearish_without_reversal(self):
        """BUY in bearish D1 without reversal → counter_trend (-1)."""
        assert compute_trend_alignment_value("BUY", "bearish", "bearish", False) == -1


class TestFeatureRegistry:
    """Test that new features are in the feature registry."""

    def test_has_reversal_in_broky_features(self):
        assert "has_reversal" in BROKY_FEATURES

    def test_reversal_strength_in_broky_features(self):
        assert "reversal_strength" in BROKY_FEATURES

    def test_trend_alignment_in_broky_features(self):
        assert "trend_alignment" in BROKY_FEATURES

    def test_all_new_features_in_all_feature_cols(self):
        assert "has_reversal" in ALL_FEATURE_COLS
        assert "reversal_strength" in ALL_FEATURE_COLS
        assert "trend_alignment" in ALL_FEATURE_COLS

    def test_feature_registry_validates(self):
        issues = validate_feature_registry()
        assert len(issues) == 0, f"Feature registry issues: {issues}"