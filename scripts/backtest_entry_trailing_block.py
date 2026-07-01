#!/usr/bin/env python3
"""End-to-end backtest: trend-aligned reversal entry + trailing TP + risk blocks.

Combines the three pieces verified separately:
  - Entry: trend-aligned reversal H2 (pullback ≥0.40%) or H3 (≥0.50%)
    (from trend_aligned_reversal_eval.py — P=92-94%)
  - Trailing TP: D 0.20/0.10 (activation 0.20%, trail 0.10%)
    (from trailing_replay_premium.py — most robust variant)
  - Blocks: DrawdownProtector + CircuitBreaker + dynamic max_positions + TradeBlocker
    (from verify_blocks.py — 43/43 PASS)

Runs across 5 starting balances: $100, $200, $500, $1000, $10000 to test survival
and scaling. Reports per balance: trades, WR, PF, PnL, MaxDD, block-fired counts.

Usage:
  python3 scripts/backtest_entry_trailing_block.py
  python3 scripts/backtest_entry_trailing_block.py --method H3 --risk-pct 0.01
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, "/Users/doctorboyz/Code/github.com/doctorboyz/god-port-oracle")

from broky.risk.sizing import risk_per_trade_size
from broky.risk.drawdown_protection import DrawdownProtector
from broky.risk.circuit_breaker import CircuitBreaker
from broky.risk.trade_blocker import TradeBlocker, BlockInput

from scripts.trend_aligned_reversal_eval import (
    classify_trend, find_swings, label_structure,
    find_trend_aligned_reversals, resample_timeframe,
)
from scripts.trailing_replay import TrailingConfig, pnl_dollars


# ---------- Config ----------

CONTRACT_SIZE = 100  # XAUUSD: 1 lot = 100 oz

TRAIL_CFG = TrailingConfig(name="D 0.20/0.10", kind="simple",
                            activation_pct=0.20, trail_pct=0.10)

# Far TP so trailing can arm and lock profit before TP hit
TP_PCT = 2.0
MAX_HOLD_BARS = 288  # 24h of M5


# ---------- Backtest trade ----------

@dataclass
class BacktestTrade:
    entry_bar: int
    direction: str           # "BUY" / "SELL"
    entry_price: float
    stop_loss: float
    take_profit: float
    lots: float
    pullback_pct: float
    entry_time: pd.Timestamp
    exit_price: float = 0.0
    exit_reason: str = ""
    pnl: float = 0.0
    bars_held: int = 0


@dataclass
class BacktestResult:
    balance_start: float
    balance_end: float
    trades: list[BacktestTrade] = field(default_factory=list)
    block_counts: Counter = field(default_factory=Counter)
    peak_equity: float = 0.0
    max_dd: float = 0.0

    @property
    def n_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl < 0)

    @property
    def win_rate(self) -> float:
        d = self.wins + self.losses
        return self.wins / d * 100 if d else 0.0

    @property
    def gross_profit(self) -> float:
        return sum(t.pnl for t in self.trades if t.pnl > 0)

    @property
    def gross_loss(self) -> float:
        return -sum(t.pnl for t in self.trades if t.pnl < 0)

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / self.gross_loss if self.gross_loss > 0 else float('inf')

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def max_dd_pct(self) -> float:
        return self.max_dd / self.balance_start * 100 if self.balance_start > 0 else 0.0


# ---------- Replay trade with trailing TP ----------

def replay_trade(t: BacktestTrade, bars: pd.DataFrame,
                  cfg: TrailingConfig) -> tuple[float, str, int]:
    """Bar-by-bar replay with trailing TP. Returns (pnl, exit_reason, bars_held)."""
    if len(bars) == 0:
        return 0.0, "no_bars", 0

    is_buy = t.direction == "BUY"
    entry = t.entry_price
    sl_price = t.stop_loss
    tp_price = t.take_profit
    peak = entry
    armed = False
    trail_pct = cfg.trail_pct

    high = bars["high"].values
    low = bars["low"].values
    n = len(bars)

    for i in range(n):
        bar_high = high[i]
        bar_low = low[i]
        if is_buy:
            if armed:
                trail_level = peak * (1 - trail_pct / 100.0)
                if bar_low <= trail_level:
                    pnl = pnl_dollars((trail_level - entry) / entry * 100, entry, t.lots)
                    return pnl, "trailing_tp", i + 1
            else:
                if bar_low <= sl_price:
                    sl_pct = (t.entry_price - t.stop_loss) / t.entry_price * 100
                    return -pnl_dollars(sl_pct, entry, t.lots), "stop_loss", i + 1
            if bar_high >= tp_price:
                tp_pct = (t.take_profit - t.entry_price) / t.entry_price * 100
                return +pnl_dollars(tp_pct, entry, t.lots), "take_profit", i + 1
            if bar_high > peak:
                peak = bar_high
            gain_pct = (peak - entry) / entry * 100
            if not armed and gain_pct >= cfg.activation_pct:
                armed = True
        else:
            if armed:
                trail_level = peak * (1 + trail_pct / 100.0)
                if bar_high >= trail_level:
                    pnl = pnl_dollars((entry - trail_level) / entry * 100, entry, t.lots)
                    return pnl, "trailing_tp", i + 1
            else:
                if bar_high >= sl_price:
                    sl_pct = (t.stop_loss - t.entry_price) / t.entry_price * 100
                    return -pnl_dollars(sl_pct, entry, t.lots), "stop_loss", i + 1
            if bar_low <= tp_price:
                tp_pct = (t.entry_price - t.take_profit) / t.entry_price * 100
                return +pnl_dollars(tp_pct, entry, t.lots), "take_profit", i + 1
            if bar_low < peak:
                peak = bar_low
            gain_pct = (entry - peak) / entry * 100
            if not armed and gain_pct >= cfg.activation_pct:
                armed = True

    # max holding — exit at last close
    last_close = bars["close"].values[-1]
    if is_buy:
        pnl = pnl_dollars((last_close - entry) / entry * 100, entry, t.lots)
    else:
        pnl = pnl_dollars((entry - last_close) / entry * 100, entry, t.lots)
    return pnl, "max_holding", n


# ---------- Backtest per balance ----------

def _calc_max_positions(equity: float, cap: int = 5, per_pos: float = 200) -> int:
    if equity <= 0:
        return 1
    return max(1, min(cap, int(equity // per_pos)))


def run_backtest(signals: list[tuple[int, str]], df: pd.DataFrame,
                  reversals: list, balance_start: float,
                  risk_pct: float = 0.01, method: str = "H2",
                  learning_mode: bool = False) -> BacktestResult:
    """Run backtest for one starting balance.

    signals: list of (bar_idx, direction) where direction is "UP"/"DOWN"
    reversals: list of Reversal objects (for pullback_pct lookup)
    """
    # Build pullback map: (bar_idx+3, dir) -> pullback_pct
    pullback_map: dict[tuple[int, str], float] = {}
    dir_map = {"BUY": "UP", "SELL": "DOWN"}
    for rev in reversals:
        key = (rev.bar_idx + 3, dir_map[rev.direction])
        pullback_map[key] = rev.reversal_size_pct

    # Sort signals by bar index
    signals_sorted = sorted(signals, key=lambda x: x[0])

    # State
    equity = balance_start
    initial_equity = balance_start
    peak_equity = balance_start
    max_dd = 0.0

    # Block instances (fresh per backtest)
    dp = DrawdownProtector(initial_equity=initial_equity,
                           daily_limit_pct=0.20, weekly_limit_pct=0.30,
                           account_limit_pct=0.30)
    cb = CircuitBreaker(daily_loss_limit_pct=0.05, consecutive_loss_limit=5,
                         cooldown_minutes=15)
    tb = TradeBlocker(daily_trade_count_limit=20, weekly_trade_count_limit=80,
                       hard_max_lots=0.50, max_risk_pct=0.05,
                       min_sl_distance_pct=0.05, max_sl_distance_pct=5.0,
                       margin_safety_factor=0.8)

    # Track open position (one at a time) — open_until_bar is when the trade
    # actually exited (sig_bar + bars_held), not the max holding window.
    open_until_bar = -1

    # Daily/weekly tracking
    current_day = None
    daily_trades = 0
    current_week = None
    weekly_trades = 0

    result = BacktestResult(balance_start=balance_start, balance_end=balance_start)

    close = df["close"].values
    N = len(df)

    # Initialize drawdown protector with starting equity
    dp.check(equity=initial_equity)

    for sig_bar, sig_dir in signals_sorted:
        # Update equity peak / drawdown
        if equity > peak_equity:
            peak_equity = equity
        dd = peak_equity - equity
        if dd > max_dd:
            max_dd = dd
        result.peak_equity = peak_equity
        result.max_dd = max_dd

        # Skip if a position is still open (sig_bar before trade's actual exit bar)
        if sig_bar < open_until_bar:
            continue

        # Day/week rollover — reset CB daily state when day changes
        ts = df.index[sig_bar]
        day = ts.normalize()
        week = ts.isocalendar()[1]
        if current_day is None or day != current_day:
            current_day = day
            daily_trades = 0
            cb.reset_daily()
        if current_week is None or week != current_week:
            current_week = week
            weekly_trades = 0

        # Direction → BUY/SELL
        direction = "BUY" if sig_dir == "UP" else "SELL"
        pullback = pullback_map.get((sig_bar, sig_dir), 0.0)
        # SL distance = pullback threshold (0.40% for H2, 0.50% for H3)
        # Use max(pullback, method_threshold) so SL is at least method threshold
        method_thr = 0.40 if method == "H2" else 0.50
        sl_distance_pct = max(pullback, method_thr) if pullback > 0 else method_thr

        entry_price = close[sig_bar]
        if direction == "BUY":
            sl_price = entry_price * (1 - sl_distance_pct / 100.0)
            tp_price = entry_price * (1 + TP_PCT / 100.0)
        else:
            sl_price = entry_price * (1 + sl_distance_pct / 100.0)
            tp_price = entry_price * (1 - TP_PCT / 100.0)

        # Lot sizing
        lots = risk_per_trade_size(equity, risk_pct, entry_price, sl_price,
                                    CONTRACT_SIZE, min_lots=0.01, max_lots=10.0)

        # Margin (rough estimate — 1:100 leverage → margin = lots * contract_size * price / 100)
        margin_required = lots * CONTRACT_SIZE * entry_price / 100.0
        free_margin = equity  # assume all equity is free for simplicity

        # Dynamic max positions
        max_pos = _calc_max_positions(equity)

        # 1. DrawdownProtector
        can_trade, dd_reason = dp.check(equity=equity)
        if not can_trade:
            result.block_counts["drawdown_protection"] += 1
            continue

        # 2. CircuitBreaker
        cb.set_time(ts)
        can_trade, cb_reason = cb.can_open_trade(equity=equity)
        if not can_trade:
            result.block_counts["circuit_breaker"] += 1
            continue

        # 3. TradeBlocker (gap-filler)
        inp = BlockInput(
            open_positions=0,  # we enforce one-at-a-time above
            max_positions=max_pos,
            daily_trades_today=daily_trades,
            weekly_trades_this_week=weekly_trades,
            lots=lots,
            risk_pct=risk_pct,
            sl_distance_pct=sl_distance_pct,
            equity=equity,
            margin_required=margin_required,
            free_margin=free_margin,
            learning_mode=learning_mode,
        )
        verdict = tb.check(inp)
        if verdict.blocked:
            result.block_counts[verdict.block_name] += 1
            continue

        # All blocks passed — enter trade
        trade = BacktestTrade(
            entry_bar=sig_bar, direction=direction, entry_price=entry_price,
            stop_loss=sl_price, take_profit=tp_price, lots=lots,
            pullback_pct=pullback, entry_time=ts,
        )

        # Replay bar-by-bar
        end_bar = min(sig_bar + MAX_HOLD_BARS, N - 1)
        bars_slice = df.iloc[sig_bar + 1:end_bar + 1]
        pnl, reason, bars_held = replay_trade(trade, bars_slice, TRAIL_CFG)

        trade.exit_price = (bars_slice["close"].values[-1]
                            if len(bars_slice) > 0 else entry_price)
        trade.exit_reason = reason
        trade.pnl = pnl
        trade.bars_held = bars_held

        equity += pnl
        daily_trades += 1
        weekly_trades += 1

        if pnl < 0:
            cb.record_loss(pnl=-abs(pnl), equity=equity)
        else:
            cb.record_win(pnl=pnl)
        dp.record_pnl(pnl=pnl, equity=equity)

        result.trades.append(trade)
        # Trade actually exited at sig_bar + bars_held (not the max window end)
        open_until_bar = sig_bar + bars_held

    result.balance_end = equity
    return result


# ---------- Reporting ----------

def print_result(r: BacktestResult, label: str) -> None:
    print(f"\n  {label}: balance ${r.balance_start:,.0f} → ${r.balance_end:,.2f}")
    print(f"    trades={r.n_trades:>4}  W={r.wins:>3} L={r.losses:>3}  "
          f"WR={r.win_rate:>5.1f}%  PF={r.profit_factor:>5.2f}  "
          f"PnL={r.total_pnl:>+9.2f}  MaxDD=${r.max_dd:,.2f} ({r.max_dd_pct:>4.1f}%)")
    if r.block_counts:
        print(f"    blocks fired: {dict(r.block_counts)}")
    else:
        print(f"    blocks fired: none")
    # Exit reason breakdown
    if r.trades:
        reasons = Counter(t.exit_reason for t in r.trades)
        print(f"    exit reasons: {dict(reasons)}")


# ---------- Main ----------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--method", choices=["H2", "H3"], default="H2",
                   help="entry method: H2=pullback≥0.40%, H3=pullback≥0.50%")
    p.add_argument("--risk-pct", type=float, default=0.01,
                   help="risk per trade as decimal (0.01 = 1%)")
    p.add_argument("--balances", default="100,200,500,1000,10000",
                   help="comma-separated starting balances")
    p.add_argument("--learning-mode", action="store_true",
                   help="bypass daily/weekly trade count blocks")
    args = p.parse_args()

    balances = [float(x) for x in args.balances.split(",")]
    method_thr = 0.40 if args.method == "H2" else 0.50

    print("=" * 78)
    print(f"END-TO-END BACKTEST: entry + trailing TP + risk blocks")
    print(f"=" * 78)
    print(f"  Entry method: {args.method} (pullback ≥ {method_thr}%, trend-aligned HL/LH)")
    print(f"  Trailing TP:   D 0.20/0.10 (activation 0.20%, trail 0.10%)")
    print(f"  TP cap:        {TP_PCT}% (far — let trailing arm first)")
    print(f"  Max hold:      {MAX_HOLD_BARS} M5 bars ({MAX_HOLD_BARS*5/60:.0f}h)")
    print(f"  Risk/trade:    {args.risk_pct*100:.1f}%")
    print(f"  Blocks:        DrawdownProtector + CircuitBreaker + TradeBlocker")
    print(f"  Balances:      {balances}")
    print(f"  Learning mode: {args.learning_mode}")

    # Load premium M5 (already has indicators, but we need raw OHLC for swings)
    print(f"\nLoading premium M5 data...", flush=True)
    df = pd.read_parquet("data/processed/xauusd_m5_indicators.parquet")
    # Ensure we have needed columns
    print(f"  {len(df)} bars, range {df.index.min()} → {df.index.max()}")

    print(f"\nResampling M5 → H4/D1 + classifying trend...", flush=True)
    h4 = resample_timeframe(df, "H4")
    d1 = resample_timeframe(df, "D1")
    trend_at = classify_trend(d1, h4)
    print(f"  H4 bars={len(h4)}  D1 bars={len(d1)}")

    print(f"Finding swings (N=3)...", flush=True)
    sh, sl = find_swings(df, n=3)
    print(f"  swing highs={len(sh)}  swing lows={len(sl)}")

    print(f"Finding trend-aligned reversals (min_pullback={method_thr}%)...", flush=True)
    revs = find_trend_aligned_reversals(df, sh, sl, trend_at, w=48,
                                        min_pullback_pct=method_thr)
    buys = sum(1 for r in revs if r.direction == "BUY")
    sells = sum(1 for r in revs if r.direction == "SELL")
    print(f"  BUY reversals: {buys}  SELL reversals: {sells}  total: {len(revs)}")

    if not revs:
        print("⚠️ No reversals found. Exiting.")
        return

    # Build entry signals: (bar_idx+3, UP/DOWN)
    dir_map = {"BUY": "UP", "SELL": "DOWN"}
    signals = [(r.bar_idx + 3, dir_map[r.direction]) for r in revs
               if r.reversal_size_pct >= method_thr]
    print(f"  Entry signals: {len(signals)}")

    # Run backtest per balance
    print(f"\n{'='*78}")
    print(f"BACKTEST RESULTS (per balance)")
    print(f"{'='*78}")
    results = []
    for bal in balances:
        r = run_backtest(signals, df, revs, bal, risk_pct=args.risk_pct,
                          method=args.method, learning_mode=args.learning_mode)
        print_result(r, f"${bal:,.0f}")
        results.append(r)

    # Summary table
    print(f"\n{'='*78}")
    print(f"SUMMARY TABLE")
    print(f"{'='*78}")
    print(f"  {'balance':>10}  {'end':>10}  {'trades':>6}  {'WR':>6}  {'PF':>6}  "
          f"{'PnL':>9}  {'MaxDD%':>7}  {'blocks':>6}")
    for r in results:
        total_blocks = sum(r.block_counts.values())
        print(f"  ${r.balance_start:>9,.0f}  ${r.balance_end:>9,.2f}  "
              f"{r.n_trades:>6}  {r.win_rate:>5.1f}%  {r.profit_factor:>5.2f}  "
              f"{r.total_pnl:>+9.2f}  {r.max_dd_pct:>6.1f}%  {total_blocks:>6}")

    # Block breakdown across all balances
    print(f"\nBLOCK FIRED TOTALS (across all balances):")
    all_blocks = Counter()
    for r in results:
        all_blocks.update(r.block_counts)
    if all_blocks:
        for name, cnt in all_blocks.most_common():
            print(f"  {name:<25} {cnt}")
    else:
        print("  none fired")


if __name__ == "__main__":
    main()