"""Feature engineering for ML pipeline — normalize, encode, and scale indicators.

FEATURE REGISTRY (single source of truth):
All feature lists are derived from the registry below. When adding a new feature,
add it to ONE place in the registry and it propagates to ALL consumers.

Convention:
- Raw categorical features → encoded by FeatureEngineer.transform()
- Encoded features → produced by transform(), listed in ENCODED_FEATURES
- Derived features → produced by transform(), listed in DERIVED_FEATURES
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
# FEATURE REGISTRY — single source of truth for ALL features
# When adding a feature, add it HERE and it propagates everywhere.
# ═══════════════════════════════════════════════════════════════════════════

# Raw categorical features that need encoding (string values → numeric)
CATEGORICAL_FEATURES = {"price_vs_cloud", "session", "d1_trend", "h4_trend", "mfi_signal", "regime"}

# Numeric features organized by group
VOLUME_FEATURES = [
    "obv", "obv_slope", "mfi", "vwap_offset_pct", "volume_roc",
    "ad_line", "ad_line_slope", "cmf",
]

OB_OS_FEATURES = [
    "rsi", "stoch_k", "stoch_d", "williams_r", "cci", "demarker", "roc",
]

MA_FEATURES = [
    "sma_10", "sma_20", "sma_50",
    "ema_9", "ema_21", "ema_50", "ema_200",
    "dema_21", "tema_21",
    "ichimoku_tenkan", "ichimoku_kijun", "ichimoku_senkou_a", "ichimoku_senkou_b",
    "ichimoku_chikou",
]

SENTIMENT_FEATURES = [
    "tick_volume_ratio", "spread_ratio", "long_short_ratio", "session_strength",
]

BROKY_FEATURES = [
    "macd_hist", "adx", "plus_di", "minus_di", "boll_pct_b", "boll_bw",
    "atr", "atr_to_price",
    "has_reversal", "reversal_strength", "trend_alignment",
]

EXTERNAL_SENTIMENT_FEATURES = [
    "fear_greed_value", "gold_bias_strength", "news_sentiment",
]

# Multi-timeframe price context features
MULTI_TF_PRICE_FEATURES = [
    "h1_close", "h4_close", "d1_close", "m5_high", "m5_low",
]

ALL_NUMERIC_FEATURES = VOLUME_FEATURES + OB_OS_FEATURES + MA_FEATURES + SENTIMENT_FEATURES + BROKY_FEATURES + EXTERNAL_SENTIMENT_FEATURES + MULTI_TF_PRICE_FEATURES

# Encoded categorical features — produced by FeatureEngineer.transform()
# Maps raw categorical → encoded numeric. Must include ALL categoricals.
ENCODED_CATEGORICAL_MAP = {
    "price_vs_cloud": "price_vs_cloud_encoded",  # above=1, inside=0, below=-1
    "d1_trend": "d1_trend_encoded",              # bullish=1, bearish=-1, other=0
    "h4_trend": "h4_trend_encoded",               # bullish=1, bearish=-1, other=0
    "mfi_signal": "mfi_signal_encoded",           # oversold=1, neutral=0, overbought=-1
    "regime": "regime_encoded",                   # trending=1, ranging=0, volatile=2 (ordinal, kept for v4 compat)
    # One-hot regime columns are added separately in transform() for v6+
}

# Derived features — computed by FeatureEngineer.transform() from other features
DERIVED_FEATURES = [
    "ema_9_21_diff",       # ema_9 - ema_21
    "di_diff",             # plus_di - minus_di
    "boll_pct_b_clipped",  # boll_pct_b clipped to [0, 1]
]

# One-hot regime columns produced by FeatureEngineer.transform() (v6+)
REGIME_ONEHOT_FEATURES = ["regime_trending", "regime_ranging", "regime_volatile"]

# Full list of all encoded/derived features produced by FeatureEngineer
# (excludes one-hot session columns which are dynamic)
# NOTE: session_strength is already in ALL_NUMERIC_FEATURES (via SENTIMENT_FEATURES),
# so we don't duplicate it here. ENCODED_FEATURES lists features that are ONLY
# produced by FeatureEngineer.transform() and NOT in ALL_NUMERIC_FEATURES.
ENCODED_FEATURES_ONLY = list(ENCODED_CATEGORICAL_MAP.values()) + REGIME_ONEHOT_FEATURES + DERIVED_FEATURES
# For backward compat: the full list including session_strength (used by trainer)
ENCODED_FEATURES = ENCODED_FEATURES_ONLY + ["session_strength"]

# Complete feature list for training.
# Uses dict.fromkeys to deduplicate while preserving order
# (session_strength appears in both SENTIMENT_FEATURES and ENCODED_FEATURES)
ALL_FEATURE_COLS = list(dict.fromkeys(
    ALL_NUMERIC_FEATURES + list(CATEGORICAL_FEATURES) + ENCODED_FEATURES
))


def validate_feature_registry() -> list[str]:
    """Validate feature registry consistency.

    Returns list of issues found. Empty list = all good.
    """
    issues = []

    # Check ENCODED_CATEGORICAL_MAP covers ALL categoricals
    for cat in CATEGORICAL_FEATURES:
        if cat not in ENCODED_CATEGORICAL_MAP and cat != "session":
            issues.append(f"Categorical '{cat}' missing from ENCODED_CATEGORICAL_MAP")

    # Check all encoded features are in ALL_FEATURE_COLS
    for feat in ENCODED_FEATURES:
        if feat not in ALL_FEATURE_COLS:
            issues.append(f"Encoded feature '{feat}' missing from ALL_FEATURE_COLS")

    # Check no duplicates in ALL_FEATURE_COLS
    seen = set()
    for feat in ALL_FEATURE_COLS:
        if feat in seen:
            issues.append(f"Duplicate feature '{feat}' in ALL_FEATURE_COLS")
        seen.add(feat)

    # Check derived features have their source features available
    derived_deps = {
        "ema_9_21_diff": ["ema_9", "ema_21"],
        "di_diff": ["plus_di", "minus_di"],
        "boll_pct_b_clipped": ["boll_pct_b"],
    }
    for derived, deps in derived_deps.items():
        for dep in deps:
            if dep not in ALL_NUMERIC_FEATURES:
                issues.append(f"Derived feature '{derived}' depends on '{dep}' which is not in ALL_NUMERIC_FEATURES")

    return issues


class FeatureEngineer:
    """Normalizes and encodes raw indicator values for ML training."""

    def __init__(self, fillna: bool = True):
        self.fillna = fillna
        self._medians: dict[str, float] = {}
        self._session_columns: list[str] = []
        self._feature_columns: Optional[list[str]] = None

    def fit(self, df: pd.DataFrame) -> "FeatureEngineer":
        """Compute median values for NaN filling from training data."""
        numeric_cols = [c for c in ALL_NUMERIC_FEATURES if c in df.columns]
        self._medians = {}
        for col in numeric_cols:
            median_val = df[col].median()
            if pd.notna(median_val):
                self._medians[col] = float(median_val)
            else:
                self._medians[col] = 0.0

        # Transform once to discover session/feature columns
        transformed = self.transform(df)
        self._session_columns = [c for c in transformed.columns if c.startswith("session_") and c != "session_strength"]
        self._feature_columns = self._compute_feature_columns(transformed)
        return self

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        """Apply feature engineering transformations."""
        result = df.copy()

        # Encode price_vs_cloud: above=1, inside=0, below=-1
        if "price_vs_cloud" in result.columns:
            cloud_map = {"above": 1, "inside": 0, "below": -1}
            result["price_vs_cloud_encoded"] = result["price_vs_cloud"].map(cloud_map).fillna(0).astype(int)

        # Encode session: one-hot (ensure all training session columns exist)
        if "session" in result.columns:
            session_dummies = pd.get_dummies(result["session"], prefix="session", dtype=int)
            result = pd.concat([result, session_dummies], axis=1)
            # Add missing session columns from training (fill with 0)
            for col in getattr(self, "_session_columns", []):
                if col not in result.columns:
                    result[col] = 0

        # Encode d1_trend: bullish=1, bearish=-1, unknown=0
        if "d1_trend" in result.columns:
            trend_map = {"bullish": 1, "bearish": -1}
            result["d1_trend_encoded"] = result["d1_trend"].map(trend_map).fillna(0).astype(int)

        # Encode h4_trend: bullish=1, bearish=-1, unknown=0
        if "h4_trend" in result.columns:
            trend_map = {"bullish": 1, "bearish": -1}
            result["h4_trend_encoded"] = result["h4_trend"].map(trend_map).fillna(0).astype(int)

        # Encode mfi_signal: oversold=1, neutral=0, overbought=-1
        if "mfi_signal" in result.columns:
            mfi_map = {"oversold": 1, "neutral": 0, "overbought": -1}
            result["mfi_signal_encoded"] = result["mfi_signal"].map(mfi_map).fillna(0).astype(int)

        # Encode regime: ordinal (v4 compat) + one-hot (v6+)
        # Ordinal: trending=1, ranging=0, volatile=2 — kept for backward compat with v4 model
        # One-hot: regime_trending, regime_ranging, regime_volatile — used by v6+ models
        if "regime" in result.columns:
            regime_map = {"trending": 1, "ranging": 0, "volatile": 2}
            result["regime_encoded"] = result["regime"].map(regime_map).fillna(0).astype(int)
            # One-hot encoding for v6+ models (avoids ordinal assumption)
            result["regime_trending"] = (result["regime"] == "trending").astype(int)
            result["regime_ranging"] = (result["regime"] == "ranging").astype(int)
            result["regime_volatile"] = (result["regime"] == "volatile").astype(int)

        # Fill NaN in numeric features with median (if fitted) or 0
        # Also force all numeric features to float dtype (SQLite may return mixed types)
        if self.fillna:
            for col in ALL_NUMERIC_FEATURES:
                if col in result.columns:
                    fill_val = self._medians.get(col, 0.0)
                    result[col] = pd.to_numeric(result[col], errors="coerce").fillna(fill_val)

        # Compute derived features
        if all(c in result.columns for c in ["ema_9", "ema_21"]):
            result["ema_9_21_diff"] = result["ema_9"] - result["ema_21"]
        if all(c in result.columns for c in ["plus_di", "minus_di"]):
            result["di_diff"] = result["plus_di"] - result["minus_di"]
        if all(c in result.columns for c in ["boll_pct_b"]):
            result["boll_pct_b_clipped"] = result["boll_pct_b"].clip(0, 1)

        return result

    def get_feature_columns(self, df: pd.DataFrame | None = None) -> list[str]:
        """Return ordered list of feature column names for ML.

        Returns cached list from fit() if available, otherwise computes from df.
        """
        if self._feature_columns is not None:
            return self._feature_columns
        if df is not None:
            return self._compute_feature_columns(df)
        return []

    def _compute_feature_columns(self, df: pd.DataFrame) -> list[str]:
        """Compute feature column names from a transformed DataFrame."""
        cols = []
        # Numeric features
        for col in ALL_NUMERIC_FEATURES:
            if col in df.columns:
                cols.append(col)
        # Encoded categorical features
        for col in ["price_vs_cloud_encoded", "d1_trend_encoded", "h4_trend_encoded", "mfi_signal_encoded", "regime_encoded"]:
            if col in df.columns:
                cols.append(col)
        # One-hot regime columns (v6+)
        for col in ["regime_trending", "regime_ranging", "regime_volatile"]:
            if col in df.columns:
                cols.append(col)
        # One-hot session columns (use cached order if available)
        if self._session_columns:
            for col in self._session_columns:
                if col in df.columns:
                    cols.append(col)
        else:
            for col in df.columns:
                if col.startswith("session_") and col != "session_strength":
                    cols.append(col)
        # Derived features
        for col in ["ema_9_21_diff", "di_diff", "boll_pct_b_clipped"]:
            if col in df.columns:
                cols.append(col)
        return cols