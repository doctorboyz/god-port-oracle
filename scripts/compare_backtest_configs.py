#!/usr/bin/env python3
"""Compare backtest results across multiple account configs and ML filter versions.

Uses pre-computed indicator data for speed. Runs each config (A, B, C) with
v4 and v5 ML filters, plus no ML filter baseline.

Usage:
    python scripts/compare_backtest_configs.py
    python scripts/compare_backtest_configs.py --start 2024-06-01 --end 2026-04-01
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import argparse
from dataclasses import dataclass
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
    calculate_weighted_score,
)
from shared.models import SignalType


@dataclass
class AccountConfig:
    name: str
    atr_multiplier: float
    risk_reward_ratio: float
    min_confidence: float
    tp1_ratio: float
    rr_scale_in: float


# Current VPS configs
ACCOUNTS = [
    AccountConfig("A", atr_multiplier=3.0, risk_reward_ratio=3.0, min_confidence=0.35, tp1_ratio=0.5, rr_scale_in=2.5),
    AccountConfig("B", atr_multiplier=2.5, risk_reward_ratio=2.5, min_confidence=0.45, tp1_ratio=0.5, rr_scale_in=2.5),
    AccountConfig("C", atr_multiplier=2.0, risk_reward_ratio=2.0, min_confidence=0.60, tp1_ratio=0.5, rr_scale_in=2.5),
]


def precompute_indicators(m5_df: pd.DataFrame) -> pd.DataFrame:
    """Pre-compute all indicators on full M5 data."""
    df = m5_df.copy()
    df["atr"] = calculate_atr(df["high"], df["low"], df["close"], period=14)
    df["ema10"] = calculate_ema(df["close"], 10)
    df["ema20"] = calculate_ema(df["close"], 20)
    df["ema50"] = calculate_ema(df["close"], 50)
    df["rsi"] = calculate_rsi(df["close"], period=14)
    macd = calculate_macd(df["close"])
    df["macd_hist"] = macd.histogram
    boll = calculate_bollinger(df["close"], period=20, std_dev=2.0)
    df["boll_bw"] = (boll.upper - boll.lower) / boll.middle.replace(0, np.nan)
    band_range = boll.upper - boll.lower
    df["boll_pct_b"] = np.where(band_range > 0, (df["close"] - boll.lower) / band_range, 0.5)
    stoch = calculate_stochastic(df["high"], df["low"], df["close"], k_period=14, d_period=3)
    df["stoch_k"] = stoch.k_line
    adx_s, pdi_s, mdi_s = calculate_adx(df["high"], df["low"], df["close"], period=14)
    df["adx"] = adx_s
    df["plus_di"] = pdi_s
    df["minus_di"] = mdi_s
    df["mfi"] = calculate_mfi(df["high"], df["low"], df["close"], df["volume"], period=14)
    df["vol_ema20"] = calculate_ema(df["volume"], 20)
    return df


def precompute_trends(d1_df: pd.DataFrame, h4_df: pd.DataFrame):
    d1_ema50 = calculate_ema(d1_df["close"], 50)
    d1_ema200 = calculate_ema(d1_df["close"], 200)
    d1_trend = pd.Series(index=d1_df.index, dtype=object)
    for i in range(len(d1_df)):
        if pd.notna(d1_ema50.iloc[i]) and pd.notna(d1_ema200.iloc[i]):
            d1_trend.iloc[i] = "bullish" if d1_ema50.iloc[i] > d1_ema200.iloc[i] else "bearish"
        else:
            d1_trend.iloc[i] = None
    d1_trend = d1_trend.dropna()

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


def get_trend_at(trend_series, timestamp):
    valid = trend_series[trend_series.index <= timestamp]
    if len(valid) > 0:
        return valid.iloc[-1]
    return None


# Regime filters (matching generator.py constants)
REGIME_RANGING_CONFIDENCE_MULT = 0.3   # Reduce confidence by 70% in ranging
REGIME_VOLATILE_SKIP = True             # Skip signals entirely in volatile regime
COUNTER_TREND_CONFIDENCE_MULT = 0.5    # Reduce confidence by 50% for counter-trend

# Lowered reversal thresholds (matching generator.py v6)
REVERSAL_OB_RSI = 65
REVERSAL_OS_RSI = 35
REVERSAL_OB_STOCH = 75
REVERSAL_OS_STOCH = 25
REVERSAL_OB_BOLL = 0.80
REVERSAL_OS_BOLL = 0.20
REVERSAL_OB_MFI = 75
REVERSAL_OS_MFI = 25


def generate_signal_from_row(row, d1_trend, h4_trend, min_confidence):
    """Generate signal from pre-computed indicator values with regime filters."""
    adx_val = row.get("adx")
    pdi_val = row.get("plus_di")
    mdi_val = row.get("minus_di")
    rsi_val = row.get("rsi")
    macd_hist = row.get("macd_hist")
    boll_pct_b = row.get("boll_pct_b")
    boll_bw = row.get("boll_bw")
    stoch_k = row.get("stoch_k")
    mfi_val = row.get("mfi")
    atr_val = row.get("atr")

    if pd.isna(adx_val) or pd.isna(atr_val):
        return None

    # ── Volatile regime: skip entirely ──
    regime = classify_regime(float(adx_val), float(boll_bw) if pd.notna(boll_bw) else None)
    if REGIME_VOLATILE_SKIP and regime == "volatile":
        return None  # Skip volatile signals

    scores = {}
    # ADX
    if pd.notna(adx_val) and pd.notna(pdi_val) and pd.notna(mdi_val):
        if adx_val >= 25:
            scores["adx"] = (1.0 if pdi_val > mdi_val else -1.0) if adx_val >= 50 else (0.5 if pdi_val > mdi_val else -0.5)
        elif adx_val >= 20:
            scores["adx"] = 0.3 if pdi_val > mdi_val else -0.3
        else:
            scores["adx"] = 0.0
    # MACD
    if pd.notna(macd_hist):
        scores["macd"] = min(macd_hist / 2.0, 1.0) if macd_hist > 0 else max(macd_hist / 2.0, -1.0)
    # Boll
    if pd.notna(boll_pct_b):
        if boll_pct_b >= 0.85:
            scores["boll"] = min((boll_pct_b - 0.5) / 0.5, 1.0)
        elif boll_pct_b <= 0.15:
            scores["boll"] = max((boll_pct_b - 0.5) / 0.5, -1.0)
        else:
            scores["boll"] = (boll_pct_b - 0.5) / 0.5 * 0.5
    # Stoch
    if pd.notna(stoch_k):
        if stoch_k > 80: scores["stoch"] = -0.5
        elif stoch_k < 20: scores["stoch"] = 0.5
        else: scores["stoch"] = 0.0
    # RSI
    if pd.notna(rsi_val):
        if rsi_val > 70: scores["rsi"] = -0.5
        elif rsi_val < 30: scores["rsi"] = 0.5
        else: scores["rsi"] = 0.0
    # Volume
    vol_ema = row.get("vol_ema20")
    vol = row.get("volume")
    if pd.notna(vol_ema) and pd.notna(vol) and vol_ema > 0:
        ratio = vol / vol_ema
        scores["volume"] = 0.5 if ratio > 1.5 else (-0.3 if ratio < 0.5 else 0.0)
    # MFI
    if pd.notna(mfi_val):
        if mfi_val > 80: scores["mfi"] = -0.5
        elif mfi_val < 20: scores["mfi"] = 0.5
        else: scores["mfi"] = 0.0

    weighted_score = calculate_weighted_score(scores)
    confidence = abs(weighted_score)

    # Note: regime already computed above (volatile already filtered)
    # Apply ranging confidence penalty
    if regime == "ranging":
        confidence *= REGIME_RANGING_CONFIDENCE_MULT

    if confidence < min_confidence:
        signal_type = SignalType.HOLD
    elif weighted_score > 0:
        signal_type = SignalType.BUY
    else:
        signal_type = SignalType.SELL

    # Reversal
    direction = signal_type.value if signal_type != SignalType.HOLD else None
    has_reversal, reversal_strength = False, 0.0
    trend_alignment = 0
    if signal_type != SignalType.HOLD:
        has_reversal, reversal_strength = compute_reversal_signal(
            direction=direction, d1_trend=d1_trend, h4_trend=h4_trend,
            rsi=float(rsi_val) if pd.notna(rsi_val) else None,
            stoch_k=float(stoch_k) if pd.notna(stoch_k) else None,
            boll_pct_b=float(boll_pct_b) if pd.notna(boll_pct_b) else None,
            mfi=float(mfi_val) if pd.notna(mfi_val) else None,
            macd_hist=float(macd_hist) if pd.notna(macd_hist) else None,
            plus_di=float(pdi_val) if pd.notna(pdi_val) else None,
            minus_di=float(mdi_val) if pd.notna(mdi_val) else None,
            boll_bw=float(boll_bw) if pd.notna(boll_bw) else None,
        )
        trend_alignment = compute_trend_alignment_value(direction, d1_trend, h4_trend, has_reversal)

        # Counter-trend penalty (trend_alignment == -1 means no reversal evidence)
        if trend_alignment == -1:
            confidence *= COUNTER_TREND_CONFIDENCE_MULT

        # Re-check confidence after penalties
        if confidence < min_confidence:
            signal_type = SignalType.HOLD
            direction = None
            trend_alignment = 0

    return {
        "signal_type": signal_type,
        "confidence": confidence,
        "regime": regime,
        "direction": direction,
        "has_reversal": has_reversal,
        "reversal_strength": reversal_strength,
        "trend_alignment": trend_alignment,
        "atr": float(atr_val) if pd.notna(atr_val) else 5.0,
    }


def run_config(m5_ind, d1_trend_s, h4_trend_s, config: AccountConfig,
               start_idx=200, max_holding=288, cooldown=12, contract_size=100.0, equity=1000.0):
    """Run backtest for one config."""
    trades = []
    position = None
    last_exit = -cooldown - 1
    total = len(m5_ind)

    for i in range(start_idx, total):
        row = m5_ind.iloc[i]
        if position is not None:
            high = float(row["high"])
            low = float(row["close"])
            close = float(row["close"])
            bars_held = i - position["entry_idx"]

            if bars_held >= max_holding:
                pnl = _calc_pnl(position, close, contract_size)
                position["exit_price"] = close
                position["pnl"] = pnl
                position["exit_reason"] = "max_holding"
                trades.append(position)
                equity += pnl
                position = None
                last_exit = i
                continue

            if position["direction"] == SignalType.BUY:
                if low <= position["sl"]:
                    pnl = _calc_pnl(position, position["sl"], contract_size)
                    position["exit_price"] = position["sl"]
                    position["pnl"] = pnl
                    position["exit_reason"] = "stop_loss"
                    trades.append(position)
                    equity += pnl
                    position = None
                    last_exit = i
                    continue
                elif high >= position["tp"]:
                    pnl = _calc_pnl(position, position["tp"], contract_size)
                    position["exit_price"] = position["tp"]
                    position["pnl"] = pnl
                    position["exit_reason"] = "take_profit"
                    trades.append(position)
                    equity += pnl
                    position = None
                    last_exit = i
                    continue
            else:
                if high >= position["sl"]:
                    pnl = _calc_pnl(position, position["sl"], contract_size)
                    position["exit_price"] = position["sl"]
                    position["pnl"] = pnl
                    position["exit_reason"] = "stop_loss"
                    trades.append(position)
                    equity += pnl
                    position = None
                    last_exit = i
                    continue
                elif low <= position["tp"]:
                    pnl = _calc_pnl(position, position["tp"], contract_size)
                    position["exit_price"] = position["tp"]
                    position["pnl"] = pnl
                    position["exit_reason"] = "take_profit"
                    trades.append(position)
                    equity += pnl
                    position = None
                    last_exit = i
                    continue

        if position is None and (i - last_exit >= cooldown):
            d1_trend = get_trend_at(d1_trend_s, m5_ind.index[i])
            h4_trend = get_trend_at(h4_trend_s, m5_ind.index[i])
            sig = generate_signal_from_row(row, d1_trend, h4_trend, config.min_confidence)
            if sig is None or sig["signal_type"] == SignalType.HOLD or sig["confidence"] < config.min_confidence:
                continue

            price = float(row["close"])
            atr = sig["atr"]
            direction = sig["direction"]
            sl = calculate_stop_loss(price, atr, direction, config.atr_multiplier, 2.0)
            tp = calculate_take_profit(price, sl, direction, config.risk_reward_ratio)
            lots = calculate_position_size(equity, 0.01, price, sl, contract_size)
            if lots <= 0:
                continue

            position = {
                "entry_idx": i, "entry_price": price,
                "direction": SignalType.BUY if direction == "BUY" else SignalType.SELL,
                "sl": sl, "tp": tp, "lot_size": lots,
                "confidence": sig["confidence"], "regime": sig["regime"],
                "d1_trend": d1_trend, "h4_trend": h4_trend,
                "has_reversal": sig["has_reversal"],
                "reversal_strength": sig["reversal_strength"],
                "trend_alignment": sig["trend_alignment"],
            }

    return trades, equity


def _calc_pnl(pos, exit_price, contract_size):
    if pos["direction"] == SignalType.BUY:
        return (exit_price - pos["entry_price"]) * pos["lot_size"] * contract_size
    return (pos["entry_price"] - exit_price) * pos["lot_size"] * contract_size


def summarize(name, trades, equity, starting=1000.0):
    """Print summary for one config."""
    if not trades:
        print(f"  {name}: No trades")
        return

    n = len(trades)
    wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
    wr = wins / n * 100
    pnl = sum(t.get("pnl", 0) for t in trades)
    tp = sum(1 for t in trades if t.get("exit_reason") == "take_profit")
    sl = sum(1 for t in trades if t.get("exit_reason") == "stop_loss")
    mh = sum(1 for t in trades if t.get("exit_reason") == "max_holding")
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Category breakdown
    ta_wr = ta_n = rev_wr = rev_n = ct_wr = ct_n = 0
    for t in trades:
        ta = t.get("trend_alignment", 0)
        pnl_t = t.get("pnl", 0)
        if ta == 1:
            ta_n += 1
            if pnl_t > 0: ta_wr += 1
        elif ta == 2:
            rev_n += 1
            if pnl_t > 0: rev_wr += 1
        elif ta == -1:
            ct_n += 1
            if pnl_t > 0: ct_wr += 1

    print(f"\n  ╔══ {name} ══╗")
    print(f"  ║ Trades: {n} | WR: {wr:.1f}% | PnL: ${pnl:.2f} | PF: {pf:.2f}")
    print(f"  ║ TP: {tp} | SL: {sl} | MaxHold: {mh} | Equity: ${equity:.2f}")
    print(f"  ║ Trend-aligned: {ta_n} ({ta_wr/max(ta_n,1)*100:.1f}%) | Counter: {ct_n} ({ct_wr/max(ct_n,1)*100:.1f}%) | Reversal: {rev_n}")
    print(f"  ╚═══════════════╝")
    return {"name": name, "trades": n, "wr": wr, "pnl": pnl, "pf": pf, "equity": equity}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2026-12-31")
    args = parser.parse_args()

    data_dir = Path(__file__).parent.parent / "data" / "xau-data"
    print("Loading premium data...")
    m5_df = load_timeframe(data_dir, "M5")
    h4_df = load_timeframe(data_dir, "H4")
    d1_df = load_timeframe(data_dir, "D1")
    for df in [m5_df, h4_df, d1_df]:
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")

    cutoff_start = pd.Timestamp(args.start, tz="UTC")
    cutoff_end = pd.Timestamp(args.end, tz="UTC")
    m5_filtered = m5_df[(m5_df.index >= cutoff_start) & (m5_df.index <= cutoff_end)]

    print(f"\nM5: {len(m5_filtered)} candles, {m5_filtered.index[0]} → {m5_filtered.index[-1]}")
    print("Pre-computing indicators...")
    m5_ind = precompute_indicators(m5_filtered)
    d1_trend_s, h4_trend_s = precompute_trends(d1_df, h4_df)

    print(f"\n{'='*60}")
    print("MULTI-CONFIG BACKTEST COMPARISON")
    print(f"Period: {args.start} → {args.end} | Data: Premium M5")
    print(f"{'='*60}")

    results = []
    for config in ACCOUNTS:
        trades, equity = run_config(m5_ind, d1_trend_s, h4_trend_s, config)
        r = summarize(f"Account {config.name} (ATR={config.atr_multiplier}, RR={config.risk_reward_ratio}, conf≥{config.min_confidence})", trades, equity)
        if r:
            results.append(r)

    # Comparison table
    if results:
        print(f"\n{'='*60}")
        print("COMPARISON TABLE")
        print(f"{'='*60}")
        print(f"{'Config':<45} {'Trades':>7} {'WR%':>6} {'PnL':>10} {'PF':>6} {'Equity':>10}")
        print(f"{'-'*45} {'-'*7} {'-'*6} {'-'*10} {'-'*6} {'-'*10}")
        for r in results:
            print(f"{r['name']:<45} {r['trades']:>7} {r['wr']:>5.1f}% ${r['pnl']:>8.2f} {r['pf']:>5.2f} ${r['equity']:>8.2f}")


if __name__ == "__main__":
    main()