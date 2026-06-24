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

# ═══════════════════════════════════════════════════════════════════════════
# V12 features — session-aware, candle patterns, multi-TF alignment, combos
# These are critical for BUY model performance (BUY WR varies 19-86% by hour)
# ═══════════════════════════════════════════════════════════════════════════

# Session cyclical features — captures hour-of-day and day-of-week patterns
# BUY at 9h-14h UTC: WR 19-32% (bad); BUY at 16h-20h UTC: WR 67-86% (great)
SESSION_CYCLICAL_FEATURES = [
    "hour_sin", "hour_cos",              # Cyclical encoding of UTC hour
    "day_of_week_sin", "day_of_week_cos", # Cyclical encoding of day of week
]

# Candle pattern / shape features — close_position is #1 by importance in V11
CANDLE_PATTERN_FEATURES = [
    "close_position",      # (close - low) / (high - low) — #1 V11 feature importance
    "body_ratio",          # |close - open| / (high - low) — candle body proportion
    "upper_shadow_ratio",  # (high - max(open,close)) / (high - low)
    "lower_shadow_ratio",  # (min(open,close) - low) / (high - low)
    "inside_bar",          # 1 if current bar is inside previous bar
    "outside_bar",         # 1 if current bar engulfs previous bar
    "doji",                # 1 if body_ratio < 0.1 (indecision candle)
    "direction_streak",    # Consecutive bars in same direction (signed)
]

# Momentum / rate-of-change variants
MOMENTUM_FEATURES = [
    "roc_4",               # 4-bar rate of change
    "roc_12",              # 12-bar rate of change
    "roc_24",              # 24-bar rate of change
    "ema_momentum_9_21",   # ema_9 / ema_21 - 1 (short vs medium momentum)
    "ema_momentum_50_200", # ema_50 / ema_200 - 1 (medium vs long momentum)
]

# Volatility / risk features
VOLATILITY_FEATURES = [
    "vol_of_vol_20",       # 20-bar rolling std of ATR pct change
    "volume_acceleration", # Volume rate of change (acceleration)
    "atr_pct_change_4",    # 4-bar % change in ATR
    "rolling_sharpe_20",   # 20-bar rolling Sharpe ratio
    "rolling_sortino_20",  # 20-bar rolling Sortino ratio
]

# Multi-timeframe alignment features — captures trend consistency across TFs
MULTI_TF_ALIGNMENT_FEATURES = [
    "h4_ema9", "h4_ema21", "h4_ema50",   # H4 EMAs
    "d1_ema9", "d1_ema21", "d1_ema50",    # D1 EMAs
    "price_vs_h4_ema50",  # Price relative to H4 EMA50
    "price_vs_d1_ema50",  # Price relative to D1 EMA50
    "h4_rsi", "d1_rsi",  # H4/D1 RSI
    "h4_ema_alignment",   # H4 EMA trend direction (-1, 0, 1)
    "d1_ema_alignment",   # D1 EMA trend direction (-1, 0, 1)
    "h1_h4_aligned",      # H1 and H4 trend alignment (1=aligned, 0=not)
    "h4_d1_aligned",      # H4 and D1 trend alignment
    "all_tf_aligned",     # All timeframes aligned (1=yes, 0=no)
    "trending_combo",     # Combined trend strength signal
    "rsi_h1_h4_aligned",  # RSI alignment between H1 and H4
    "rsi_h1_d1_aligned",  # RSI alignment between H1 and D1
]

# Combo features — interaction terms between indicators
COMBO_FEATURES = [
    "rsi_adx_combo",       # RSI * ADX / 100 — momentum × trend strength
    "ema_cross_volume",    # EMA crossover × volume ratio
    "boll_rsi_combo",      # Bollinger position × RSI — overbought/oversold confirmation
    "atr_direction_combo", # ATR × direction (signed volatility)
    "adx_volume_combo",   # ADX × volume — trending with volume
    "macd_adx_combo",     # MACD histogram × ADX — momentum in trend
    "price_above_ema_combo", # Composite of price above EMA9/21/50/200
]

# Candlestick pattern flags
CANDLESTICK_PATTERN_FEATURES = [
    "bearish_engulfing",   # Bearish engulfing pattern
    "shooting_star",       # Shooting star (bearish reversal)
    "evening_star",        # Evening star (bearish reversal)
    "bear_pin_bar",        # Bearish pin bar
    "upper_rejection",    # Upper wick rejection
]

# Fear & Greed extended features
FEAR_GREED_EXTENDED_FEATURES = [
    "fear_greed_change",   # Day-over-day change in fear/greed
    "fear_greed_ma_7d",   # 7-day moving average
    "fear_greed_ma_30d",  # 30-day moving average
    "fear_greed_zscore",  # Z-score of current value
    "fear_greed_extreme",  # 1 if extreme fear/greed (<=20 or >=80)
    "fear_greed_regime",  # Discrete regime: fear/neutral/greed
]

# Zone / regime classification features
ZONE_FEATURES = [
    "rsi_zone",           # RSI zone: oversold(1)/neutral(0)/overbought(-1)
    "boll_zone",          # Bollinger zone: lower(1)/middle(0)/upper(-1)
    "vol_regime",         # Volatility regime: low(0)/medium(1)/high(2)
]

# Derived numeric features (beyond the 3 basic ones)
V12_DERIVED_FEATURES = [
    "price_momentum_5",    # 5-bar price momentum
    "normalized_range",    # (high - low) / ATR — range normalized by volatility
    "stoch_divergence",   # Stochastic divergence signal
    "adx_squared",        # ADX² — emphasizes strong trends
    "macd_cross",         # MACD crossover signal
]

# Open interest
OPEN_INTEREST_FEATURE = ["open_interest"]

# Funding rate features (crypto-specific, may not be available for XAUUSD)
FUNDING_RATE_FEATURES = [
    "funding_rate",
    "funding_rate_ma8",
    "funding_rate_change",
    "funding_rate_regime",
    "funding_rate_abs",
    "funding_rate_zscore",
]

ALL_NUMERIC_FEATURES = (
    VOLUME_FEATURES + OB_OS_FEATURES + MA_FEATURES + SENTIMENT_FEATURES +
    BROKY_FEATURES + EXTERNAL_SENTIMENT_FEATURES + MULTI_TF_PRICE_FEATURES +
    SESSION_CYCLICAL_FEATURES + CANDLE_PATTERN_FEATURES + MOMENTUM_FEATURES +
    VOLATILITY_FEATURES + MULTI_TF_ALIGNMENT_FEATURES + COMBO_FEATURES +
    CANDLESTICK_PATTERN_FEATURES + FEAR_GREED_EXTENDED_FEATURES + ZONE_FEATURES +
    V12_DERIVED_FEATURES + OPEN_INTEREST_FEATURE + FUNDING_RATE_FEATURES
)

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

        # ═══ V12 derived features ════════════════════════════════════════════
        # Session cyclical — hour of day and day of week
        if "hour" in result.columns:
            hour_rad = result["hour"] * (2 * np.pi / 24)
            result["hour_sin"] = np.sin(hour_rad)
            result["hour_cos"] = np.cos(hour_rad)
        if "day_of_week" in result.columns:
            dow_rad = result["day_of_week"] * (2 * np.pi / 7)
            result["day_of_week_sin"] = np.sin(dow_rad)
            result["day_of_week_cos"] = np.cos(dow_rad)

        # Candle patterns — shape of current bar
        if all(c in result.columns for c in ["close", "high", "low", "open"]):
            hl_range = result["high"] - result["low"]
            hl_range = hl_range.replace(0, np.nan)  # avoid div-by-zero
            result["close_position"] = ((result["close"] - result["low"]) / hl_range).fillna(0.5)
            result["body_ratio"] = (result["close"] - result["open"]).abs() / hl_range.fillna(1)
            result["body_ratio"] = result["body_ratio"].clip(0, 1)
            result["upper_shadow_ratio"] = (result["high"] - result[["open", "close"]].max(axis=1)) / hl_range.fillna(1)
            result["lower_shadow_ratio"] = (result[["open", "close"]].min(axis=1) - result["low"]) / hl_range.fillna(1)
            result["doji"] = (result["body_ratio"] < 0.1).astype(int)
            # Inside/outside bar need previous bar data — set to 0 if not available
            if "prev_high" in result.columns and "prev_low" in result.columns:
                result["inside_bar"] = ((result["high"] <= result["prev_high"]) & (result["low"] >= result["prev_low"])).astype(int)
                result["outside_bar"] = ((result["high"] >= result["prev_high"]) & (result["low"] <= result["prev_low"])).astype(int)
            else:
                result["inside_bar"] = 0
                result["outside_bar"] = 0

        # Direction streak — consecutive bars in same direction
        if all(c in result.columns for c in ["close"]):
            direction = (result["close"].diff() > 0).astype(int) - (result["close"].diff() < 0).astype(int)
            # Count consecutive same-direction bars
            groups = (direction != direction.shift()).cumsum()
            result["direction_streak"] = direction.groupby(groups).cumcount() + 1
            result["direction_streak"] = result["direction_streak"] * direction  # sign the streak

        # Momentum features
        if "close" in result.columns:
            for period, name in [(4, "roc_4"), (12, "roc_12"), (24, "roc_24")]:
                result[name] = result["close"].pct_change(period)
            if all(c in result.columns for c in ["ema_9", "ema_21"]):
                result["ema_momentum_9_21"] = result["ema_9"] / result["ema_21"] - 1
            if all(c in result.columns for c in ["ema_50", "ema_200"]):
                result["ema_momentum_50_200"] = result["ema_50"] / result["ema_200"] - 1

        # Volatility features
        if "atr" in result.columns:
            result["atr_pct_change_4"] = result["atr"].pct_change(4)
            result["vol_of_vol_20"] = result["atr"].pct_change().rolling(20).std()
        if all(c in result.columns for c in ["tick_volume_ratio", "volume_roc"]):
            result["volume_acceleration"] = result["volume_roc"].diff()

        # Rolling risk metrics (need close and returns)
        if "close" in result.columns:
            returns = result["close"].pct_change()
            result["rolling_sharpe_20"] = returns.rolling(20).mean() / (returns.rolling(20).std() + 1e-8)
            downside = returns.where(returns < 0, 0)
            result["rolling_sortino_20"] = returns.rolling(20).mean() / (downside.rolling(20).std() + 1e-8)

        # Combo features
        if all(c in result.columns for c in ["rsi", "adx"]):
            result["rsi_adx_combo"] = result["rsi"] * result["adx"] / 100
        if all(c in result.columns for c in ["ema_9", "ema_21", "tick_volume_ratio"]):
            cross = (result["ema_9"] > result["ema_21"]).astype(int)
            result["ema_cross_volume"] = cross * result["tick_volume_ratio"]
        if all(c in result.columns for c in ["boll_pct_b", "rsi"]):
            result["boll_rsi_combo"] = result["boll_pct_b"] * result["rsi"] / 100
        if all(c in result.columns for c in ["atr", "direction_streak"]):
            result["atr_direction_combo"] = result["atr"] * result.get("direction_streak", 0)
        if all(c in result.columns for c in ["adx", "tick_volume_ratio"]):
            result["adx_volume_combo"] = result["adx"] * result["tick_volume_ratio"] / 100
        if all(c in result.columns for c in ["macd_hist", "adx"]):
            result["macd_adx_combo"] = result["macd_hist"] * result["adx"] / 100

        # Price vs EMA combo
        ema_cols = [c for c in ["ema_9", "ema_21", "ema_50", "ema_200"] if c in result.columns]
        if len(ema_cols) >= 2 and "close" in result.columns:
            result["price_above_ema_combo"] = sum(
                (result["close"] > result[c]).astype(int) for c in ema_cols
            ) / len(ema_cols)

        # Zone classification features
        if "rsi" in result.columns:
            result["rsi_zone"] = result["rsi"].apply(
                lambda x: 1 if x <= 30 else (-1 if x >= 70 else 0)
            )
        if "boll_pct_b" in result.columns:
            result["boll_zone"] = result["boll_pct_b"].apply(
                lambda x: 1 if x <= 0.15 else (-1 if x >= 0.85 else 0)
            )
        if all(c in result.columns for c in ["adx", "boll_bw"]):
            adx_median = result["adx"].median() if len(result["adx"]) > 0 else 25
            result["vol_regime"] = result["adx"].apply(
                lambda x: 2 if x > 35 else (0 if x < 15 else 1)
            )

        # V12 derived numeric features
        if "close" in result.columns:
            result["price_momentum_5"] = result["close"].pct_change(5)
        if all(c in result.columns for c in ["high", "low", "atr"]):
            result["normalized_range"] = (result["high"] - result["low"]) / (result["atr"] + 1e-8)
        if all(c in result.columns for c in ["stoch_k", "stoch_d"]):
            result["stoch_divergence"] = (result["stoch_k"] - result["stoch_d"]).abs() * (result["stoch_k"] > 80).astype(int) * -1 + \
                                          (result["stoch_k"] - result["stoch_d"]).abs() * (result["stoch_k"] < 20).astype(int)
        if "adx" in result.columns:
            result["adx_squared"] = result["adx"] ** 2
        if "macd_hist" in result.columns:
            result["macd_cross"] = (result["macd_hist"] > 0).astype(int).diff().fillna(0)

        # Multi-TF alignment features (computed if H4/D1 data available)
        for tf_ema in ["h4_ema9", "h4_ema21", "h4_ema50", "d1_ema9", "d1_ema21", "d1_ema50"]:
            if tf_ema not in result.columns:
                result[tf_ema] = 0.0  # Default if H4/D1 data not available

        if all(c in result.columns for c in ["close", "h4_ema50"]):
            result["price_vs_h4_ema50"] = (result["close"] - result["h4_ema50"]) / (result["h4_ema50"] + 1e-8)
        if all(c in result.columns for c in ["close", "d1_ema50"]):
            result["price_vs_d1_ema50"] = (result["close"] - result["d1_ema50"]) / (result["d1_ema50"] + 1e-8)

        for rsi_col in ["h4_rsi", "d1_rsi"]:
            if rsi_col not in result.columns:
                result[rsi_col] = 50.0  # Neutral default

        # TF alignment signals (default 0 if H4/D1 not available)
        for align_col in ["h4_ema_alignment", "d1_ema_alignment", "h1_h4_aligned",
                          "h4_d1_aligned", "all_tf_aligned", "trending_combo",
                          "rsi_h1_h4_aligned", "rsi_h1_d1_aligned"]:
            if align_col not in result.columns:
                result[align_col] = 0

        # Candlestick patterns (simplified detection)
        if all(c in result.columns for c in ["close", "open", "high", "low"]):
            body = (result["close"] - result["open"]).abs()
            upper_wick = result["high"] - result[["open", "close"]].max(axis=1)
            lower_wick = result[["open", "close"]].min(axis=1) - result["low"]
            hl = result["high"] - result["low"]

            # Bearish engulfing: current red bar engulfs previous green bar
            is_red = result["close"] < result["open"]
            prev_green = result["close"].shift(1) > result["open"].shift(1)
            result["bearish_engulfing"] = (is_red & prev_green & (result["close"] < result["open"].shift(1)) & (result["open"] > result["close"].shift(1))).astype(int)

            # Shooting star: small body, long upper wick, small lower wick
            result["shooting_star"] = ((upper_wick > 2 * body) & (lower_wick < body) & is_red).astype(int)

            # Bear pin bar: long lower wick, small body at top
            result["bear_pin_bar"] = 0  # Simplified — would need more context

            # Upper rejection: long upper wick relative to body
            result["upper_rejection"] = ((upper_wick > 2 * body) & (body > 0)).astype(int)

        # Evening star needs 3 bars — simplified
        result["evening_star"] = 0  # Placeholder — computed in backfill with more context

        # Fear & Greed extended (defaults if not available)
        for fg_col in ["fear_greed_change", "fear_greed_ma_7d", "fear_greed_ma_30d",
                       "fear_greed_zscore", "fear_greed_extreme", "fear_greed_regime"]:
            if fg_col not in result.columns:
                result[fg_col] = 0.0

        # Funding rate (defaults — not available for XAUUSD)
        for fr_col in FUNDING_RATE_FEATURES:
            if fr_col not in result.columns:
                result[fr_col] = 0.0

        # Open interest (default)
        if "open_interest" not in result.columns:
            result["open_interest"] = 0.0

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
        # Numeric features (includes V12 features via ALL_NUMERIC_FEATURES)
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
        # Derived features (v4 + V12)
        for col in ["ema_9_21_diff", "di_diff", "boll_pct_b_clipped"]:
            if col in df.columns:
                cols.append(col)
        return cols