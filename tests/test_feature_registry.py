"""Validate feature registry consistency — single source of truth test.

This test ensures that when a feature is added, it propagates to ALL consumers.
If this test fails, the feature pipeline is broken.

Usage:
    python -m pytest tests/test_feature_registry.py -v
"""
from __future__ import annotations

import pytest
import pandas as pd
import numpy as np

from broky.ml.features import (
    FeatureEngineer,
    ALL_FEATURE_COLS,
    ALL_NUMERIC_FEATURES,
    CATEGORICAL_FEATURES,
    ENCODED_CATEGORICAL_MAP,
    ENCODED_FEATURES,
    DERIVED_FEATURES,
    validate_feature_registry,
)


class TestFeatureRegistryConsistency:
    """Validate that feature lists are consistent and complete."""

    def test_registry_has_no_issues(self):
        """validate_feature_registry() should return empty list."""
        issues = validate_feature_registry()
        assert issues == [], f"Feature registry issues: {issues}"

    def test_all_feature_cols_no_duplicates(self):
        """ALL_FEATURE_COLS must have no duplicate entries."""
        assert len(ALL_FEATURE_COLS) == len(set(ALL_FEATURE_COLS)), \
            f"Duplicates in ALL_FEATURE_COLS: {[x for x in ALL_FEATURE_COLS if ALL_FEATURE_COLS.count(x) > 1]}"

    def test_encoded_categorical_map_covers_all_categoricals(self):
        """Every categorical (except session which is one-hot) must have an encoding mapping."""
        for cat in CATEGORICAL_FEATURES:
            if cat == "session":
                continue  # session uses one-hot encoding, not ordinal
            assert cat in ENCODED_CATEGORICAL_MAP, \
                f"Categorical '{cat}' missing from ENCODED_CATEGORICAL_MAP"

    def test_encoded_features_all_in_all_feature_cols(self):
        """Every encoded/derived feature must appear in ALL_FEATURE_COLS."""
        for feat in ENCODED_FEATURES:
            assert feat in ALL_FEATURE_COLS, \
                f"Encoded feature '{feat}' missing from ALL_FEATURE_COLS"

    def test_categorical_cols_match_features_py(self):
        """CATEGORICAL_FEATURES set must match what FeatureEngineer encodes."""
        # FeatureEngineer.encode handles exactly these categoricals
        expected = {"price_vs_cloud", "session", "d1_trend", "h4_trend", "mfi_signal", "regime"}
        assert CATEGORICAL_FEATURES == expected, \
            f"CATEGORICAL_FEATURES mismatch: {CATEGORICAL_FEATURES} != {expected}"

    def test_all_numeric_features_no_duplicates(self):
        """ALL_NUMERIC_FEATURES must have no duplicate entries."""
        assert len(ALL_NUMERIC_FEATURES) == len(set(ALL_NUMERIC_FEATURES)), \
            f"Duplicates in ALL_NUMERIC_FEATURES"

    def test_buy_top_features_all_valid(self):
        """BUY_TOP_FEATURES must only contain features that exist in ALL_FEATURE_COLS or are produced by FeatureEngineer."""
        from broky.ml.trade_outcome_trainer import BUY_TOP_FEATURES
        valid_features = set(ALL_FEATURE_COLS) | set(ENCODED_FEATURES)
        for feat in BUY_TOP_FEATURES:
            assert feat in valid_features, \
                f"BUY_TOP_FEATURES contains unknown feature: '{feat}'"

    def test_sell_top_features_all_valid(self):
        """SELL_TOP_FEATURES must only contain features that exist in ALL_FEATURE_COLS or are produced by FeatureEngineer."""
        from broky.ml.trade_outcome_trainer import SELL_TOP_FEATURES
        valid_features = set(ALL_FEATURE_COLS) | set(ENCODED_FEATURES)
        for feat in SELL_TOP_FEATURES:
            assert feat in valid_features, \
                f"SELL_TOP_FEATURES contains unknown feature: '{feat}'"

    def test_feature_engineer_produces_expected_encoded(self):
        """FeatureEngineer.transform() must produce all expected encoded features."""
        # Create minimal test data
        n = 50
        dates = pd.date_range("2026-01-01", periods=n, freq="1h")
        df = pd.DataFrame({
            "close": np.random.randn(n).cumsum() + 2650,
            "high": np.random.randn(n).cumsum() + 2655,
            "low": np.random.randn(n).cumsum() + 2645,
            "volume": np.random.randint(100, 1000, n),
            "session": np.random.choice(["london", "new_york", "asian", "overlap"], n),
            "d1_trend": np.random.choice(["bullish", "bearish", "neutral"], n),
            "h4_trend": np.random.choice(["bullish", "bearish", "unknown"], n),
            "price_vs_cloud": np.random.choice(["above", "below", "inside"], n),
            "mfi_signal": np.random.choice(["oversold", "overbought", "neutral"], n),
            "regime": np.random.choice(["trending", "ranging", "volatile"], n),
            # Add required numeric features
            "ema_9": np.random.randn(n),
            "ema_21": np.random.randn(n),
            "ema_50": np.random.randn(n),
            "ema_200": np.random.randn(n),
            "sma_10": np.random.randn(n),
            "sma_20": np.random.randn(n),
            "sma_50": np.random.randn(n),
            "dema_21": np.random.randn(n),
            "tema_21": np.random.randn(n),
            "ichimoku_tenkan": np.random.randn(n),
            "ichimoku_kijun": np.random.randn(n),
            "ichimoku_senkou_a": np.random.randn(n),
            "ichimoku_senkou_b": np.random.randn(n),
            "ichimoku_chikou": np.random.randn(n),
            "rsi": np.random.uniform(20, 80, n),
            "stoch_k": np.random.uniform(20, 80, n),
            "stoch_d": np.random.uniform(20, 80, n),
            "williams_r": np.random.uniform(-80, -20, n),
            "cci": np.random.randn(n),
            "demarker": np.random.uniform(0.3, 0.7, n),
            "roc": np.random.randn(n),
            "macd_hist": np.random.randn(n),
            "adx": np.random.uniform(10, 40, n),
            "plus_di": np.random.uniform(15, 30, n),
            "minus_di": np.random.uniform(15, 30, n),
            "boll_pct_b": np.random.uniform(0, 1, n),
            "boll_bw": np.random.uniform(0.01, 0.05, n),
            "atr": np.random.uniform(3, 8, n),
            "atr_to_price": np.random.uniform(0.001, 0.003, n),
            "mfi": np.random.uniform(30, 70, n),
            "obv": np.random.randn(n).cumsum(),
            "obv_slope": np.random.randn(n),
            "tick_volume_ratio": np.random.uniform(0.5, 2, n),
            "volume_roc": np.random.randn(n),
            "ad_line": np.random.randn(n).cumsum(),
            "ad_line_slope": np.random.randn(n),
            "cmf": np.random.uniform(-0.1, 0.1, n),
            "vwap_offset_pct": np.random.randn(n),
            "fear_greed_value": np.random.uniform(30, 70, n),
            "gold_bias_strength": np.random.uniform(30, 70, n),
            "news_sentiment": np.random.uniform(-0.5, 0.5, n),
            "session_strength": np.random.uniform(0.2, 1.0, n),
            "spread_ratio": np.random.uniform(0.5, 1.5, n),
            "long_short_ratio": np.random.uniform(0.8, 1.2, n),
            "h1_close": np.random.randn(n) + 2650,
            "h4_close": np.random.randn(n) + 2650,
            "d1_close": np.random.randn(n) + 2650,
            "m5_high": np.random.randn(n) + 2655,
            "m5_low": np.random.randn(n) + 2645,
        }, index=dates)

        engineer = FeatureEngineer(fillna=True)
        engineer.fit(df)
        transformed = engineer.transform(df)
        feature_cols = engineer.get_feature_columns(transformed)

        # Check all expected encoded features are present
        for cat, encoded in ENCODED_CATEGORICAL_MAP.items():
            if cat == "session":
                continue  # session uses one-hot, not a single column
            assert encoded in transformed.columns, \
                f"FeatureEngineer missing encoded column '{encoded}' for '{cat}'"

        # Check derived features
        for derived in DERIVED_FEATURES:
            assert derived in transformed.columns, \
                f"FeatureEngineer missing derived column '{derived}'"

        # Check get_feature_columns returns reasonable count
        assert len(feature_cols) >= 30, \
            f"get_feature_columns returned only {len(feature_cols)} features (expected 30+)"

    def test_model_feature_count_matches_registry(self):
        """Model training feature count should match ALL_FEATURE_COLS length."""
        from broky.ml.trade_outcome_trainer import ALL_FEATURE_COLS as TRAINER_ALL
        # Trainer's ALL_FEATURE_COLS should match features.py's
        assert len(TRAINER_ALL) == len(ALL_FEATURE_COLS), \
            f"trainer ALL_FEATURE_COLS ({len(TRAINER_ALL)}) != features ALL_FEATURE_COLS ({len(ALL_FEATURE_COLS)})"