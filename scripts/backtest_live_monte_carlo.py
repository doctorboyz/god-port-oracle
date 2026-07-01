#!/usr/bin/env python3
"""Realistic live-environment backtest + Monte Carlo robustness.

Live environment (Exness Standard account, XAUUSD):
  - Spread: ~$0.20 typical (variable $0.15-0.30) — applied at entry & exit
  - Slippage: 0-1 point ($0.01-0.10) random
  - Leverage 1:100 → margin = lots × contract × price / 100
  - Min lot 0.01, lot step 0.01
  - No commission (standard account = spread only)
  - Swap: small overnight fee (ignored for <24h holds)

Monte Carlo adds diversity by:
  1. Randomizing spread per trade (uniform $0.15-0.30) — tests spread sensitivity
  2. Randomizing slippage per trade (0-1 point)
  3. Bootstrapping trade order (shuffle PnL sequence) — tests "what if losses
     clustered differently?" → distribution of MaxDD

Reports per balance: baseline (no MC) vs MC median/p10/p90 for
final balance, MaxDD, PF, WR. Plus probability of ruin (balance < 50% start).

Usage:
  python3 scripts/backtest_live_monte_carlo.py
  python3 scripts/backtest_live_monte_carlo.py --mc-runs 500 --balances 100,1000
"""
from __future__ import annotations

import argparse
import random
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
    classify_trend, find_swings, find_trend_aligned_reversals, resample_timeframe,
)
from scripts.trailing_replay import TrailingConfig, pnl_dollars


# ---------- Live environment constants ----------

CONTRACT_SIZE = 100      # XAUUSD: 1 lot = 100 oz
LEVERAGE = 100           # Exness Standard 1:100
LOT_STEP = 0.01
MIN_LOT = 0.01

SPREAD_TYPICAL_USD = 0.20    # $0.20 typical XAUUSD spread on Standard
SPREAD_MIN_USD = 0.15
SPREAD_MAX_USD = 0.30
SLIPPAGE_MAX_USD = 0.05      # 0-5 cents slippage

TRAIL_CFG = TrailingConfig(name="D 0.20/0.10", kind="simple",
                            activation_pct=0.20, trail_pct=0.10)
TP_PCT = 2.0
MAX_HOLD_BARS = 288

RUIN_THRESHOLD = 0.50   # balance < 50% of start = ruin


# ---------- Live replay (spread + slippage) ----------

def replay_trade_live(entry_price: float, sl_price: float, tp_price: float,
                       direction: str, lots: float, bars: pd.DataFrame,
                       cfg: TrailingConfig,
                       spread_usd: float, slippage_usd: float,
                       ) -> tuple[float, str, int]:
    """Bar-by-bar replay with live spread + slippage.

    Spread cost: paid at entry AND exit (round-trip ~2× spread/2 = spread).
    Slippage: price moves against you by slippage_usd on entry.
    """
    if len(bars) == 0:
        return 0.0, "no_bars", 0

    is_buy = direction == "BUY"
    # Entry price adjusted: BUY enters at ask (close + spread/2 + slippage)
    #                    SELL enters at bid (close - spread/2 - slippage)
    if is_buy:
        entry = entry_price + spread_usd / 2 + slippage_usd
    else:
        entry = entry_price - spread_usd / 2 - slippage_usd
    # SL/TP relative to adjusted entry
    sl_d = (entry_price - sl_price) if is_buy else (sl_price - entry_price)
    if is_buy:
        sl_p = entry - sl_d
        tp_p = entry + (tp_price - entry_price)
    else:
        sl_p = entry + sl_d
        tp_p = entry - (entry_price - tp_price)

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
                    # Exit at bid (trail_level - spread/2 - slippage)
                    exit_p = trail_level - spread_usd / 2 - slippage_usd
                    pnl = pnl_dollars((exit_p - entry) / entry * 100, entry, lots)
                    return pnl, "trailing_tp", i + 1
            else:
                if bar_low <= sl_p:
                    exit_p = sl_p - spread_usd / 2 - slippage_usd
                    pnl = pnl_dollars((exit_p - entry) / entry * 100, entry, lots)
                    return pnl, "stop_loss", i + 1
            if bar_high >= tp_p:
                exit_p = tp_p - spread_usd / 2 - slippage_usd
                pnl = pnl_dollars((exit_p - entry) / entry * 100, entry, lots)
                return pnl, "take_profit", i + 1
            if bar_high > peak:
                peak = bar_high
            gain_pct = (peak - entry) / entry * 100
            if not armed and gain_pct >= cfg.activation_pct:
                armed = True
        else:
            if armed:
                trail_level = peak * (1 + trail_pct / 100.0)
                if bar_high >= trail_level:
                    exit_p = trail_level + spread_usd / 2 + slippage_usd
                    pnl = pnl_dollars((entry - exit_p) / entry * 100, entry, lots)
                    return pnl, "trailing_tp", i + 1
            else:
                if bar_high >= sl_p:
                    exit_p = sl_p + spread_usd / 2 + slippage_usd
                    pnl = pnl_dollars((entry - exit_p) / entry * 100, entry, lots)
                    return pnl, "stop_loss", i + 1
            if bar_low <= tp_p:
                exit_p = tp_p + spread_usd / 2 + slippage_usd
                pnl = pnl_dollars((entry - exit_p) / entry * 100, entry, lots)
                return pnl, "take_profit", i + 1
            if bar_low < peak:
                peak = bar_low
            gain_pct = (entry - peak) / entry * 100
            if not armed and gain_pct >= cfg.activation_pct:
                armed = True

    # max holding — exit at last close (with spread cost)
    last_close = bars["close"].values[-1]
    if is_buy:
        exit_p = last_close - spread_usd / 2 - slippage_usd
        pnl = pnl_dollars((exit_p - entry) / entry * 100, entry, lots)
    else:
        exit_p = last_close + spread_usd / 2 + slippage_usd
        pnl = pnl_dollars((entry - exit_p) / entry * 100, entry, lots)
    return pnl, "max_holding", n


# ---------- Backtest with live environment ----------

@dataclass
class LiveResult:
    balance_start: float
    balance_end: float
    pnl_list: list[float] = field(default_factory=list)
    block_counts: Counter = field(default_factory=Counter)
    exit_reasons: Counter = field(default_factory=Counter)
    peak_equity: float = 0.0
    max_dd: float = 0.0

    @property
    def n_trades(self) -> int:
        return len(self.pnl_list)

    @property
    def wins(self) -> int:
        return sum(1 for p in self.pnl_list if p > 0)

    @property
    def losses(self) -> int:
        return sum(1 for p in self.pnl_list if p < 0)

    @property
    def win_rate(self) -> float:
        d = self.wins + self.losses
        return self.wins / d * 100 if d else 0.0

    @property
    def gross_profit(self) -> float:
        return sum(p for p in self.pnl_list if p > 0)

    @property
    def gross_loss(self) -> float:
        return -sum(p for p in self.pnl_list if p < 0)

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / self.gross_loss if self.gross_loss > 0 else float('inf')

    @property
    def total_pnl(self) -> float:
        return sum(self.pnl_list)

    @property
    def max_dd_pct(self) -> float:
        return self.max_dd / self.balance_start * 100 if self.balance_start > 0 else 0.0


def _calc_max_positions(equity: float, cap: int = 5, per_pos: float = 200) -> int:
    if equity <= 0:
        return 1
    return max(1, min(cap, int(equity // per_pos)))


def run_live_backtest(signals: list[tuple[int, str]], df: pd.DataFrame,
                       reversals: list, balance_start: float,
                       risk_pct: float = 0.01, method: str = "H2",
                       spread_usd: float = SPREAD_TYPICAL_USD,
                       slippage_usd: float = 0.0,
                       rng: Optional[random.Random] = None,
                       ) -> LiveResult:
    """Run backtest with live environment (spread + slippage)."""
    dir_map = {"BUY": "UP", "SELL": "DOWN"}
    pullback_map = {(r.bar_idx + 3, dir_map[r.direction]): r.reversal_size_pct
                    for r in reversals}
    signals_sorted = sorted(signals, key=lambda x: x[0])

    equity = balance_start
    initial_equity = balance_start
    peak_equity = balance_start
    max_dd = 0.0

    dp = DrawdownProtector(initial_equity=initial_equity,
                           daily_limit_pct=0.20, weekly_limit_pct=0.30,
                           account_limit_pct=0.30)
    cb = CircuitBreaker(daily_loss_limit_pct=0.05, consecutive_loss_limit=5,
                         cooldown_minutes=15)
    tb = TradeBlocker(daily_trade_count_limit=20, weekly_trade_count_limit=80,
                       hard_max_lots=0.50, max_risk_pct=0.05,
                       min_sl_distance_pct=0.05, max_sl_distance_pct=5.0,
                       margin_safety_factor=0.8)

    open_until_bar = -1
    current_day = None
    daily_trades = 0
    current_week = None
    weekly_trades = 0

    result = LiveResult(balance_start=balance_start, balance_end=balance_start)
    close = df["close"].values
    N = len(df)

    dp.check(equity=initial_equity)

    for sig_bar, sig_dir in signals_sorted:
        if equity > peak_equity:
            peak_equity = equity
        dd = peak_equity - equity
        if dd > max_dd:
            max_dd = dd
        result.peak_equity = peak_equity
        result.max_dd = max_dd

        if sig_bar < open_until_bar:
            continue

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

        # Set simulated time so DP cooldown/rollover works in backtest
        dp.set_time(ts)

        direction = "BUY" if sig_dir == "UP" else "SELL"
        pullback = pullback_map.get((sig_bar, sig_dir), 0.0)
        method_thr = 0.40 if method == "H2" else 0.50
        sl_distance_pct = max(pullback, method_thr) if pullback > 0 else method_thr

        entry_price = close[sig_bar]
        if direction == "BUY":
            sl_price = entry_price * (1 - sl_distance_pct / 100.0)
            tp_price = entry_price * (1 + TP_PCT / 100.0)
        else:
            sl_price = entry_price * (1 + sl_distance_pct / 100.0)
            tp_price = entry_price * (1 - TP_PCT / 100.0)

        # Live spread/slippage for this trade (MC randomizes)
        s_usd = spread_usd
        slip_usd = slippage_usd
        if rng is not None:
            s_usd = rng.uniform(SPREAD_MIN_USD, SPREAD_MAX_USD)
            slip_usd = rng.uniform(0, SLIPPAGE_MAX_USD)

        # Lot sizing — entry price adjusted for spread (buyer pays ask)
        entry_for_sizing = entry_price + s_usd / 2 if direction == "BUY" else entry_price - s_usd / 2
        lots = risk_per_trade_size(equity, risk_pct, entry_for_sizing, sl_price,
                                    CONTRACT_SIZE, min_lots=MIN_LOT, max_lots=10.0)
        # Round to lot step
        lots = max(MIN_LOT, round(lots / LOT_STEP) * LOT_STEP)

        margin_required = lots * CONTRACT_SIZE * entry_price / LEVERAGE
        free_margin = equity
        max_pos = _calc_max_positions(equity)

        # Blocks
        can_trade, _ = dp.check(equity=equity)
        if not can_trade:
            result.block_counts["drawdown_protection"] += 1
            continue
        cb.set_time(ts)
        can_trade, _ = cb.can_open_trade(equity=equity)
        if not can_trade:
            result.block_counts["circuit_breaker"] += 1
            continue
        verdict = tb.check(BlockInput(
            open_positions=0, max_positions=max_pos,
            daily_trades_today=daily_trades, weekly_trades_this_week=weekly_trades,
            lots=lots, risk_pct=risk_pct, sl_distance_pct=sl_distance_pct,
            equity=equity, margin_required=margin_required, free_margin=free_margin,
        ))
        if verdict.blocked:
            result.block_counts[verdict.block_name] += 1
            continue

        # Replay with live spread/slippage
        end_bar = min(sig_bar + MAX_HOLD_BARS, N - 1)
        bars_slice = df.iloc[sig_bar + 1:end_bar + 1]
        pnl, reason, bars_held = replay_trade_live(
            entry_price, sl_price, tp_price, direction, lots,
            bars_slice, TRAIL_CFG, s_usd, slip_usd,
        )

        equity += pnl
        daily_trades += 1
        weekly_trades += 1
        if pnl < 0:
            cb.record_loss(pnl=-abs(pnl), equity=equity)
        else:
            cb.record_win(pnl=pnl)
        dp.record_pnl(pnl=pnl, equity=equity)

        result.pnl_list.append(pnl)
        result.exit_reasons[reason] += 1
        open_until_bar = sig_bar + bars_held

    result.balance_end = equity
    return result


# ---------- Monte Carlo ----------

@dataclass
class MCResult:
    n_runs: int
    final_balances: list[float] = field(default_factory=list)
    max_dds: list[float] = field(default_factory=list)
    pnl_totals: list[float] = field(default_factory=list)
    wins: list[int] = field(default_factory=list)
    n_trades: list[int] = field(default_factory=list)
    n_ruin: int = 0

    def percentile(self, lst: list, p: float) -> float:
        if not lst:
            return 0.0
        s = sorted(lst)
        k = int(p / 100 * (len(s) - 1))
        return s[k]

    @property
    def ruin_prob(self) -> float:
        return self.n_ruin / self.n_runs * 100 if self.n_runs else 0.0


def monte_carlo_bootstrap(pnl_list: list[float], balance_start: float,
                           n_runs: int, rng: random.Random,
                           ruin_threshold: float = RUIN_THRESHOLD) -> MCResult:
    """Bootstrap: shuffle PnL order, recompute equity path + MaxDD.

    Tests 'what if losses clustered differently?' — gives distribution of
    MaxDD and final balance for the same set of trades.
    """
    res = MCResult(n_runs=n_runs)
    if not pnl_list:
        return res
    ruin_limit = balance_start * ruin_threshold

    for _ in range(n_runs):
        # Shuffle PnL order
        shuffled = pnl_list.copy()
        rng.shuffle(shuffled)
        equity = balance_start
        peak = balance_start
        max_dd = 0.0
        n_wins = 0
        ruined = False
        for p in shuffled:
            equity += p
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
            if p > 0:
                n_wins += 1
            if equity < ruin_limit:
                ruined = True
                break
        res.final_balances.append(equity)
        res.max_dds.append(max_dd)
        res.pnl_totals.append(equity - balance_start)
        res.wins.append(n_wins)
        res.n_trades.append(len(shuffled))
        if ruined:
            res.n_ruin += 1
    return res


def monte_carlo_full(signals, df, reversals, balance_start: float,
                      n_runs: int, base_rng_seed: int,
                      risk_pct: float, method: str) -> tuple[MCResult, LiveResult]:
    """Full MC: re-run backtest with randomized spread/slippage per trade.

    Each run re-runs the entire backtest with different spread/slippage draws.
    More expensive than bootstrap but captures spread sensitivity on PnL.
    """
    res = MCResult(n_runs=n_runs)
    # Baseline (deterministic, typical spread)
    baseline = run_live_backtest(signals, df, reversals, balance_start,
                                   risk_pct=risk_pct, method=method,
                                   spread_usd=SPREAD_TYPICAL_USD, slippage_usd=0.0)
    ruin_limit = balance_start * RUIN_THRESHOLD

    for run_i in range(n_runs):
        rng = random.Random(base_rng_seed + run_i * 7919)
        r = run_live_backtest(signals, df, reversals, balance_start,
                               risk_pct=risk_pct, method=method,
                               spread_usd=SPREAD_TYPICAL_USD, slippage_usd=0.0,
                               rng=rng)
        res.final_balances.append(r.balance_end)
        res.max_dds.append(r.max_dd)
        res.pnl_totals.append(r.total_pnl)
        res.wins.append(r.wins)
        res.n_trades.append(r.n_trades)
        if r.balance_end < ruin_limit:
            res.n_ruin += 1
    return res, baseline


# ---------- Reporting ----------

def print_live_result(r: LiveResult, label: str) -> None:
    print(f"\n  {label}: ${r.balance_start:,.0f} → ${r.balance_end:,.2f}")
    print(f"    trades={r.n_trades:>4}  W={r.wins:>3} L={r.losses:>3}  "
          f"WR={r.win_rate:>5.1f}%  PF={r.profit_factor:>5.2f}  "
          f"PnL={r.total_pnl:>+9.2f}  MaxDD=${r.max_dd:,.2f} ({r.max_dd_pct:>5.1f}%)")
    if r.block_counts:
        print(f"    blocks: {dict(r.block_counts)}")
    if r.exit_reasons:
        print(f"    exits: {dict(r.exit_reasons)}")


def print_mc_result(mc: MCResult, label: str, balance_start: float) -> None:
    fb = mc.final_balances
    dd = mc.max_dds
    pnls = mc.pnl_totals
    print(f"\n  {label}: MC over {mc.n_runs} runs")
    print(f"    final balance:  p10=${mc.percentile(fb, 10):>9,.2f}  "
          f"p50=${mc.percentile(fb, 50):>9,.2f}  p90=${mc.percentile(fb, 90):>9,.2f}")
    print(f"    MaxDD:          p10=${mc.percentile(dd, 10):>9,.2f}  "
          f"p50=${mc.percentile(dd, 50):>9,.2f}  p90=${mc.percentile(dd, 90):>9,.2f}")
    print(f"    total PnL:      p10=${mc.percentile(pnls, 10):>+9,.2f}  "
          f"p50=${mc.percentile(pnls, 50):>+9,.2f}  p90=${mc.percentile(pnls, 90):>+9,.2f}")
    print(f"    prob of ruin (balance < {RUIN_THRESHOLD*100:.0f}% of start): {mc.ruin_prob:>5.1f}%")


# ---------- Main ----------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--method", choices=["H2", "H3"], default="H2")
    p.add_argument("--risk-pct", type=float, default=0.01)
    p.add_argument("--balances", default="100,200,500,1000,10000")
    p.add_argument("--mc-runs", type=int, default=200,
                   help="MC runs per balance (full + bootstrap)")
    p.add_argument("--mc-mode", choices=["bootstrap", "full", "both"], default="both",
                   help="bootstrap=shuffle PnL order (fast); full=re-run with random spread (slow)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--data", choices=["premium", "exness"], default="premium",
                   help="dataset: premium (200k bars 2023-2026) or exness (13k bars Feb-Apr 2026)")
    p.add_argument("--exness-csv", default="/Users/doctorboyz/Documents/xau-data/xauusd_5m.csv",
                   help="Exness M5 CSV path")
    args = p.parse_args()

    balances = [float(x) for x in args.balances.split(",")]
    method_thr = 0.40 if args.method == "H2" else 0.50

    print("=" * 78)
    print(f"LIVE-ENVIRONMENT BACKTEST + MONTE CARLO")
    print(f"=" * 78)
    print(f"  Account:      Exness Standard (XAUUSD)")
    print(f"  Dataset:      {args.data}")
    print(f"  Spread:       ${SPREAD_TYPICAL_USD:.2f} typical (${SPREAD_MIN_USD}-${SPREAD_MAX_USD} MC range)")
    print(f"  Slippage:     0-${SLIPPAGE_MAX_USD:.2f} (MC random)")
    print(f"  Leverage:     1:{LEVERAGE}")
    print(f"  Min lot:      {MIN_LOT}  step: {LOT_STEP}")
    print(f"  Entry:        {args.method} (pullback ≥ {method_thr}%)")
    print(f"  Trailing:     D 0.20/0.10")
    print(f"  Risk/trade:   {args.risk_pct*100:.1f}%")
    print(f"  MC runs:      {args.mc_runs}  mode: {args.mc_mode}")
    print(f"  Ruin:         balance < {RUIN_THRESHOLD*100:.0f}% of start")

    print(f"\nLoading {args.data} M5...", flush=True)
    if args.data == "premium":
        df = pd.read_parquet("data/processed/xauusd_m5_indicators.parquet")
    else:
        raw = pd.read_csv(args.exness_csv)
        # Normalize columns: date,Open,High,Low,Close,Volume,session → lowercase
        raw = raw.rename(columns={c: c.lower() for c in raw.columns})
        raw["timestamp"] = pd.to_datetime(raw["date"])
        df = raw.set_index("timestamp").sort_index()
        # Ensure required columns
        for col in ("open", "high", "low", "close", "volume"):
            if col not in df.columns:
                df[col] = 0.0
    print(f"  {len(df)} bars, {df.index.min()} → {df.index.max()}")

    print(f"Resampling + trend + swings + reversals...", flush=True)
    h4 = resample_timeframe(df, "H4")
    d1 = resample_timeframe(df, "D1")
    trend_at = classify_trend(d1, h4)
    sh, sl = find_swings(df, n=3)
    revs = find_trend_aligned_reversals(df, sh, sl, trend_at, w=48,
                                        min_pullback_pct=method_thr)
    dir_map = {"BUY": "UP", "SELL": "DOWN"}
    signals = [(r.bar_idx + 3, dir_map[r.direction]) for r in revs
               if r.reversal_size_pct >= method_thr]
    print(f"  reversals: {len(revs)}  signals: {len(signals)}")

    if not signals:
        print("⚠️ No signals. Exiting.")
        return

    # Baseline (deterministic, typical spread, no slippage)
    print(f"\n{'='*78}")
    print(f"BASELINE (typical spread ${SPREAD_TYPICAL_USD:.2f}, no slippage)")
    print(f"{'='*78}")
    baseline_results: dict[float, LiveResult] = {}
    for bal in balances:
        r = run_live_backtest(signals, df, revs, bal, risk_pct=args.risk_pct,
                               method=args.method, spread_usd=SPREAD_TYPICAL_USD,
                               slippage_usd=0.0)
        print_live_result(r, f"${bal:,.0f}")
        baseline_results[bal] = r

    # MC
    if args.mc_mode in ("bootstrap", "both"):
        print(f"\n{'='*78}")
        print(f"MC BOOTSTRAP (shuffle trade order, {args.mc_runs} runs/balance)")
        print(f"{'='*78}")
        rng = random.Random(args.seed)
        for bal in balances:
            base = baseline_results[bal]
            mc = monte_carlo_bootstrap(base.pnl_list, bal, args.mc_runs, rng)
            print_mc_result(mc, f"${bal:,.0f}  bootstrap", bal)

    if args.mc_mode in ("full", "both"):
        print(f"\n{'='*78}")
        print(f"MC FULL (random spread+slippage per trade, {args.mc_runs} runs/balance)")
        print(f"{'='*78}")
        for bal in balances:
            mc, _ = monte_carlo_full(signals, df, revs, bal, args.mc_runs,
                                       args.seed, args.risk_pct, args.method)
            print_mc_result(mc, f"${bal:,.0f}  full", bal)

    # Summary
    print(f"\n{'='*78}")
    print(f"SUMMARY (baseline vs MC-bootstrap p50/p90)")
    print(f"{'='*78}")
    print(f"  {'balance':>9}  {'base end':>10}  {'base DD%':>8}  "
          f"{'MC p50':>10}  {'MC p90':>10}  {'MC DD p90':>10}  {'ruin%':>6}")
    rng = random.Random(args.seed)
    for bal in balances:
        base = baseline_results[bal]
        mc = monte_carlo_bootstrap(base.pnl_list, bal, args.mc_runs, rng)
        print(f"  ${bal:>8,.0f}  ${base.balance_end:>9,.2f}  {base.max_dd_pct:>7.1f}%  "
              f"${mc.percentile(mc.final_balances, 50):>9,.2f}  "
              f"${mc.percentile(mc.final_balances, 90):>9,.2f}  "
              f"${mc.percentile(mc.max_dds, 90):>9,.2f}  "
              f"{mc.ruin_prob:>5.1f}%")


if __name__ == "__main__":
    main()