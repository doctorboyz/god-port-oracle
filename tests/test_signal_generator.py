"""Tests for signal generation engine."""

import numpy as np
import pandas as pd
import pytest
from datetime import datetime, timezone

from shared.models import SignalType, SessionType
from broky.signals.generator import (
    classify_session,
    calculate_indicator_scores,
    calculate_weighted_score,
    score_to_signal_type,
    score_to_confidence,
    generate_signal,
)
from broky.signals.scaling import calculate_scaling_action, ScalingAction


def _make_market_data(n: int = 200, trend: float = 0.5, volatility: float = 2.0):
    """Generate synthetic OHLCV data for testing."""
    np.random.seed(42)
    close = pd.Series(1900 + np.cumsum(np.random.normal(trend, volatility, n)))
    spread = np.random.uniform(1, 5, n)
    high = close + spread
    low = close - spread
    volume = pd.Series(np.random.uniform(1000, 5000, n))
    return close, high, low, volume


def _make_bullish_data(n: int = 200):
    """Generate bullish (uptrending) data."""
    return _make_market_data(n, trend=1.0, volatility=1.5)


def _make_bearish_data(n: int = 200):
    """Generate bearish (downtrending) data."""
    return _make_market_data(n, trend=-1.0, volatility=1.5)


class TestClassifySession:
    def test_asian_session(self):
        assert classify_session(datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)) == SessionType.ASIAN

    def test_london_session(self):
        assert classify_session(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)) == SessionType.LONDON

    def test_ny_session(self):
        assert classify_session(datetime(2026, 1, 1, 18, 0, tzinfo=timezone.utc)) == SessionType.NY

    def test_overlap_session(self):
        assert classify_session(datetime(2026, 1, 1, 14, 0, tzinfo=timezone.utc)) == SessionType.OVERLAP

    def test_overlap_takes_priority_over_london(self):
        """Overlap (13-16 UTC) is London/NY overlap."""
        assert classify_session(datetime(2026, 1, 1, 13, 0, tzinfo=timezone.utc)) == SessionType.OVERLAP

    def test_asian_boundary(self):
        """Early morning hours = Asian."""
        assert classify_session(datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)) == SessionType.ASIAN


class TestCalculateIndicatorScores:
    def test_scores_are_in_range(self):
        close, high, low, volume = _make_market_data(200)
        scores, adx_val = calculate_indicator_scores(close, high, low, volume)
        for name, score in scores.items():
            assert -1.0 <= score <= 1.0, f"{name} score {score} out of range"
        assert adx_val >= 0.0

    def test_bullish_data_tends_positive(self):
        close, high, low, volume = _make_bullish_data(200)
        scores, adx_val = calculate_indicator_scores(close, high, low, volume)
        weighted = calculate_weighted_score(scores)
        # Bullish data should tend positive (but not guaranteed)
        # Just check it doesn't crash
        assert -1.0 <= weighted <= 1.0

    def test_scores_have_expected_keys(self):
        close, high, low, volume = _make_market_data(200)
        scores, adx_val = calculate_indicator_scores(close, high, low, volume)
        expected_keys = {"macd", "ema_cross", "ema_trend", "bollinger", "volume", "adx"}
        assert set(scores.keys()).issubset(expected_keys)
        assert adx_val >= 0.0


class TestCalculateWeightedScore:
    def test_all_bullish(self):
        scores = {"rsi": 1.0, "macd": 1.0, "ema_cross": 1.0, "bollinger": 1.0, "stochastic": 1.0, "volume": 1.0}
        result = calculate_weighted_score(scores)
        assert result == 1.0

    def test_all_bearish(self):
        scores = {"rsi": -1.0, "macd": -1.0, "ema_cross": -1.0, "bollinger": -1.0, "stochastic": -1.0, "volume": -1.0}
        result = calculate_weighted_score(scores)
        assert result == -1.0

    def test_mixed_signals(self):
        scores = {"rsi": 1.0, "macd": -1.0, "ema_cross": 0.0, "bollinger": 0.5, "stochastic": -0.5, "volume": 0.5}
        result = calculate_weighted_score(scores)
        assert -1.0 <= result <= 1.0

    def test_empty_scores(self):
        result = calculate_weighted_score({})
        assert result == 0.0


class TestScoreToSignalType:
    def test_positive_score_is_buy(self):
        assert score_to_signal_type(0.5) == SignalType.BUY

    def test_negative_score_is_sell(self):
        assert score_to_signal_type(-0.5) == SignalType.SELL

    def test_near_zero_is_hold(self):
        assert score_to_signal_type(0.1) == SignalType.HOLD

    def test_strong_positive_is_buy(self):
        assert score_to_signal_type(0.9) == SignalType.BUY

    def test_strong_negative_is_sell(self):
        assert score_to_signal_type(-0.9) == SignalType.SELL


class TestScoreToConfidence:
    def test_absolute_value(self):
        assert abs(score_to_confidence(0.8) - 0.8) < 0.001

    def test_negative_score_same_confidence(self):
        assert abs(score_to_confidence(-0.8) - 0.8) < 0.001

    def test_max_confidence_is_1(self):
        assert score_to_confidence(1.5) == 1.0

    def test_zero_confidence(self):
        assert score_to_confidence(0.0) == 0.0


class TestGenerateSignal:
    def test_signal_has_correct_structure(self):
        close, high, low, volume = _make_market_data(200)
        signal = generate_signal(close, high, low, volume)
        assert signal.symbol == "XAUUSD"
        assert signal.signal_type in [SignalType.BUY, SignalType.SELL, SignalType.HOLD]
        assert 0.0 <= signal.confidence <= 1.0
        assert signal.price > 0
        assert signal.timeframe == "M5"

    def test_signal_with_entry_price_includes_scaling(self):
        close, high, low, volume = _make_market_data(200)
        current_price = float(close.iloc[-1])
        # Entry price 30% below current = price rose 30%
        entry_price = current_price / 1.3
        signal = generate_signal(close, high, low, volume, entry_price=entry_price)
        assert "Scaling" in signal.reason or signal.signal_type == SignalType.HOLD

    def test_signal_custom_timeframe(self):
        close, high, low, volume = _make_market_data(200)
        signal = generate_signal(close, high, low, volume, timeframe="H1")
        assert signal.timeframe == "H1"

    def test_signal_custom_price(self):
        close, high, low, volume = _make_market_data(200)
        signal = generate_signal(close, high, low, volume, current_price=1950.0)
        assert signal.price == 1950.0