#!/usr/bin/env python3
"""Backfill v6 training data from premium M5 indicator parquet.

Generates synthetic trade outcomes by:
1. Loading M5 candles with pre-computed indicators
2. Computing missing features (OBV, CMF, AD line, session, etc.)
3. Generating WIN/LOSS labels based on forward price movement
4. Filtering by trend-following rules (no counter-trend trades)
5. Saving to trade_outcomes table for v6 model training

Usage:
    python scripts/backfill_v6_training_data.py [--min-bars 12] [--db-path data/oracle.db]
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from broky.signals.generator import classify_regime, compute_reversal_signal, compute_trend_alignment_value

logger = logging.getLogger(__name__)

# Minimum forward bars to look ahead for labeling
MIN_FORWARD_BARS = 12

# Threshold for WIN/LOSS (in pips for XAUUSD)
# $3 on $2000 ≈ 0.15% — matches the original label threshold
PIP_THRESHOLD_PCT = 0.15


def load_premium_data(processed_dir: Path = Path("data/processed")) -> pd.DataFrame:
    """Load premium M5 data with all pre-computed indicators."""
    m5_path = processed_dir / "xauusd_m5_indicators.parquet"
    if not m5_path.exists():
        raise FileNotFoundError(
            f"Premium data not found at {m5_path}. "
            "Run: python scripts/process_premium_data.py"
        )

    df = pd.read_parquet(m5_path)
    logger.info("Loaded premium M5 data: %d rows, %s → %s",
                len(df), df.index[0], df.index[-1])
    return df


def compute_missing_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute features that exist in feature_snapshots but not in premium parquet.

    Premium parquet has: ema10, ema20, ema50, ema200, atr, adx, plus_di, minus_di,
    rsi, macd_hist, boll_*, stoch_k, stoch_d, mfi, vol_ema20, vol_ratio, regime,
    d1_trend, h4_trend, has_reversal, reversal_strength, trend_alignment

    Missing from feature_snapshots: sma_10, sma_20, sma_50, ema_9, ema_21, dema_21,
    tema_21, ichimoku_*, obv, obv_slope, ad_line, ad_line_slope, cmf, cci,
    williams_r, roc, demarker, vwap_offset_pct, spread_ratio, long_short_ratio,
    session, session_strength, price_vs_cloud, mfi_signal, h1_close, h4_close,
    d1_close, m5_high, m5_low, balance_at_entry, leverage_at_entry, fear_greed_value,
    gold_bias_strength, news_sentiment, tick_volume_ratio
    """
    result = df.copy()
    close = result["close"].astype(float)
    high = result["high"].astype(float)
    low = result["low"].astype(float)
    volume = result["volume"].astype(float) if "volume" in result.columns else pd.Series(1, index=result.index)

    # ── SMA ──
    for period in [10, 20, 50]:
        col = f"sma_{period}"
        if col not in result.columns:
            result[col] = close.rolling(period).mean()

    # ── EMA 9, 21 ──
    for period in [9, 21]:
        col = f"ema_{period}"
        if col not in result.columns:
            result[col] = close.ewm(span=period, adjust=False).mean()

    # ── DEMA 21 ──
    if "dema_21" not in result.columns:
        ema21 = close.ewm(span=21, adjust=False).mean()
        ema21_ema = ema21.ewm(span=21, adjust=False).mean()
        result["dema_21"] = 2 * ema21 - ema21_ema

    # ── TEMA 21 ──
    if "tema_21" not in result.columns:
        ema1 = close.ewm(span=21, adjust=False).mean()
        ema2 = ema1.ewm(span=21, adjust=False).mean()
        ema3 = ema2.ewm(span=21, adjust=False).mean()
        result["tema_21"] = 3 * ema1 - 3 * ema2 + ema3

    # ── Ichimoku ──
    high9 = high.rolling(9).max()
    low9 = low.rolling(9).min()
    high26 = high.rolling(26).max()
    low26 = low.rolling(26).min()
    high52 = high.rolling(52).max()
    low52 = low.rolling(52).min()

    result["ichimoku_tenkan"] = (high9 + low9) / 2
    result["ichimoku_kijun"] = (high26 + low26) / 2
    result["ichimoku_senkou_a"] = (result["ichimoku_tenkan"] + result["ichimoku_kijun"]) / 2
    result["ichimoku_senkou_b"] = (high52 + low52) / 2
    result["ichimoku_chikou"] = close.shift(26)

    # ── OBV ──
    obv = (np.sign(close.diff()) * volume).cumsum()
    result["obv"] = obv
    result["obv_slope"] = (obv - obv.shift(14)) / 14

    # ── AD Line ──
    clv = ((close - low) - (high - close)) / (high - low + 1e-9)
    ad = (clv * volume).cumsum()
    result["ad_line"] = ad
    result["ad_line_slope"] = (ad - ad.shift(14)) / 14

    # ── CMF ──
    mfv = clv * volume
    result["cmf"] = mfv.rolling(20).sum() / volume.rolling(20).sum().replace(0, 1e-9)

    # ── CCI ──
    tp = (high + low + close) / 3
    sma_tp = tp.rolling(20).mean()
    mad = tp.rolling(20).apply(lambda x: float(np.abs(x - x.mean()).mean()), raw=True)
    result["cci"] = (tp - sma_tp) / (0.015 * mad.replace(0, 1e-9))

    # ── Williams %R ──
    low14 = low.rolling(14).min()
    high14 = high.rolling(14).max()
    result["williams_r"] = -100 * (high14 - close) / (high14 - low14 + 1e-9)

    # ── ROC ──
    result["roc"] = 100 * (close / close.shift(14) - 1)

    # ── DeMarker ──
    de_max = high.diff().where(high.diff() > 0, 0)
    de_min = (-low.diff()).where(low.diff() < 0, 0)
    dem_sum_max = de_max.rolling(13).sum()
    dem_sum_min = de_min.rolling(13).sum()
    result["demarker"] = dem_sum_max / (dem_sum_max + dem_sum_min + 1e-9)

    # ── VWAP offset ──
    typical = (high + low + close) / 3
    vwap = (typical * volume).cumsum() / volume.cumsum().replace(0, 1e-9)
    result["vwap_offset_pct"] = 100 * (close - vwap) / vwap.replace(0, 1e-9)

    # ── Session and session_strength ──
    if hasattr(result.index, "hour"):
        hours = result.index.hour
    else:
        hours = pd.to_datetime(result.index).hour

    result["session_strength"] = hours.map(lambda h:
        1.0 if 13 <= h <= 16 else
        0.7 if 8 <= h <= 16 else
        0.7 if 13 <= h <= 22 else
        0.4 if 0 <= h <= 8 else 0.2
    )
    result["session"] = hours.map(lambda h:
        "london" if 8 <= h <= 16 else
        "new_york" if 13 <= h <= 22 else
        "asian" if 0 <= h <= 8 else "off_hours"
    )

    # ── tick_volume_ratio (use vol_ratio which is volume/SMA20 volume) ──
    if "vol_ratio" in result.columns:
        result["tick_volume_ratio"] = result["vol_ratio"]
    else:
        vol_sma20 = volume.rolling(20).mean()
        result["tick_volume_ratio"] = volume / vol_sma20.replace(0, 1e-9)

    # ── volume_roc ──
    result["volume_roc"] = 100 * (volume / volume.shift(14) - 1)

    # ── price_vs_cloud ──
    senkou_a = result["ichimoku_senkou_a"]
    senkou_b = result["ichimoku_senkou_b"]
    cloud_top = np.maximum(senkou_a, senkou_b)
    cloud_bottom = np.minimum(senkou_a, senkou_b)
    result["price_vs_cloud"] = np.where(
        close > cloud_top, "above",
        np.where(close < cloud_bottom, "below", "inside")
    )

    # ── mfi_signal ──
    mfi = result.get("mfi", pd.Series(50.0, index=result.index))
    result["mfi_signal"] = np.where(mfi < 20, "oversold",
                                    np.where(mfi > 80, "overbought", "neutral"))

    # ── Multi-timeframe price context ──
    result["m5_high"] = high
    result["m5_low"] = low
    # h1_close: resample from M5 to H1
    result["h1_close"] = close.resample("1h").last().reindex(result.index, method="ffill")
    # h4_close and d1_close already present from premium data
    if "h4_close" not in result.columns:
        result["h4_close"] = np.nan
    if "d1_close" not in result.columns:
        result["d1_close"] = np.nan

    # ── Sentiment defaults (not available in backfill) ──
    result["fear_greed_value"] = 50.0
    result["gold_bias_strength"] = 50.0
    result["news_sentiment"] = 0.0

    # ── spread_ratio: (high-low) / rolling_avg(high-low) ──
    bar_range = high - low
    avg_range = bar_range.rolling(20).mean()
    result["spread_ratio"] = bar_range / avg_range.replace(0, 1e-9)

    # ── long_short_ratio: NaN (no broker data) ──
    result["long_short_ratio"] = np.nan

    # ── balance_at_entry, leverage_at_entry: not available ──
    result["balance_at_entry"] = 0.0
    result["leverage_at_entry"] = 0

    # ── Regime (re-classify with boll_bw) ──
    # Premium data already has regime but let's ensure it's via classify_regime()
    if "boll_bw" in result.columns and "adx" in result.columns:
        result["regime"] = result.apply(
            lambda r: classify_regime(r["adx"], r["boll_bw"])
            if pd.notna(r["adx"]) else "ranging",
            axis=1
        )

    # ── EMA 9-21 diff ──
    if "ema_9" in result.columns and "ema_21" in result.columns:
        result["ema_9_21_diff"] = result["ema_9"] - result["ema_21"]

    # ── DI diff ──
    if "plus_di" in result.columns and "minus_di" in result.columns:
        result["di_diff"] = result["plus_di"] - result["minus_di"]

    # ── boll_pct_b_clipped ──
    if "boll_pct_b" in result.columns:
        result["boll_pct_b_clipped"] = result["boll_pct_b"].clip(0, 1)

    # ── Direction (for labeling) ──
    # Use close-to-close direction as proxy for signal direction
    result["_price_diff"] = close.diff()
    result["_direction"] = np.where(result["_price_diff"] > 0, "BUY",
                                     np.where(result["_price_diff"] < 0, "SELL", "FLAT"))

    return result


def generate_trade_outcomes(df: pd.DataFrame, min_forward_bars: int = 24,
                             threshold_pct: float = PIP_THRESHOLD_PCT) -> pd.DataFrame:
    """Generate WIN/LOSS labels using ATR-based dynamic thresholds and signal quality filters.

    Key improvements:
    1. ATR-based dynamic threshold (adapts to volatility regime)
    2. Only label bars where ADX > 18 (real trend exists)
    3. Use DI + momentum + D1 trend for direction (not just price diff)
    4. 24-bar look-ahead (2 hours on M5) for reliable outcomes
    5. Separate TP/SL thresholds (1.5x ATR TP, 1.0x ATR SL)
    """
    n = len(df)
    close = df["close"].values.astype(float)
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    adx_arr = df["adx"].values.astype(float) if "adx" in df.columns else np.full(n, 25.0)
    atr_arr = df["atr"].values.astype(float) if "atr" in df.columns else np.full(n, 5.0)
    regimes = df["regime"].values if "regime" in df.columns else np.full(n, "ranging")
    d1_trends = df["d1_trend"].values if "d1_trend" in df.columns else np.full(n, None)
    h4_trends = df["h4_trend"].values if "h4_trend" in df.columns else np.full(n, None)
    plus_di_arr = df["plus_di"].values.astype(float) if "plus_di" in df.columns else np.full(n, 20.0)
    minus_di_arr = df["minus_di"].values.astype(float) if "minus_di" in df.columns else np.full(n, 20.0)
    macd_hist_arr = df["macd_hist"].values.astype(float) if "macd_hist" in df.columns else np.full(n, 0.0)
    boll_pct_b_arr = df["boll_pct_b"].values.astype(float) if "boll_pct_b" in df.columns else np.full(n, 0.5)
    rsi_arr = df["rsi"].values.astype(float) if "rsi" in df.columns else np.full(n, 50.0)
    stoch_k_arr = df["stoch_k"].values.astype(float) if "stoch_k" in df.columns else np.full(n, 50.0)

    look_ahead = 24  # 2 hours on M5

    labels = np.full(n, np.nan)
    profits = np.full(n, np.nan)
    profit_pcts = np.full(n, np.nan)
    directions = np.full(n, "FLAT", dtype=object)

    stats = {"win": 0, "loss": 0, "skip_counter": 0, "skip_low_adx": 0,
             "skip_no_signal": 0, "buy_win": 0, "buy_loss": 0, "sell_win": 0, "sell_loss": 0}

    for i in range(200, n - look_ahead):
        # ── Skip low-volatility bars (ADX < 18 = no clear trend) ──
        if pd.isna(adx_arr[i]) or adx_arr[i] < 18:
            stats["skip_low_adx"] += 1
            continue

        # ── ATR-based dynamic threshold ──
        # Use 2x ATR for TP and 1x ATR for SL — only label clear outcomes
        if pd.isna(atr_arr[i]) or atr_arr[i] <= 0 or close[i] <= 0:
            continue
        atr_thresh = max(0.20, min(0.60, (atr_arr[i] * 2.0 / close[i]) * 100))
        sl_thresh = max(0.15, min(0.40, (atr_arr[i] * 1.0 / close[i]) * 100))

        # ── Determine direction from indicators (not just price diff) ──
        bullish = 0
        bearish = 0
        if plus_di_arr[i] > minus_di_arr[i]:
            bullish += 1
        else:
            bearish += 1
        if macd_hist_arr[i] > 0:
            bullish += 1
        else:
            bearish += 1
        d1 = d1_trends[i]
        if d1 == "bullish":
            bullish += 2
        elif d1 == "bearish":
            bearish += 2
        boll_val = boll_pct_b_arr[i] if not pd.isna(boll_pct_b_arr[i]) else 0.5
        if boll_val > 0.65:
            bullish += 1
        elif boll_val < 0.35:
            bearish += 1
        # RSI: overbought = bearish signal, oversold = bullish signal
        rsi_val = rsi_arr[i] if not pd.isna(rsi_arr[i]) else 50.0
        if rsi_val > 60:
            bearish += 1
        elif rsi_val < 40:
            bullish += 1

        if bullish > bearish:
            direction = "BUY"
        elif bearish > bullish:
            direction = "SELL"
        else:
            stats["skip_no_signal"] += 1
            continue

        # ── Trend-following filter ──
        h4 = h4_trends[i]
        if direction == "BUY" and d1 == "bearish" and h4 == "bearish":
            stats["skip_counter"] += 1
            continue
        if direction == "SELL" and d1 == "bullish" and h4 == "bullish":
            stats["skip_counter"] += 1
            continue

        # ── Compute outcome from forward bars ──
        current_price = close[i]
        outcome = None

        for j in range(1, look_ahead + 1):
            future_high = high[i + j]
            future_low = low[i + j]

            if direction == "BUY":
                favorable = (future_high - current_price) / current_price * 100
                adverse = (current_price - future_low) / current_price * 100
                if favorable >= atr_thresh:
                    outcome = "WIN"
                    break
                if adverse >= sl_thresh * 1.5:
                    outcome = "LOSS"
                    break
            elif direction == "SELL":
                favorable = (current_price - future_low) / current_price * 100
                adverse = (future_high - current_price) / current_price * 100
                if favorable >= atr_thresh:
                    outcome = "WIN"
                    break
                if adverse >= sl_thresh * 1.5:
                    outcome = "LOSS"
                    break

        # Fallback: use net direction at end of look-ahead
        if outcome is None:
            final_pct = (close[i + look_ahead] - current_price) / current_price * 100
            if direction == "BUY":
                outcome = "WIN" if final_pct > 0 else "LOSS"
                profits[i] = final_pct
            else:
                outcome = "WIN" if final_pct < 0 else "LOSS"
                profits[i] = -final_pct
        else:
            final_pct = (close[i + look_ahead] - current_price) / current_price * 100
            if direction == "BUY":
                profits[i] = final_pct
            else:
                profits[i] = -final_pct

        profit_pcts[i] = profits[i] / 100 if pd.notna(profits[i]) else np.nan
        labels[i] = 1 if outcome == "WIN" else 0
        directions[i] = direction

        if outcome == "WIN":
            stats["win"] += 1
            if direction == "BUY":
                stats["buy_win"] += 1
            else:
                stats["sell_win"] += 1
        else:
            stats["loss"] += 1
            if direction == "BUY":
                stats["buy_loss"] += 1
            else:
                stats["sell_loss"] += 1

    df["outcome_label"] = labels
    df["profit"] = profits
    df["profit_pct"] = profit_pcts
    df["direction"] = directions

    total = stats["win"] + stats["loss"]
    logger.info("Generated labels: WIN=%d, LOSS=%d (total=%d)", stats["win"], stats["loss"], total)
    logger.info("  BUY: %d win, %d loss (WR=%.1f%%)",
                stats["buy_win"], stats["buy_loss"],
                stats["buy_win"] / max(1, stats["buy_win"] + stats["buy_loss"]) * 100)
    logger.info("  SELL: %d win, %d loss (WR=%.1f%%)",
                stats["sell_win"], stats["sell_loss"],
                stats["sell_win"] / max(1, stats["sell_win"] + stats["sell_loss"]) * 100)
    logger.info("  Skipped: counter_trend=%d, low_adx=%d, no_signal=%d",
                stats["skip_counter"], stats["skip_low_adx"], stats["skip_no_signal"])

    return df


def compute_reversal_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute reversal signal features for each row."""
    has_reversal = np.zeros(len(df))
    reversal_strength = np.zeros(len(df))
    trend_alignment = np.zeros(len(df))

    for i in range(200, len(df)):
        row = df.iloc[i]
        direction = row.get("_direction", "BUY")
        if direction == "FLAT":
            direction = "BUY"

        d1_trend = row.get("d1_trend")
        h4_trend = row.get("h4_trend")

        rev, rev_str = compute_reversal_signal(
            direction=direction,
            d1_trend=d1_trend if pd.notna(d1_trend) else None,
            h4_trend=h4_trend if pd.notna(h4_trend) else None,
            rsi=float(row["rsi"]) if pd.notna(row.get("rsi")) else None,
            stoch_k=float(row["stoch_k"]) if pd.notna(row.get("stoch_k")) else None,
            boll_pct_b=float(row["boll_pct_b"]) if pd.notna(row.get("boll_pct_b")) else None,
            mfi=float(row["mfi"]) if pd.notna(row.get("mfi")) else None,
            macd_hist=float(row["macd_hist"]) if pd.notna(row.get("macd_hist")) else None,
            plus_di=float(row["plus_di"]) if pd.notna(row.get("plus_di")) else None,
            minus_di=float(row["minus_di"]) if pd.notna(row.get("minus_di")) else None,
            boll_bw=float(row["boll_bw"]) if pd.notna(row.get("boll_bw")) else None,
        )
        t_align = compute_trend_alignment_value(direction, d1_trend, h4_trend, rev)

        has_reversal[i] = 1.0 if rev else 0.0
        reversal_strength[i] = rev_str
        trend_alignment[i] = float(t_align)

    df["has_reversal"] = has_reversal
    df["reversal_strength"] = reversal_strength
    df["trend_alignment"] = trend_alignment

    return df


def save_to_database(df: pd.DataFrame, db_path: str = "data/oracle.db", strategy_id: str = "premium_backfill_v6") -> int:
    """Save backfilled trade outcomes to the database."""
    # Filter rows with valid labels
    valid = df[df["outcome_label"].notna()].copy()
    # Drop rows where critical indicators are still NaN (warmup period)
    critical_indicators = ["rsi", "adx", "boll_pct_b", "ema_50", "ema_200", "atr"]
    for col in critical_indicators:
        if col in valid.columns:
            valid = valid[valid[col].notna()]
    logger.info("After dropping warmup NaN rows: %d valid outcomes", len(valid))

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Ensure trade_outcomes table exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trade_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER,
            direction TEXT,
            trading_mode TEXT,
            strategy_id TEXT,
            outcome_label TEXT,
            profit REAL,
            profit_pct REAL,
            features_json TEXT,
            account_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Delete old backfill data
    cur.execute("DELETE FROM trade_outcomes WHERE strategy_id = ?", (strategy_id,))
    conn.commit()

    # Feature columns that go into features_json
    feature_cols = [
        # Volume
        "obv", "obv_slope", "mfi", "vwap_offset_pct", "volume_roc",
        "ad_line", "ad_line_slope", "cmf",
        # OB/OS
        "rsi", "stoch_k", "stoch_d", "williams_r", "cci", "demarker", "roc",
        # MA
        "sma_10", "sma_20", "sma_50",
        "ema_9", "ema_21", "ema_50", "ema_200",
        "dema_21", "tema_21",
        "ichimoku_tenkan", "ichimoku_kijun", "ichimoku_senkou_a", "ichimoku_senkou_b", "ichimoku_chikou",
        # Sentiment
        "tick_volume_ratio", "spread_ratio", "long_short_ratio", "session_strength",
        # Broky
        "macd_hist", "adx", "plus_di", "minus_di", "boll_pct_b", "boll_bw",
        "atr", "atr_to_price",
        "has_reversal", "reversal_strength", "trend_alignment",
        # External sentiment
        "fear_greed_value", "gold_bias_strength", "news_sentiment",
        # Multi-TF price
        "h1_close", "h4_close", "d1_close", "m5_high", "m5_low",
        # Derived
        "ema_9_21_diff", "di_diff", "boll_pct_b_clipped",
        # Categorical
        "session", "d1_trend", "h4_trend", "price_vs_cloud", "mfi_signal", "regime",
        # Encoded
        "price_vs_cloud_encoded", "d1_trend_encoded", "h4_trend_encoded",
        "mfi_signal_encoded", "regime_encoded",
        "regime_trending", "regime_ranging", "regime_volatile",
        # Balance/leverage
        "balance_at_entry", "leverage_at_entry",
    ]

    saved = 0
    for idx, row in valid.iterrows():
        features = {}
        for col in feature_cols:
            if col in row.index:
                val = row[col]
                if pd.isna(val):
                    # Skip None/NaN — let FeatureEngineer handle missing values
                    continue
                elif isinstance(val, (np.integer, np.int64)):
                    features[col] = int(val)
                elif isinstance(val, (np.floating, np.float64)):
                    features[col] = float(val)
                elif isinstance(val, (bool, np.bool_)):
                    features[col] = bool(val)
                elif isinstance(val, str):
                    features[col] = val
                else:
                    # Try to convert to float
                    try:
                        features[col] = float(val)
                    except (ValueError, TypeError):
                        features[col] = str(val)

        # Add direction to features (needed for direction-specific models)
        features["direction"] = str(row.get("direction", row.get("_direction", "BUY")))

        outcome = "WIN" if row["outcome_label"] == 1 else "LOSS"
        direction = str(row.get("direction", row.get("_direction", "BUY")))
        profit = float(row["profit"]) if pd.notna(row["profit"]) else 0.0
        profit_pct = float(row["profit_pct"]) if pd.notna(row["profit_pct"]) else 0.0

        # Use negative trade_id for synthetic trades (avoids collision with live trades)
        trade_id = -(saved + 1)

        # Compute entry/exit prices from the candle data
        entry_price = float(row["close"]) if pd.notna(row["close"]) else 0.0
        # Exit price: approximate from forward movement
        if direction == "BUY":
            exit_price = entry_price * (1 + profit_pct / 100) if profit_pct != 0 else entry_price
        else:
            exit_price = entry_price * (1 - profit_pct / 100) if profit_pct != 0 else entry_price

        cur.execute("""
            INSERT INTO trade_outcomes
                (trade_id, direction, trading_mode, strategy_id, outcome_label,
                 entry_price, exit_price, profit, profit_pct, features_json, account_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id,
            direction,
            "premium_backfill",
            strategy_id,
            outcome,
            entry_price,
            exit_price,
            profit,
            profit_pct,
            json.dumps(features),
            0,  # account_id=0 for synthetic
        ))
        saved += 1

        if saved % 10000 == 0:
            logger.info("Saved %d / %d outcomes...", saved, len(valid))

    conn.commit()
    conn.close()

    logger.info("✅ Saved %d trade outcomes to %s (strategy_id=%s)", saved, db_path, strategy_id)
    return saved


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Backfill v6 training data from premium M5 data")
    parser.add_argument("--db-path", default="data/oracle.db", help="Database path")
    parser.add_argument("--min-forward-bars", type=int, default=12, help="Bars to look ahead for labeling")
    parser.add_argument("--threshold-pct", type=float, default=0.15, help="Win/Loss threshold in %")
    parser.add_argument("--strategy-id", default="premium_backfill_v6", help="Strategy ID for backfilled data")
    parser.add_argument("--skip-save", action="store_true", help="Skip saving to database (dry run)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1. Load premium data
    logger.info("Loading premium M5 data...")
    df = load_premium_data()

    # 2. Compute missing features
    logger.info("Computing missing features...")
    df = compute_missing_features(df)

    # 3. Compute reversal features
    logger.info("Computing reversal signal features...")
    df = compute_reversal_features(df)

    # 4. Generate trade outcome labels
    logger.info("Generating trade outcome labels (threshold=%.2f%%, forward_bars=%d)...",
                args.threshold_pct, args.min_forward_bars)
    df = generate_trade_outcomes(df, min_forward_bars=args.min_forward_bars,
                                  threshold_pct=args.threshold_pct)

    # 5. Print stats
    valid = df[df["outcome_label"].notna()]
    win_count = (valid["outcome_label"] == 1).sum()
    loss_count = (valid["outcome_label"] == 0).sum()
    total = len(valid)
    logger.info("=== Label Distribution ===")
    logger.info("  Total valid: %d", total)
    logger.info("  WIN: %d (%.1f%%)", win_count, win_count / total * 100 if total > 0 else 0)
    logger.info("  LOSS: %d (%.1f%%)", loss_count, loss_count / total * 100 if total > 0 else 0)
    logger.info("  Win Rate: %.1f%%", win_count / total * 100 if total > 0 else 0)

    # Direction distribution
    for direction in ["BUY", "SELL"]:
        mask = valid["_direction"] == direction
        if mask.sum() > 0:
            dir_win = (valid.loc[mask, "outcome_label"] == 1).sum()
            dir_total = mask.sum()
            logger.info("  %s: %d samples, WR=%.1f%%", direction, dir_total,
                       dir_win / dir_total * 100 if dir_total > 0 else 0)

    # Regime distribution
    for regime in valid["regime"].unique():
        mask = valid["regime"] == regime
        if mask.sum() > 0:
            reg_win = (valid.loc[mask, "outcome_label"] == 1).sum()
            reg_total = mask.sum()
            logger.info("  %s: %d samples, WR=%.1f%%", regime, reg_total,
                       reg_win / reg_total * 100 if reg_total > 0 else 0)

    # 6. Save to database
    if not args.skip_save:
        saved = save_to_database(valid, db_path=args.db_path, strategy_id=args.strategy_id)
        logger.info("✅ Backfill complete: %d outcomes saved", saved)
    else:
        logger.info("Dry run — skipped database save")

    return valid


if __name__ == "__main__":
    main()