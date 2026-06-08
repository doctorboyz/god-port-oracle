#!/usr/bin/env python3
"""Process premium data into pre-computed indicator parquet files.

Creates fast-loading parquet files with all indicators pre-computed,
ready for ML training and backtesting.

Usage:
    python scripts/process_premium_data.py [--output data/processed/]

Output files:
    - xauusd_m5_indicators.parquet  (M5 with all indicators, ~200k rows)
    - xauusd_h4_trend.parquet       (H4 with EMA trend)
    - xauusd_d1_trend.parquet       (D1 with EMA trend)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np

from broky.data.loader import load_timeframe
from broky.indicators.atr import calculate_atr
from broky.indicators.ema import calculate_ema
from broky.indicators.adx import calculate_adx
from broky.indicators.rsi import calculate_rsi
from broky.indicators.macd import calculate_macd
from broky.indicators.bollinger import calculate_bollinger
from broky.indicators.stochastic import calculate_stochastic
from broky.indicators.mfi import calculate_mfi
from broky.signals.generator import (
    classify_regime, compute_reversal_signal, compute_trend_alignment_value,
)


def process_m5(m5_df: pd.DataFrame, d1_trend_series: pd.Series, h4_trend_series: pd.Series) -> pd.DataFrame:
    """Process M5 data with all indicators."""
    print(f"Processing M5: {len(m5_df)} candles...")
    df = m5_df.copy()

    # ── Trend indicators ──
    df["ema10"] = calculate_ema(df["close"], 10)
    df["ema20"] = calculate_ema(df["close"], 20)
    df["ema50"] = calculate_ema(df["close"], 50)
    df["ema200"] = calculate_ema(df["close"], 200)

    # ── Volatility ──
    df["atr"] = calculate_atr(df["high"], df["low"], df["close"], period=14)
    df["atr_to_price"] = df["atr"] / df["close"]

    # ── ADX / DI ──
    adx_s, pdi_s, mdi_s = calculate_adx(df["high"], df["low"], df["close"], period=14)
    df["adx"] = adx_s
    df["plus_di"] = pdi_s
    df["minus_di"] = mdi_s

    # ── RSI ──
    df["rsi"] = calculate_rsi(df["close"], period=14)

    # ── MACD ──
    macd = calculate_macd(df["close"])
    df["macd_hist"] = macd.histogram

    # ── Bollinger ──
    boll = calculate_bollinger(df["close"], period=20, std_dev=2.0)
    df["boll_upper"] = boll.upper
    df["boll_middle"] = boll.middle
    df["boll_lower"] = boll.lower
    band_range = boll.upper - boll.lower
    df["boll_bw"] = (boll.upper - boll.lower) / boll.middle.replace(0, np.nan)
    df["boll_pct_b"] = np.where(band_range > 0, (df["close"] - boll.lower) / band_range, 0.5)

    # ── Stochastic ──
    stoch = calculate_stochastic(df["high"], df["low"], df["close"], k_period=14, d_period=3)
    df["stoch_k"] = stoch.k_line
    df["stoch_d"] = stoch.d_line

    # ── MFI ──
    df["mfi"] = calculate_mfi(df["high"], df["low"], df["close"], df["volume"], period=14)

    # ── Volume EMA ──
    df["vol_ema20"] = calculate_ema(df["volume"], 20)
    df["vol_ratio"] = df["volume"] / df["vol_ema20"].replace(0, np.nan)

    # ── Regime classification ──
    df["regime"] = df.apply(lambda r: classify_regime(r["adx"], r["boll_bw"]) if pd.notna(r["adx"]) else "ranging", axis=1)

    # ── D1 and H4 trend (forward-filled) ──
    df["d1_trend"] = None
    df["h4_trend"] = None
    for i in range(len(df)):
        ts = df.index[i]
        valid_d1 = d1_trend_series[d1_trend_series.index <= ts]
        if len(valid_d1) > 0:
            df.iloc[i, df.columns.get_loc("d1_trend")] = valid_d1.iloc[-1]
        valid_h4 = h4_trend_series[h4_trend_series.index <= ts]
        if len(valid_h4) > 0:
            df.iloc[i, df.columns.get_loc("h4_trend")] = valid_h4.iloc[-1]

    # ── Reversal signal ──
    df["has_reversal"] = False
    df["reversal_strength"] = 0.0
    df["trend_alignment"] = 0

    for i in range(200, len(df)):  # Skip warmup
        r = df.iloc[i]
        close_diff = df["close"].iloc[i] - df["close"].iloc[i-1]
        if close_diff > 0:
            direction = "BUY"
        elif close_diff < 0:
            direction = "SELL"
        else:
            continue

        d1_trend = r.get("d1_trend")
        h4_trend = r.get("h4_trend")

        has_rev, rev_str = compute_reversal_signal(
            direction=direction, d1_trend=d1_trend, h4_trend=h4_trend,
            rsi=float(r["rsi"]) if pd.notna(r["rsi"]) else None,
            stoch_k=float(r["stoch_k"]) if pd.notna(r["stoch_k"]) else None,
            boll_pct_b=float(r["boll_pct_b"]) if pd.notna(r["boll_pct_b"]) else None,
            mfi=float(r["mfi"]) if pd.notna(r["mfi"]) else None,
            macd_hist=float(r["macd_hist"]) if pd.notna(r["macd_hist"]) else None,
            plus_di=float(r["plus_di"]) if pd.notna(r["plus_di"]) else None,
            minus_di=float(r["minus_di"]) if pd.notna(r["minus_di"]) else None,
            boll_bw=float(r["boll_bw"]) if pd.notna(r["boll_bw"]) else None,
        )
        t_align = compute_trend_alignment_value(direction, d1_trend, h4_trend, has_rev)
        df.iloc[i, df.columns.get_loc("has_reversal")] = has_rev
        df.iloc[i, df.columns.get_loc("reversal_strength")] = rev_str
        df.iloc[i, df.columns.get_loc("trend_alignment")] = t_align

    # Count reversal stats
    rev_count = df["has_reversal"].sum()
    ta_counts = df["trend_alignment"].value_counts().sort_index()
    print(f"  has_reversal=True: {rev_count} rows ({rev_count/len(df)*100:.1f}%)")
    print(f"  trend_alignment distribution: {dict(ta_counts)}")

    return df


def process_h4(h4_df: pd.DataFrame) -> pd.DataFrame:
    """Process H4 data with trend indicators."""
    print(f"Processing H4: {len(h4_df)} candles...")
    df = h4_df.copy()
    df["ema10"] = calculate_ema(df["close"], 10)
    df["ema50"] = calculate_ema(df["close"], 50)
    df["trend"] = df.apply(
        lambda r: "bullish" if pd.notna(r["ema10"]) and pd.notna(r["ema50"]) and r["ema10"] > r["ema50"]
        else "bearish" if pd.notna(r["ema10"]) and pd.notna(r["ema50"]) else None,
        axis=1
    )
    return df


def process_d1(d1_df: pd.DataFrame) -> pd.DataFrame:
    """Process D1 data with trend indicators."""
    print(f"Processing D1: {len(d1_df)} candles...")
    df = d1_df.copy()
    df["ema50"] = calculate_ema(df["close"], 50)
    df["ema200"] = calculate_ema(df["close"], 200)
    df["trend"] = df.apply(
        lambda r: "bullish" if pd.notna(r["ema50"]) and pd.notna(r["ema200"]) and r["ema50"] > r["ema200"]
        else "bearish" if pd.notna(r["ema50"]) and pd.notna(r["ema200"]) else None,
        axis=1
    )
    return df


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Process premium data into parquet files")
    parser.add_argument("--output", default="data/processed", help="Output directory")
    parser.add_argument("--skip-m5-indicators", action="store_true", help="Skip expensive M5 indicator computation")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    data_dir = Path(__file__).parent.parent / "data" / "xau-data"

    # Load raw data
    print("Loading premium data...")
    m5_df = load_timeframe(data_dir, "M5")
    h4_df = load_timeframe(data_dir, "H4")
    d1_df = load_timeframe(data_dir, "D1")

    for df, name in [(m5_df, "M5"), (h4_df, "H4"), (d1_df, "D1")]:
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        print(f"  {name}: {len(df)} rows ({df.index[0]} → {df.index[-1]})")

    # Process and save H4
    h4_processed = process_h4(h4_df)
    h4_path = output_dir / "xauusd_h4_trend.parquet"
    h4_processed.to_parquet(h4_path)
    print(f"Saved H4 trend data → {h4_path} ({len(h4_processed)} rows)")

    # Process and save D1
    d1_processed = process_d1(d1_df)
    d1_path = output_dir / "xauusd_d1_trend.parquet"
    d1_processed.to_parquet(d1_path)
    print(f"Saved D1 trend data → {d1_path} ({len(d1_processed)} rows)")

    # Process D1/H4 trend series for M5
    d1_trend_series = d1_processed["trend"].dropna()
    h4_trend_series = h4_processed["trend"].dropna()

    # Process and save M5 (expensive)
    if not args.skip_m5_indicators:
        m5_processed = process_m5(m5_df, d1_trend_series, h4_trend_series)
        m5_path = output_dir / "xauusd_m5_indicators.parquet"
        m5_processed.to_parquet(m5_path)
        print(f"Saved M5 indicators → {m5_path} ({len(m5_processed)} rows)")
    else:
        print("Skipping M5 indicator computation (--skip-m5-indicators)")

    print("\n✅ Done! Processed files saved to:", output_dir)
    print("\nUsage in Python:")
    print(f'  m5 = pd.read_parquet("{output_dir}/xauusd_m5_indicators.parquet")')
    print(f'  h4 = pd.read_parquet("{output_dir}/xauusd_h4_trend.parquet")')
    print(f'  d1 = pd.read_parquet("{output_dir}/xauusd_d1_trend.parquet")')


if __name__ == "__main__":
    main()