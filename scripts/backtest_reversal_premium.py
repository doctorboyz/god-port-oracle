#!/usr/bin/env python3
"""Backtest reversal signal feature on premium data.

Pre-computes all indicators once, then simulates trades by looking up values.
~100x faster than naive slice-per-candle approach.

Usage:
    python scripts/backtest_reversal_premium.py [--start 2024-01-01] [--end 2026-04-01]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
from datetime import datetime, timezone

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
from broky.risk.position_sizing import calculate_stop_loss, calculate_take_profit, calculate_position_size
from broky.signals.generator import (
    classify_regime, compute_reversal_signal, compute_trend_alignment_value,
    calculate_weighted_score, MIN_CONFIDENCE,
    REVERSAL_OB_RSI, REVERSAL_OS_RSI, REVERSAL_OB_STOCH, REVERSAL_OS_STOCH,
    REVERSAL_OB_BOLL, REVERSAL_OS_BOLL, REVERSAL_OB_MFI, REVERSAL_OS_MFI,
)
from shared.models import SignalType, MarketRegime


def precompute_indicators(m5_df: pd.DataFrame) -> pd.DataFrame:
    """Pre-compute all indicators once on full M5 data."""
    print("Pre-computing indicators on M5 data...")
    df = m5_df.copy()

    # ATR
    atr = calculate_atr(df["high"], df["low"], df["close"], period=14)
    df["atr"] = atr

    # EMA
    df["ema10"] = calculate_ema(df["close"], 10)
    df["ema20"] = calculate_ema(df["close"], 20)
    df["ema50"] = calculate_ema(df["close"], 50)

    # RSI
    df["rsi"] = calculate_rsi(df["close"], period=14)

    # MACD
    macd = calculate_macd(df["close"])
    df["macd_hist"] = macd.histogram

    # Bollinger
    boll = calculate_bollinger(df["close"], period=20, std_dev=2.0)
    df["boll_upper"] = boll.upper
    df["boll_middle"] = boll.middle
    df["boll_lower"] = boll.lower
    df["boll_bw"] = (boll.upper - boll.lower) / boll.middle.replace(0, np.nan)
    # Band position (boll_pct_b): 0 = lower band, 1 = upper band
    band_range = boll.upper - boll.lower
    df["boll_pct_b"] = np.where(band_range > 0, (df["close"] - boll.lower) / band_range, 0.5)

    # Stochastic
    stoch = calculate_stochastic(df["high"], df["low"], df["close"], k_period=14, d_period=3)
    df["stoch_k"] = stoch.k_line
    df["stoch_d"] = stoch.d_line

    # ADX
    adx_s, pdi_s, mdi_s = calculate_adx(df["high"], df["low"], df["close"], period=14)
    df["adx"] = adx_s
    df["plus_di"] = pdi_s
    df["minus_di"] = mdi_s

    # MFI
    df["mfi"] = calculate_mfi(df["high"], df["low"], df["close"], df["volume"], period=14)

    # Volume EMA (for volume confirmation)
    df["vol_ema20"] = calculate_ema(df["volume"], 20)

    return df


def precompute_trends(d1_df: pd.DataFrame, h4_df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Compute D1 and H4 trend series."""
    # D1 trend: EMA 50 vs EMA 200
    d1_ema50 = calculate_ema(d1_df["close"], 50)
    d1_ema200 = calculate_ema(d1_df["close"], 200)
    d1_trend = pd.Series(index=d1_df.index, dtype=object)
    for i in range(len(d1_df)):
        if pd.notna(d1_ema50.iloc[i]) and pd.notna(d1_ema200.iloc[i]):
            d1_trend.iloc[i] = "bullish" if d1_ema50.iloc[i] > d1_ema200.iloc[i] else "bearish"
        else:
            d1_trend.iloc[i] = None
    d1_trend = d1_trend.dropna()

    # H4 trend: EMA 10 vs EMA 50
    h4_ema10 = calculate_ema(h4_df["close"], 10)
    h4_ema50 = calculate_ema(h4_df["close"], 50)
    h4_trend = pd.Series(index=h4_df.index, dtype=object)
    for i in range(len(h4_df)):
        if pd.notna(h4_ema10.iloc[i]) and pd.notna(h4_ema50.iloc[i]):
            h4_trend.iloc[i] = "bullish" if h4_ema10.iloc[i] > h4_ema50.iloc[i] else "bearish"
        else:
            h4_trend.iloc[i] = None
    h4_trend = h4_trend.dropna()

    return d1_trend, h4_trend


def get_trend_at(trend_series: pd.Series, timestamp: pd.Timestamp) -> str | None:
    """Get trend value at or before the given timestamp."""
    valid = trend_series[trend_series.index <= timestamp]
    if len(valid) > 0:
        return valid.iloc[-1]
    return None


def generate_signal_fast(row: pd.Series, d1_trend: str | None, h4_trend: str | None,
                          min_confidence: float = 0.55) -> dict:
    """Fast signal generation using pre-computed indicator values.

    Returns dict with signal info including reversal features, or None for HOLD.
    """
    # Extract values from pre-computed row
    adx_val = row.get("adx")
    pdi_val = row.get("plus_di")
    mdi_val = row.get("minus_di")
    rsi_val = row.get("rsi")
    macd_hist_val = row.get("macd_hist")
    boll_pct_b = row.get("boll_pct_b")
    boll_bw_val = row.get("boll_bw")
    stoch_k_val = row.get("stoch_k")
    mfi_val = row.get("mfi")
    close_val = row["close"]
    atr_val = row.get("atr")

    # Skip if essential indicators are NaN
    if pd.isna(adx_val) or pd.isna(atr_val):
        return None

    # ── Build indicator scores (same logic as generator) ──
    scores = {}

    # ADX score
    if pd.notna(adx_val) and pd.notna(pdi_val) and pd.notna(mdi_val):
        if adx_val >= 25:
            if adx_val >= 50:
                scores["adx"] = 1.0 if pdi_val > mdi_val else -1.0
            else:
                scores["adx"] = 0.5 if pdi_val > mdi_val else -0.5
        elif adx_val >= 20:
            scores["adx"] = 0.3 if pdi_val > mdi_val else -0.3
        else:
            scores["adx"] = 0.0

    # MACD
    if pd.notna(macd_hist_val):
        if macd_hist_val > 0:
            scores["macd"] = min(macd_hist_val / 2.0, 1.0)
        else:
            scores["macd"] = max(macd_hist_val / 2.0, -1.0)

    # Bollinger Band
    if pd.notna(boll_pct_b):
        if boll_pct_b >= 0.85:
            scores["boll"] = min((boll_pct_b - 0.5) / 0.5, 1.0)
        elif boll_pct_b <= 0.15:
            scores["boll"] = max((boll_pct_b - 0.5) / 0.5, -1.0)
        else:
            scores["boll"] = (boll_pct_b - 0.5) / 0.5 * 0.5

    # Stochastic
    if pd.notna(stoch_k_val):
        if stoch_k_val > 80:
            scores["stoch"] = -0.5  # overbought → bearish
        elif stoch_k_val < 20:
            scores["stoch"] = 0.5   # oversold → bullish
        else:
            scores["stoch"] = 0.0

    # RSI
    if pd.notna(rsi_val):
        if rsi_val > 70:
            scores["rsi"] = -0.5  # overbought
        elif rsi_val < 30:
            scores["rsi"] = 0.5   # oversold
        else:
            scores["rsi"] = 0.0

    # Volume
    vol_ema = row.get("vol_ema20")
    volume_val = row.get("volume")
    if pd.notna(vol_ema) and pd.notna(volume_val) and vol_ema > 0:
        vol_ratio = volume_val / vol_ema
        if vol_ratio > 1.5:
            scores["volume"] = 0.5
        elif vol_ratio < 0.5:
            scores["volume"] = -0.3
        else:
            scores["volume"] = 0.0

    # MFI
    if pd.notna(mfi_val):
        if mfi_val > 80:
            scores["mfi"] = -0.5  # overbought
        elif mfi_val < 20:
            scores["mfi"] = 0.5   # oversold
        else:
            scores["mfi"] = 0.0

    # ── Weighted score ──
    weighted_score = calculate_weighted_score(scores)
    confidence = abs(weighted_score)

    # ── Regime ──
    regime = classify_regime(adx_val, boll_bw_val)

    # ── Signal type ──
    if confidence < min_confidence:
        signal_type = SignalType.HOLD
    elif weighted_score > 0:
        signal_type = SignalType.BUY
    else:
        signal_type = SignalType.SELL

    # ── Reversal signal ──
    direction = signal_type.value if signal_type != SignalType.HOLD else None
    has_reversal = False
    reversal_strength = 0.0
    trend_alignment = 0

    if signal_type != SignalType.HOLD:
        has_reversal, reversal_strength = compute_reversal_signal(
            direction=direction, d1_trend=d1_trend, h4_trend=h4_trend,
            rsi=float(rsi_val) if pd.notna(rsi_val) else None,
            stoch_k=float(stoch_k_val) if pd.notna(stoch_k_val) else None,
            boll_pct_b=float(boll_pct_b) if pd.notna(boll_pct_b) else None,
            mfi=float(mfi_val) if pd.notna(mfi_val) else None,
            macd_hist=float(macd_hist_val) if pd.notna(macd_hist_val) else None,
            plus_di=float(pdi_val) if pd.notna(pdi_val) else None,
            minus_di=float(mdi_val) if pd.notna(mdi_val) else None,
            boll_bw=float(boll_bw_val) if pd.notna(boll_bw_val) else None,
        )
        trend_alignment = compute_trend_alignment_value(direction, d1_trend, h4_trend, has_reversal)

    return {
        "signal_type": signal_type,
        "confidence": confidence,
        "weighted_score": weighted_score,
        "regime": regime,
        "direction": direction,
        "has_reversal": has_reversal,
        "reversal_strength": reversal_strength,
        "trend_alignment": trend_alignment,
        "atr": float(atr_val) if pd.notna(atr_val) else 5.0,
    }


def run_backtest(
    m5_df: pd.DataFrame,
    d1_df: pd.DataFrame,
    h4_df: pd.DataFrame,
    start_date: str = "2024-01-01",
    end_date: str = "2026-12-31",
    min_confidence: float = 0.55,
    atr_multiplier: float = 2.0,
    risk_reward_ratio: float = 2.5,
    risk_per_trade: float = 0.01,
    max_holding_bars: int = 288,  # 288 M5 bars = 24 hours
    cooldown_bars: int = 12,
    contract_size: float = 100.0,
    starting_equity: float = 1000.0,
):
    """Run backtest with pre-computed indicators for speed."""
    # Filter by date range
    cutoff_start = pd.Timestamp(start_date, tz="UTC")
    cutoff_end = pd.Timestamp(end_date, tz="UTC")
    m5_filtered = m5_df[(m5_df.index >= cutoff_start) & (m5_df.index <= cutoff_end)].copy()

    print(f"\nM5 data: {len(m5_filtered)} candles, {m5_filtered.index[0]} → {m5_filtered.index[-1]}")
    print(f"Conf: {min_confidence}, ATR: {atr_multiplier}, RR: {risk_reward_ratio}")

    # Pre-compute indicators
    m5_ind = precompute_indicators(m5_filtered)
    d1_trend_series, h4_trend_series = precompute_trends(d1_df, h4_df)

    warmup = 200
    equity = starting_equity

    # Track trades by category
    categories = {
        "trend_aligned": {"trades": [], "wins": 0, "pnl": 0.0},
        "reversal": {"trades": [], "wins": 0, "pnl": 0.0},
        "counter_trend": {"trades": [], "wins": 0, "pnl": 0.0},
        "neutral": {"trades": [], "wins": 0, "pnl": 0.0},
    }

    all_trades = []
    position = None
    last_exit_idx = -cooldown_bars - 1

    total_candles = len(m5_ind)
    print_every = max(1, total_candles // 20)  # Progress every 5%

    for i in range(warmup, total_candles):
        row = m5_ind.iloc[i]

        if i % print_every == 0:
            pct = i / total_candles * 100
            print(f"  Progress: {pct:.0f}% ({i}/{total_candles}), trades so far: {len(all_trades)}")

        # Check exit for open position
        if position is not None:
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            bars_held = i - position["entry_idx"]

            # Max holding exit
            if bars_held >= max_holding_bars:
                pnl = _calc_pnl(position, close, contract_size)
                position["exit_price"] = close
                position["pnl"] = pnl
                position["exit_reason"] = "max_holding"
                all_trades.append(position)
                equity += pnl
                position = None
                last_exit_idx = i
                continue

            # SL/TP check
            if position["direction"] == SignalType.BUY:
                if low <= position["stop_loss"]:
                    exit_price = position["stop_loss"]
                    pnl = _calc_pnl(position, exit_price, contract_size)
                    position["exit_price"] = exit_price
                    position["pnl"] = pnl
                    position["exit_reason"] = "stop_loss"
                    all_trades.append(position)
                    equity += pnl
                    position = None
                    last_exit_idx = i
                    continue
                elif high >= position["take_profit"]:
                    exit_price = position["take_profit"]
                    pnl = _calc_pnl(position, exit_price, contract_size)
                    position["exit_price"] = exit_price
                    position["pnl"] = pnl
                    position["exit_reason"] = "take_profit"
                    all_trades.append(position)
                    equity += pnl
                    position = None
                    last_exit_idx = i
                    continue
            else:  # SELL
                if high >= position["stop_loss"]:
                    exit_price = position["stop_loss"]
                    pnl = _calc_pnl(position, exit_price, contract_size)
                    position["exit_price"] = exit_price
                    position["pnl"] = pnl
                    position["exit_reason"] = "stop_loss"
                    all_trades.append(position)
                    equity += pnl
                    position = None
                    last_exit_idx = i
                    continue
                elif low <= position["take_profit"]:
                    exit_price = position["take_profit"]
                    pnl = _calc_pnl(position, exit_price, contract_size)
                    position["exit_price"] = exit_price
                    position["pnl"] = pnl
                    position["exit_reason"] = "take_profit"
                    all_trades.append(position)
                    equity += pnl
                    position = None
                    last_exit_idx = i
                    continue

        # Try to open new position
        if position is None and (i - last_exit_idx >= cooldown_bars):
            d1_trend = get_trend_at(d1_trend_series, m5_ind.index[i])
            h4_trend = get_trend_at(h4_trend_series, m5_ind.index[i])

            sig = generate_signal_fast(row, d1_trend, h4_trend, min_confidence)
            if sig is None or sig["signal_type"] == SignalType.HOLD:
                continue
            if sig["confidence"] < min_confidence:
                continue

            current_price = float(row["close"])
            atr_val = sig["atr"]
            direction = sig["direction"]

            sl = calculate_stop_loss(current_price, atr_val, direction, atr_multiplier, 2.0)
            tp = calculate_take_profit(current_price, sl, direction, risk_reward_ratio)
            lots = calculate_position_size(equity, risk_per_trade, current_price, sl, contract_size)

            if lots <= 0:
                continue

            position = {
                "entry_idx": i,
                "entry_time": m5_ind.index[i],
                "entry_price": current_price,
                "direction": SignalType.BUY if direction == "BUY" else SignalType.SELL,
                "stop_loss": sl,
                "take_profit": tp,
                "lot_size": lots,
                "confidence": sig["confidence"],
                "regime": sig["regime"],
                "d1_trend": d1_trend,
                "h4_trend": h4_trend,
                "has_reversal": sig["has_reversal"],
                "reversal_strength": sig["reversal_strength"],
                "trend_alignment": sig["trend_alignment"],
                "weighted_score": sig["weighted_score"],
            }

    # ── Results ──
    print(f"\n{'='*80}")
    print(f"BACKTEST RESULTS — Premium M5 Data (Pre-computed Indicators)")
    print(f"{'='*80}")
    print(f"Period: {start_date} → {end_date}")
    print(f"Total trades: {len(all_trades)}")
    print(f"Final equity: ${equity:.2f} (started: ${starting_equity:.2f})")

    if not all_trades:
        print("\nNo trades generated. Try adjusting min_confidence or date range.")
        return all_trades

    # Categorize
    for t in all_trades:
        ta = t.get("trend_alignment", 0)
        if ta == 1:
            cat = "trend_aligned"
        elif ta == 2:
            cat = "reversal"
        elif ta == -1:
            cat = "counter_trend"
        else:
            cat = "neutral"
        categories[cat]["trades"].append(t)
        categories[cat]["pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            categories[cat]["wins"] += 1

    # Print category table
    _print_results(categories, all_trades, equity, starting_equity)

    return all_trades


def _calc_pnl(position: dict, exit_price: float, contract_size: float) -> float:
    """Calculate PnL for a closed position."""
    if position["direction"] == SignalType.BUY:
        return (exit_price - position["entry_price"]) * position["lot_size"] * contract_size
    else:
        return (position["entry_price"] - exit_price) * position["lot_size"] * contract_size


def _print_results(categories: dict, all_trades: list, equity: float, starting_equity: float):
    """Print formatted backtest results."""
    print(f"\n{'─'*80}")
    print(f"{'Category':<20} {'Trades':>8} {'Wins':>8} {'WR%':>8} {'PnL':>12} {'Avg PnL':>10}")
    print(f"{'─'*80}")

    total_trades = 0
    total_wins = 0
    total_pnl = 0.0

    for cat in ["trend_aligned", "reversal", "counter_trend", "neutral"]:
        c = categories[cat]
        n = len(c["trades"])
        wr = (c["wins"] / n * 100) if n > 0 else 0
        avg = (c["pnl"] / n) if n > 0 else 0
        print(f"{cat:<20} {n:>8} {c['wins']:>8} {wr:>7.1f}% ${c['pnl']:>10.2f} ${avg:>8.2f}")
        total_trades += n
        total_wins += c["wins"]
        total_pnl += c["pnl"]

    print(f"{'─'*80}")
    total_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
    total_avg = (total_pnl / total_trades) if total_trades > 0 else 0
    print(f"{'TOTAL':<20} {total_trades:>8} {total_wins:>8} {total_wr:>7.1f}% ${total_pnl:>10.2f} ${total_avg:>8.2f}")
    print(f"{'─'*80}")

    # ── Reversal vs Counter-trend ──
    rev = categories["reversal"]
    ct = categories["counter_trend"]
    ta = categories["trend_aligned"]

    print(f"\n{'='*80}")
    print("TREND ALIGNMENT ANALYSIS")
    print(f"{'='*80}")

    for name, c in [("Trend-aligned", ta), ("Reversal", rev), ("Counter-trend", ct), ("Neutral", categories["neutral"])]:
        n = len(c["trades"])
        if n > 0:
            wr = c["wins"] / n * 100
            avg = c["pnl"] / n
            print(f"  {name:<20} {n:>5} trades, WR={wr:>6.1f}%, Avg=${avg:.2f}, Total=${c['pnl']:.2f}")
        else:
            print(f"  {name:<20}     0 trades")

    # ── Regime breakdown ──
    print(f"\n{'='*80}")
    print("REGIME BREAKDOWN")
    print(f"{'='*80}")

    regime_cats = {}
    for t in all_trades:
        regime = t.get("regime", "unknown")
        if regime not in regime_cats:
            regime_cats[regime] = {"trades": 0, "wins": 0, "pnl": 0.0}
        regime_cats[regime]["trades"] += 1
        regime_cats[regime]["pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            regime_cats[regime]["wins"] += 1

    for regime in ["trending", "ranging", "volatile", "unknown"]:
        if regime in regime_cats:
            c = regime_cats[regime]
            n = c["trades"]
            wr = c["wins"] / n * 100 if n > 0 else 0
            print(f"  {regime:<15} {n:>5} trades, WR={wr:>6.1f}%, PnL=${c['pnl']:.2f}")

    # ── Exit reason breakdown ──
    print(f"\n{'='*80}")
    print("EXIT REASON BREAKDOWN")
    print(f"{'='*80}")

    exit_cats = {}
    for t in all_trades:
        reason = t.get("exit_reason", "unknown")
        if reason not in exit_cats:
            exit_cats[reason] = {"count": 0, "pnl": 0.0}
        exit_cats[reason]["count"] += 1
        exit_cats[reason]["pnl"] += t.get("pnl", 0)

    for reason in ["take_profit", "stop_loss", "max_holding"]:
        if reason in exit_cats:
            c = exit_cats[reason]
            avg = c["pnl"] / c["count"] if c["count"] > 0 else 0
            wr = sum(1 for t in all_trades if t.get("exit_reason") == reason and t.get("pnl", 0) > 0) / c["count"] * 100 if c["count"] > 0 else 0
            print(f"  {reason:<15} {c['count']:>5} trades, PnL=${c['pnl']:.2f}, Avg=${avg:.2f}/trade, WR={wr:.1f}%")

    # ── Reversal strength distribution ──
    print(f"\n{'='*80}")
    print("REVERSAL STRENGTH DISTRIBUTION")
    print(f"{'='*80}")

    rev_trades = [t for t in all_trades if t.get("has_reversal")]
    if rev_trades:
        strengths = [t.get("reversal_strength", 0) for t in rev_trades]
        print(f"  Reversal trades: {len(rev_trades)}")
        print(f"  Strength range: {min(strengths):.2f} → {max(strengths):.2f}")
        print(f"  Strength mean: {np.mean(strengths):.2f}")

        for lo, hi, label in [(0.0, 0.3, "low"), (0.3, 0.6, "medium"), (0.6, 1.0, "high")]:
            bucket = [t for t in rev_trades if lo <= t.get("reversal_strength", 0) < hi]
            if bucket:
                n = len(bucket)
                wins = sum(1 for t in bucket if t.get("pnl", 0) > 0)
                wr = wins / n * 100
                pnl = sum(t.get("pnl", 0) for t in bucket)
                print(f"  Strength {lo:.1f}-{hi:.1f} ({label:>6}): {n:>4} trades, WR={wr:>5.1f}%, PnL=${pnl:>8.2f}")
    else:
        print("  No reversal trades found")

    # ── D1 trend direction breakdown ──
    print(f"\n{'='*80}")
    print("DIRECTION × D1 TREND BREAKDOWN")
    print(f"{'='*80}")

    for direction in ["BUY", "SELL"]:
        for d1 in ["bullish", "bearish", "None"]:
            subset = [t for t in all_trades
                      if (SignalType.BUY if direction == "BUY" else SignalType.SELL) == t["direction"]
                      and str(t.get("d1_trend")) == d1]
            if subset:
                n = len(subset)
                wins = sum(1 for t in subset if t.get("pnl", 0) > 0)
                wr = wins / n * 100 if n > 0 else 0
                pnl = sum(t.get("pnl", 0) for t in subset)
                label = "TREND-ALIGNED" if (direction == "BUY" and d1 == "bullish") or (direction == "SELL" and d1 == "bearish") else "COUNTER-TREND" if d1 in ["bullish", "bearish"] else "NEUTRAL"
                print(f"  {direction:>4} in {d1:>8} ({label:>14}): {n:>4} trades, WR={wr:>5.1f}%, PnL=${pnl:>8.2f}")

    # ── Summary ──
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    ta_n = len(ta["trades"])
    rev_n = len(rev["trades"])
    ct_n = len(ct["trades"])
    ta_wr = ta["wins"] / ta_n * 100 if ta_n > 0 else 0
    rev_wr = rev["wins"] / rev_n * 100 if rev_n > 0 else 0
    ct_wr = ct["wins"] / ct_n * 100 if ct_n > 0 else 0
    profit_factor = total_pnl / abs(sum(t["pnl"] for t in all_trades if t["pnl"] < 0)) if any(t["pnl"] < 0 for t in all_trades) else float("inf")

    print(f"  Total trades: {total_trades}")
    print(f"  Overall WR: {total_wr:.1f}%")
    print(f"  Total PnL: ${total_pnl:.2f}")
    print(f"  Profit Factor: {profit_factor:.2f}")
    print(f"  Final equity: ${equity:.2f} (started ${starting_equity:.2f}, ROI={((equity-starting_equity)/starting_equity*100):.1f}%)")
    print()

    # Key insight
    best_cat = max(
        [("Trend-aligned", ta_wr, ta_n), ("Reversal", rev_wr, rev_n), ("Counter-trend", ct_wr, ct_n)],
        key=lambda x: x[1]
    )
    worst_cat = min(
        [("Trend-aligned", ta_wr, ta_n), ("Reversal", rev_wr, rev_n), ("Counter-trend", ct_wr, ct_n)],
        key=lambda x: x[1]
    )
    print(f"  ✅ Best category:  {best_cat[0]} WR={best_cat[1]:.1f}% ({best_cat[2]} trades)")
    print(f"  ❌ Worst category: {worst_cat[0]} WR={worst_cat[1]:.1f}% ({worst_cat[2]} trades)")
    if rev_n > 0 and ct_n > 0:
        if rev_wr > ct_wr:
            print(f"  📈 Reversal trades OUTPERFORM plain counter-trend ({rev_wr:.1f}% vs {ct_wr:.1f}%)")
        else:
            print(f"  ⚠️  Reversal trades UNDERPERFORM plain counter-trend ({rev_wr:.1f}% vs {ct_wr:.1f}%)")
            print(f"      → ML should learn: trend_alignment=2 (reversal) = AVOID")


def main():
    parser = argparse.ArgumentParser(description="Backtest reversal signal on premium data")
    parser.add_argument("--start", default="2024-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-12-31", help="End date (YYYY-MM-DD)")
    parser.add_argument("--confidence", type=float, default=0.55, help="Min confidence threshold")
    parser.add_argument("--atr", type=float, default=2.0, help="ATR multiplier for SL")
    parser.add_argument("--rr", type=float, default=2.5, help="Risk-reward ratio for TP")
    parser.add_argument("--risk", type=float, default=0.01, help="Risk per trade (fraction)")
    parser.add_argument("--equity", type=float, default=1000.0, help="Starting equity")
    args = parser.parse_args()

    data_dir = Path(__file__).parent.parent / "data" / "xau-data"

    print(f"Loading premium data from {data_dir}...")
    m5_df = load_timeframe(data_dir, "M5")
    h4_df = load_timeframe(data_dir, "H4")
    d1_df = load_timeframe(data_dir, "D1")

    print(f"M5: {len(m5_df)} candles ({m5_df.index[0]} → {m5_df.index[-1]})")
    print(f"H4: {len(h4_df)} candles ({h4_df.index[0]} → {h4_df.index[-1]})")
    print(f"D1: {len(d1_df)} candles ({d1_df.index[0]} → {d1_df.index[-1]})")

    # Ensure UTC timezone
    for df in [m5_df, h4_df, d1_df]:
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")

    run_backtest(
        m5_df=m5_df,
        d1_df=d1_df,
        h4_df=h4_df,
        start_date=args.start,
        end_date=args.end,
        min_confidence=args.confidence,
        atr_multiplier=args.atr,
        risk_reward_ratio=args.rr,
        risk_per_trade=args.risk,
        starting_equity=args.equity,
    )


if __name__ == "__main__":
    main()