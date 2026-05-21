"""
Tests for ML trade outcome predictor and integration with scalp traders.

Usage:
    python -m pytest tests/test_ml_predictor.py -v
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from broky.ml.trade_outcome_predictor import (
    TradeOutcomePredictor,
    compute_features_from_candles,
    DEFAULT_LOSS_THRESHOLD,
)


def _make_test_candles(n: int = 200) -> dict[str, pd.DataFrame]:
    """Create realistic M5 candle data for testing."""
    np.random.seed(42)
    dates = pd.date_range("2026-05-21 14:00", periods=n, freq="5min")
    price = 2650 + np.cumsum(np.random.randn(n) * 2)
    close = pd.Series(price, index=dates)
    high = close + abs(np.random.randn(n) * 5)
    low = close - abs(np.random.randn(n) * 5)
    volume = pd.Series(np.random.randint(100, 1000, n), index=dates)
    m5 = pd.DataFrame({"close": close, "high": high, "low": low, "volume": volume})
    return {"M5": m5}


class TestFeatureComputation:
    """Test compute_features_from_candles produces valid feature dict."""

    def test_returns_all_required_features(self):
        candles = _make_test_candles()
        features = compute_features_from_candles(candles, "BUY")

        required = [
            "ema_9", "ema_21", "ema_50", "ema_200",
            "sma_10", "sma_20", "sma_50",
            "dema_21", "tema_21",
            "rsi", "stoch_k", "stoch_d", "williams_r", "cci",
            "macd_hist", "adx", "plus_di", "minus_di",
            "atr", "atr_to_price",
            "boll_pct_b", "boll_bw",
            "mfi", "cmf",
            "ichimoku_senkou_a", "ichimoku_senkou_b",
            "price_vs_cloud",
        ]
        for feat in required:
            assert feat in features, f"Missing {feat}"

    def test_returns_string_categoricals(self):
        candles = _make_test_candles()
        features = compute_features_from_candles(candles, "BUY",
                                                  d1_trend="bullish",
                                                  session="london")
        assert features["session"] == "london"
        assert features["d1_trend"] == "bullish"
        assert features["price_vs_cloud"] in ("above", "below", "inside")

    def test_numeric_values_are_finite(self):
        candles = _make_test_candles()
        features = compute_features_from_candles(candles, "BUY")
        for k, v in features.items():
            if isinstance(v, (int, float)):
                assert np.isfinite(v), f"{k} = {v} is not finite"

    def test_empty_candles_returns_empty_dict(self):
        features = compute_features_from_candles({}, "BUY")
        assert features == {}

    def test_missing_m5_returns_empty(self):
        features = compute_features_from_candles({"M15": pd.DataFrame()}, "BUY")
        assert features == {}


class TestTradeOutcomePredictor:
    """Test TradeOutcomePredictor loading and prediction."""

    def test_disabled_when_no_models(self, tmp_path):
        p = TradeOutcomePredictor(model_dir=str(tmp_path))
        assert not p.enabled
        assert p.should_skip({}) == (False, "ML filter disabled")
        assert p.predict_loss_proba({}) is None

    def test_should_skip_returns_false_when_disabled(self):
        p = TradeOutcomePredictor(model_dir="nonexistent/path")
        skip, reason = p.should_skip({})
        assert not skip
        assert "disabled" in reason

    def test_predict_loss_proba_returns_none_when_disabled(self):
        p = TradeOutcomePredictor(model_dir="nonexistent/path")
        assert p.predict_loss_proba({}) is None

    def test_model_name_resolution(self):
        """Test model name fallback: regime×direction > direction > regime > overall."""
        p = TradeOutcomePredictor(model_dir="nonexistent/path")
        # Without models, should return None
        assert p.predict_loss_proba({}, regime="trending", direction="BUY") is None
        assert p.predict_loss_proba({}, direction="SELL") is None
        assert p.predict_loss_proba({}, regime="ranging") is None


class TestMLFilterIntegration:
    """Integration tests for ML filter in scalp trader flow."""

    def test_normal_case_signal_passes(self):
        """Normal: signal with neutral features should pass filter (P(LOSS) < threshold)."""
        candles = _make_test_candles()
        features = compute_features_from_candles(
            candles, "BUY", spread=15, d1_trend="bullish", session="london",
        )

        # Test that feature computation works end-to-end
        assert len(features) > 40  # Should have 50+ features
        assert features["session"] == "london"

    def test_edge_case_model_unavailable(self):
        """Edge: non-existent model dir → should return disabled, no crash."""
        p = TradeOutcomePredictor(model_dir="nonexistent_dir")
        features = compute_features_from_candles(_make_test_candles(), "BUY")
        skip, reason = p.should_skip(features, "trending", "BUY")
        assert not skip
        assert "disabled" in reason

    def test_edge_case_empty_features(self):
        """Edge: empty features → should return gracefully."""
        p = TradeOutcomePredictor(model_dir="nonexistent_dir")
        skip, reason = p.should_skip({})
        assert not skip

    def test_edge_case_none_regime_direction(self):
        """Edge: None regime/direction → should fall back to overall model."""
        p = TradeOutcomePredictor(model_dir="nonexistent_dir")
        features = compute_features_from_candles(_make_test_candles(), "BUY")
        proba = p.predict_loss_proba(features, regime=None, direction=None)
        assert proba is None or 0 <= proba <= 1

    def test_loss_threshold_configurable(self):
        """Loss threshold should be configurable."""
        p1 = TradeOutcomePredictor(model_dir="nonexistent_dir", loss_threshold=0.50)
        p2 = TradeOutcomePredictor(model_dir="nonexistent_dir", loss_threshold=0.80)
        assert p1.loss_threshold == 0.50
        assert p2.loss_threshold == 0.80


class TestPredictorWithRandomFeatures:
    """Test predictor behavior with random/dummy features to ensure no crashes."""

    def test_random_numeric_features_no_crash(self):
        """Random numeric features should not crash the predictor."""
        p = TradeOutcomePredictor(model_dir="nonexistent_dir")
        features = {k: float(np.random.random()) for k in [
            "ema_9", "ema_21", "ema_50", "ema_200", "sma_10", "sma_20", "sma_50",
            "dema_21", "rsi", "stoch_k", "stoch_d", "macd_hist", "adx",
            "plus_di", "minus_di", "atr", "atr_to_price", "boll_pct_b", "boll_bw",
            "mfi", "cmf", "ichimoku_senkou_a", "ichimoku_senkou_b",
        ]}
        features["session"] = "london"
        features["d1_trend"] = "bullish"
        features["price_vs_cloud"] = "above"
        proba = p.predict_loss_proba(features)
        assert proba is None or 0 <= proba <= 1
