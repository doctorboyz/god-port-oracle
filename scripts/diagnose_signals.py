"""Diagnose signal generation — score distribution and confidence analysis."""

import sys
sys.path.insert(0, ".")

import pandas as pd
from broky.data.loader import load_timeframe
from broky.signals.generator import (
    calculate_indicator_scores,
    calculate_weighted_score,
    calculate_signal_confidence,
    score_to_signal_type,
    MIN_CONFIDENCE,
)
from broky.indicators.ema import calculate_ema


def main():
    data_dir = "data/xau-data"
    df = load_timeframe(data_dir, "H1")
    df_d1 = load_timeframe(data_dir, "D1")

    # Compute D1 trend
    ema50_d1 = calculate_ema(df_d1["Close"], 50)
    ema200_d1 = calculate_ema(df_d1["Close"], 200)

    print(f"H1 data: {len(df)} candles, {df.index[0]} → {df.index[-1]}")
    print(f"MIN_CONFIDENCE = {MIN_CONFIDENCE}")
    print()

    # Sample every 100 candles from warmup onward
    warmup = 200
    sample_step = 100
    buy_count = 0
    sell_count = 0
    hold_count = 0
    confidence_dist = []
    score_dist = []

    for i in range(warmup, len(df), sample_step):
        close = df["Close"].iloc[:i+1]
        high = df["High"].iloc[:i+1]
        low = df["Low"].iloc[:i+1]
        volume = df["Volume"].iloc[:i+1]

        scores = calculate_indicator_scores(close, high, low, volume)
        weighted = calculate_weighted_score(scores)
        confidence = calculate_signal_confidence(scores, weighted)
        signal_type = score_to_signal_type(weighted)

        confidence_dist.append(confidence)
        score_dist.append(weighted)

        if signal_type.value == "BUY":
            buy_count += 1
        elif signal_type.value == "SELL":
            sell_count += 1
        else:
            hold_count += 1

    total = buy_count + sell_count + hold_count
    print(f"Sampled {total} candles (every {sample_step}th from warmup {warmup}):")
    print(f"  BUY:  {buy_count} ({buy_count/total:.1%})")
    print(f"  SELL: {sell_count} ({sell_count/total:.1%})")
    print(f"  HOLD: {hold_count} ({hold_count/total:.1%})")

    conf_series = pd.Series(confidence_dist)
    score_series = pd.Series(score_dist)

    print(f"\nConfidence distribution:")
    print(f"  mean={conf_series.mean():.3f}, median={conf_series.median():.3f}")
    print(f"  min={conf_series.min():.3f}, max={conf_series.max():.3f}")
    print(f"  >= 0.55: {(conf_series >= 0.55).sum()} ({(conf_series >= 0.55).mean():.1%})")
    print(f"  >= 0.50: {(conf_series >= 0.50).sum()} ({(conf_series >= 0.50).mean():.1%})")
    print(f"  >= 0.40: {(conf_series >= 0.40).sum()} ({(conf_series >= 0.40).mean():.1%})")

    print(f"\nWeighted score distribution:")
    print(f"  mean={score_series.mean():.3f}, median={score_series.median():.3f}")
    print(f"  min={score_series.min():.3f}, max={score_series.max():.3f}")
    print(f"  |score| > 0.3: {(score_series.abs() > 0.3).sum()} ({(score_series.abs() > 0.3).mean():.1%})")
    print(f"  |score| > 0.2: {(score_series.abs() > 0.2).sum()} ({(score_series.abs() > 0.2).mean():.1%})")

    # D1 trend distribution
    d1_bullish = 0
    d1_bearish = 0
    for i in range(len(df_d1)):
        if pd.notna(ema50_d1.iloc[i]) and pd.notna(ema200_d1.iloc[i]):
            if ema50_d1.iloc[i] > ema200_d1.iloc[i]:
                d1_bullish += 1
            else:
                d1_bearish += 1
    print(f"\nD1 trend: bullish={d1_bullish}, bearish={d1_bearish} days")
    print(f"  Bullish %: {d1_bullish/(d1_bullish+d1_bearish):.1%}")

    # Check specific period: 2023-2024
    print(f"\n--- Focus on 2023-2024 ---")
    mask = (df.index >= "2023-01-01") & (df.index < "2025-01-01")
    df_focus = df[mask]
    print(f"H1 candles in 2023-2024: {len(df_focus)}")

    for i in range(len(df)):
        if df.index[i] >= pd.Timestamp("2023-01-01") and i >= warmup:
            start_i = i
            break

    buy_23 = sell_23 = hold_23 = 0
    conf_23 = []
    for i in range(start_i, min(start_i + 5000, len(df)), 10):
        close = df["Close"].iloc[:i+1]
        high = df["High"].iloc[:i+1]
        low = df["Low"].iloc[:i+1]
        volume = df["Volume"].iloc[:i+1]

        scores = calculate_indicator_scores(close, high, low, volume)
        weighted = calculate_weighted_score(scores)
        confidence = calculate_signal_confidence(scores, weighted)
        signal_type = score_to_signal_type(weighted)
        conf_23.append(confidence)

        if signal_type.value == "BUY":
            buy_23 += 1
        elif signal_type.value == "SELL":
            sell_23 += 1
        else:
            hold_23 += 1

    total_23 = buy_23 + sell_23 + hold_23
    print(f"Sampled {total_23} in 2023-2024:")
    print(f"  BUY: {buy_23}, SELL: {sell_23}, HOLD: {hold_23}")
    s23 = pd.Series(conf_23)
    print(f"  Confidence: mean={s23.mean():.3f}, max={s23.max():.3f}")
    print(f"  >= 0.55: {(s23 >= 0.55).sum()} ({(s23 >= 0.55).mean():.1%})")

    # Print indicator score breakdown for a bullish period
    print(f"\n--- Sample indicator scores (latest candle) ---")
    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]
    scores = calculate_indicator_scores(close, high, low, volume)
    weighted = calculate_weighted_score(scores)
    confidence = calculate_signal_confidence(scores, weighted)
    for k, v in sorted(scores.items()):
        w = INDICATOR_WEIGHTS.get(k, 0.0)
        print(f"  {k:>12}: {v:+.2f} (weight={w:.2f}, contribution={v*w:+.3f})")
    print(f"  Weighted: {weighted:+.3f}")
    print(f"  Confidence: {confidence:.3f}")


from broky.signals.generator import INDICATOR_WEIGHTS

if __name__ == "__main__":
    main()