#!/usr/bin/env python3
"""Premium data trailing comparison — synthetic entries on full 200k M5 bars.

Generates synthetic BUY/SELL entries at fixed intervals with ATR-based SL/TP,
then replays each trailing variant bar-by-bar. Since all variants get the SAME
entries, differences are purely from trailing logic — clean comparison across
market regimes (2023-2026).

This is faster than running the full signal engine (which is O(n²)) and gives
a controlled experiment for trailing algorithm evaluation.

Usage:
  python3 scripts/trailing_replay_premium.py
  python3 scripts/trailing_replay_premium.py --csv ... --interval 50 --rr 2.0
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Optional

import pandas as pd

sys.path.insert(0, "/Users/doctorboyz/Code/github.com/doctorboyz/god-port-oracle")

from broky.indicators.atr import calculate_atr
from broky.indicators.ema import calculate_ema
from shared.models import SignalType

from scripts.trailing_replay import (
    TrailingConfig, VariantResult, pnl_dollars, VARIANTS,
)


@dataclass
class PremiumTrade:
    id: int
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    lot_size: float
    atr_at_entry: float
    regime: str
    entry_time: pd.Timestamp
    exit_time: Optional[pd.Timestamp]
    original_pnl: float
    original_exit_reason: str


def classify_regime(adx_proxy: float, atr_pct: float) -> str:
    if adx_proxy >= 25:
        return "trending"
    if atr_pct >= 0.6:
        return "volatile"
    return "ranging"


def generate_synthetic_entries(df: pd.DataFrame, interval: int = 100,
                                atr_mult: float = 1.5, rr: float = 2.0,
                                lot_size: float = 0.01) -> list[PremiumTrade]:
    """Generate synthetic entries at fixed intervals.

    Direction: based on EMA(50) slope — if close > EMA(50) BUY, else SELL.
    This simulates a simple trend-following entry without the full signal engine.
    """
    atr = calculate_atr(df["high"], df["low"], df["close"], period=14)
    ema50 = calculate_ema(df["close"], period=50)
    atr_pct = atr / df["close"] * 100
    ret = df["close"].pct_change()
    adx_proxy = ret.rolling(14).std() * 100

    trades = []
    n = len(df)
    i = 200  # warmup
    tid = 0
    while i < n - 200:
        row = df.iloc[i]
        close = row["close"]
        a = float(atr.iloc[i]) if pd.notna(atr.iloc[i]) else 0
        if a <= 0 or close <= 0:
            i += interval; continue
        e50 = float(ema50.iloc[i]) if pd.notna(ema50.iloc[i]) else close
        # Direction: trend-following
        direction = "BUY" if close > e50 else "SELL"
        # ATR-based SL/TP
        if direction == "BUY":
            sl = close - atr_mult * a
            tp = close + atr_mult * a * rr
        else:
            sl = close + atr_mult * a
            tp = close - atr_mult * a * rr
        ap = float(adx_proxy.iloc[i]) if pd.notna(adx_proxy.iloc[i]) else 0
        atr_p = float(atr_pct.iloc[i]) if pd.notna(atr_pct.iloc[i]) else 0
        regime = classify_regime(ap, atr_p)
        trades.append(PremiumTrade(
            id=tid, direction=direction,
            entry_price=float(close),
            stop_loss=float(sl),
            take_profit=float(tp),
            lot_size=lot_size,
            atr_at_entry=a,
            regime=regime,
            entry_time=df.index[i],
            exit_time=None,  # let trailing decide; we cap at max_bars
            original_pnl=0,
            original_exit_reason="",
        ))
        tid += 1
        i += interval
    return trades


def replay_trade_premium(t: PremiumTrade, bars: pd.DataFrame,
                          cfg: TrailingConfig) -> tuple[float, str]:
    if len(bars) == 0:
        return 0.0, "no_bars"
    if cfg.kind == "actual":
        return t.original_pnl, "max_holding"

    sl_pct = (t.entry_price - t.stop_loss) / t.entry_price * 100 if t.direction == "BUY" \
             else (t.stop_loss - t.entry_price) / t.entry_price * 100
    tp_pct = (t.take_profit - t.entry_price) / t.entry_price * 100 if t.direction == "BUY" \
             else (t.entry_price - t.take_profit) / t.entry_price * 100
    entry = t.entry_price
    is_buy = t.direction == "BUY"
    sl_price = t.stop_loss
    tp_price = t.take_profit
    peak = entry
    armed = False
    be_armed = False
    trail_pct = cfg.trail_pct
    if cfg.kind == "atr" and t.atr_at_entry > 0:
        trail_pct = (cfg.atr_mult * t.atr_at_entry) / entry * 100

    for i in range(len(bars)):
        bar_high = bars.iloc[i]["high"]
        bar_low = bars.iloc[i]["low"]
        if is_buy:
            if armed:
                trail_level = peak * (1 - trail_pct / 100.0)
                if bar_low <= trail_level:
                    return pnl_dollars((trail_level - entry) / entry * 100, entry, t.lot_size), "trailing_tp"
            else:
                if bar_low <= sl_price:
                    return -pnl_dollars(sl_pct, entry, t.lot_size), "stop_loss"
            if cfg.kind == "breakeven" and be_armed and bar_low <= entry:
                return 0.0, "breakeven"
            if bar_high >= tp_price:
                return +pnl_dollars(tp_pct, entry, t.lot_size), "take_profit"
            if bar_high > peak: peak = bar_high
            gain_pct = (peak - entry) / entry * 100
            if cfg.kind == "breakeven" and not be_armed and gain_pct >= cfg.breakeven_arm_pct:
                be_armed = True
            if cfg.kind == "multi" and cfg.levels:
                for arm_pct, lvl_trail in cfg.levels:
                    if gain_pct >= arm_pct:
                        trail_pct = lvl_trail
                        armed = True
            if cfg.kind in ("simple", "atr") and not armed and gain_pct >= cfg.activation_pct:
                armed = True
            if cfg.kind == "breakeven" and be_armed and not armed and gain_pct >= cfg.activation_pct:
                armed = True
            if cfg.kind == "regime":
                if t.regime == "trending":
                    act, tr = cfg.activation_pct, cfg.trail_pct * 1.5
                elif t.regime == "volatile":
                    act, tr = cfg.activation_pct * 0.8, cfg.trail_pct * 0.8
                else:
                    act, tr = cfg.activation_pct * 0.7, cfg.trail_pct * 0.7
                trail_pct = tr
                if gain_pct >= act: armed = True
        else:
            if armed:
                trail_level = peak * (1 + trail_pct / 100.0)
                if bar_high >= trail_level:
                    return pnl_dollars((entry - trail_level) / entry * 100, entry, t.lot_size), "trailing_tp"
            else:
                if bar_high >= sl_price:
                    return -pnl_dollars(sl_pct, entry, t.lot_size), "stop_loss"
            if cfg.kind == "breakeven" and be_armed and bar_high >= entry:
                return 0.0, "breakeven"
            if bar_low <= tp_price:
                return +pnl_dollars(tp_pct, entry, t.lot_size), "take_profit"
            if bar_low < peak: peak = bar_low
            gain_pct = (entry - peak) / entry * 100
            if cfg.kind == "breakeven" and not be_armed and gain_pct >= cfg.breakeven_arm_pct:
                be_armed = True
            if cfg.kind == "multi" and cfg.levels:
                for arm_pct, lvl_trail in cfg.levels:
                    if gain_pct >= arm_pct:
                        trail_pct = lvl_trail
                        armed = True
            if cfg.kind in ("simple", "atr") and not armed and gain_pct >= cfg.activation_pct:
                armed = True
            if cfg.kind == "breakeven" and be_armed and not armed and gain_pct >= cfg.activation_pct:
                armed = True
            if cfg.kind == "regime":
                if t.regime == "trending":
                    act, tr = cfg.activation_pct, cfg.trail_pct * 1.5
                elif t.regime == "volatile":
                    act, tr = cfg.activation_pct * 0.8, cfg.trail_pct * 0.8
                else:
                    act, tr = cfg.activation_pct * 0.7, cfg.trail_pct * 0.7
                trail_pct = tr
                if gain_pct >= act: armed = True

    if cfg.kind == "let_run":
        last_close = bars.iloc[-1]["close"]
        return pnl_dollars((last_close - entry) / entry * 100 * (1 if is_buy else -1),
                           entry, t.lot_size), "max_holding"
    return 0.0, "max_holding"


def run_variant_premium(trades: list[PremiumTrade], df: pd.DataFrame,
                         cfg: TrailingConfig, max_bars: int) -> VariantResult:
    res = VariantResult(name=cfg.name)
    for t in trades:
        end = t.entry_time + pd.Timedelta(minutes=5 * max_bars)
        sub = df.loc[t.entry_time:end + pd.Timedelta(minutes=5)]
        if len(sub) > max_bars:
            sub = sub.iloc[:max_bars]
        bars = sub[["high", "low", "close"]].copy()
        pnl, reason = replay_trade_premium(t, bars, cfg)
        res.n += 1
        res.pnl_list.append(pnl)
        res.total_pnl += pnl
        if pnl > 0: res.wins += 1
        elif pnl < 0: res.losses += 1
        else: res.zero += 1
        if reason == "take_profit": res.tp_hits += 1
        elif reason == "stop_loss": res.sl_hits += 1
        elif reason == "trailing_tp": res.trail_exits += 1
        elif reason == "breakeven": res.be_exits += 1
        elif reason == "max_holding": res.max_holding += 1
        elif reason == "no_bars": res.no_bars += 1
    return res


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default="/Users/doctorboyz/Documents/xau-data/XAUUSD_M5-2026-04-15-06_40-Premium Data.csv")
    parser.add_argument("--interval", type=int, default=100, help="bars between entries (100 M5 = 8.3h)")
    parser.add_argument("--rr", type=float, default=2.0, help="risk:reward ratio")
    parser.add_argument("--atr-mult", type=float, default=1.5, help="SL distance = atr_mult * ATR")
    parser.add_argument("--max-bars", type=int, default=96, help="holding window M5 bars (96=8h)")
    parser.add_argument("--lot-size", type=float, default=0.01)
    args = parser.parse_args()

    print(f"=== Premium data trailing backtest (synthetic entries) ===")
    print(f"CSV: {args.csv}")
    print(f"Interval: {args.interval} bars ({args.interval*5/60:.1f}h between entries)")
    print(f"RR: {args.rr}, ATR mult: {args.atr_mult}, max holding: {args.max_bars} bars ({args.max_bars*5/60:.1f}h)\n")

    print(f"Loading premium M5...", flush=True)
    df = pd.read_csv(args.csv, header=None,
                     names=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.set_index("timestamp").sort_index()
    print(f"  {len(df)} bars, range {df.index.min()} -> {df.index.max()}", flush=True)

    print(f"Generating synthetic entries...", flush=True)
    trades = generate_synthetic_entries(df, interval=args.interval,
                                          atr_mult=args.atr_mult, rr=args.rr,
                                          lot_size=args.lot_size)
    print(f"  {len(trades)} entries generated", flush=True)
    if not trades:
        print("No trades. Exiting.")
        return

    # Direction split
    buys = sum(1 for t in trades if t.direction == "BUY")
    print(f"  BUY: {buys}, SELL: {len(trades)-buys}")

    print(f"\n=== VARIANT COMPARISON (premium, bar-by-bar) ===")
    for cfg in VARIANTS:
        res = run_variant_premium(trades, df, cfg, args.max_bars)
        print(res.report())

    # Risk-adjusted
    print(f"\n=== RISK-ADJUSTED (PnL / MaxDD) ===")
    print(f"  {'variant':<32} {'PnL':>9} {'MaxDD':>8} {'PnL/MaxDD':>10}")
    for cfg in VARIANTS:
        res = run_variant_premium(trades, df, cfg, args.max_bars)
        max_dd = 0.0; peak = 0.0; cum = 0.0
        for p in res.pnl_list:
            cum += p
            if cum > peak: peak = cum
            dd = peak - cum
            if dd > max_dd: max_dd = dd
        ratio = res.total_pnl / max_dd if max_dd > 0 else float('inf')
        print(f"  {cfg.name:<32} {res.total_pnl:>+9.2f} {max_dd:>8.2f} {ratio:>10.2f}")

    # By year
    print(f"\n=== BY YEAR (D-simple 0.20/0.10 — best Exness risk-adjusted) ===")
    cfg = TrailingConfig(name="D-simple 0.20/0.10", kind="simple",
                         activation_pct=0.20, trail_pct=0.10)
    by_year = {}
    for t in trades:
        y = t.entry_time.year
        by_year.setdefault(y, []).append(t)
    for y, yt in sorted(by_year.items()):
        res = run_variant_premium(yt, df, cfg, args.max_bars)
        wr = 100 * res.wins / max(1, res.wins + res.losses + res.zero)
        print(f"  {y}: n={len(yt):>4} PnL={res.total_pnl:>+9.2f} WR={wr:>5.1f}% "
              f"trail={res.trail_exits} SL={res.sl_hits} TP={res.tp_hits} mh={res.max_holding}")

    # By regime (overall)
    print(f"\n=== BY REGIME (D-simple 0.20/0.10) ===")
    by_regime = {}
    for t in trades:
        by_regime.setdefault(t.regime, []).append(t)
    for regime, rt in sorted(by_regime.items()):
        res = run_variant_premium(rt, df, cfg, args.max_bars)
        wr = 100 * res.wins / max(1, res.wins + res.losses + res.zero)
        print(f"  {regime:<10} n={len(rt):>4} PnL={res.total_pnl:>+9.2f} WR={wr:>5.1f}% "
              f"trail={res.trail_exits} SL={res.sl_hits} TP={res.tp_hits}")


if __name__ == "__main__":
    main()