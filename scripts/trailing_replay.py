#!/usr/bin/env python3
"""Bar-by-bar trailing stop replay — precise evaluation of trailing algorithms.

Replaces MFE/MAE approximation. For each trade, walks M5 bars from entry to
exit in chronological order, checking SL/TP/trailing triggers bar by bar.

Conservative intra-bar convention (adverse-first):
  Within a bar, assume price moved against position before moving in favor.
  This UNDERESTIMATES trailing performance — if trailing still wins, it's robust.

Trailing algorithms:
  D_SIMPLE : activation % / trail % (current simple version, baseline)
  ATR      : trail_distance = k * ATR_at_entry (volatility-adaptive)
  BREAKEVEN: move SL to entry when +X% gain, then trail from entry
  MULTI    : multi-stage — arm at L1, tighten trail at L2, tighten more at L3
  REGIME   : trending -> wider trail (give trend room), ranging/volatile -> tighter

Usage:
  python3 scripts/trailing_replay.py --db /tmp/oracle_engine.db \
    --bars /tmp/exness_m5_bars.csv --since 2026-05-04
  python3 scripts/trailing_replay.py --premium  # use premium M5 CSV
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


CONTRACT_SIZE = 100.0  # XAUUSD 1 lot = 100 oz


# ---------- Trailing algorithm definitions ----------

@dataclass
class TrailingConfig:
    name: str
    kind: str = "none"            # none | simple | atr | breakeven | multi | regime
    activation_pct: float = 0.30  # arm when price moves this % in favor
    trail_pct: float = 0.15       # lock profit this % below peak
    atr_mult: float = 1.0         # for ATR: trail = atr_mult * ATR / entry * 100 (in %)
    breakeven_arm_pct: float = 0.20  # move SL to entry at +0.20%
    levels: tuple = ()             # for multi: ((arm_pct, trail_pct), ...)


# ---------- Trade + result ----------

@dataclass
class Trade:
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


@dataclass
class VariantResult:
    name: str
    n: int = 0
    wins: int = 0
    losses: int = 0
    zero: int = 0
    total_pnl: float = 0.0
    pnl_list: list = field(default_factory=list)
    tp_hits: int = 0
    sl_hits: int = 0
    trail_exits: int = 0
    be_exits: int = 0
    max_holding: int = 0
    no_bars: int = 0  # trades with no M5 bars covering them

    def report(self) -> str:
        wr = 100 * self.wins / max(1, self.wins + self.losses + self.zero)
        avg = self.total_pnl / max(1, self.n)
        gross_profit = sum(p for p in self.pnl_list if p > 0)
        gross_loss = -sum(p for p in self.pnl_list if p < 0)
        pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        max_dd = 0.0; peak = 0.0; cum = 0.0
        for p in self.pnl_list:
            cum += p
            if cum > peak: peak = cum
            dd = peak - cum
            if dd > max_dd: max_dd = dd
        return (f"  {self.name:<28} n={self.n:>4} W={self.wins:>3} L={self.losses:>3} "
                f"0={self.zero:>3} WR={wr:>5.1f}% PnL={self.total_pnl:>+9.2f} "
                f"avg={avg:>+6.2f} PF={pf:>4.2f} MaxDD={max_dd:>7.2f} "
                f"TP={self.tp_hits} SL={self.sl_hits} trail={self.trail_exits} "
                f"BE={self.be_exits} mh={self.max_holding} noBars={self.no_bars}")


# ---------- Bar-by-bar replay engine ----------

def pnl_dollars(pnl_pct: float, entry_price: float, lot_size: float) -> float:
    return pnl_pct * entry_price * lot_size * CONTRACT_SIZE / 100.0


def sl_tp_dist_pct(t: Trade) -> tuple[float, float]:
    """Return (sl_dist_pct, tp_dist_pct) — both positive."""
    if t.direction == "BUY":
        sl = (t.entry_price - t.stop_loss) / t.entry_price * 100
        tp = (t.take_profit - t.entry_price) / t.entry_price * 100
    else:
        sl = (t.stop_loss - t.entry_price) / t.entry_price * 100
        tp = (t.entry_price - t.take_profit) / t.entry_price * 100
    return sl, tp


def replay_trade(t: Trade, bars: pd.DataFrame, cfg: TrailingConfig) -> tuple[float, str]:
    """Bar-by-bar replay one trade. Returns (pnl_dollars, exit_reason).

    bars: DataFrame indexed by timestamp with columns open/high/low/close,
          filtered to start >= t.entry_time and end <= t.exit_time (or to end of data).
    Conservative intra-bar: adverse-first (SL checked before TP/trailing update).

    kind="let_run": pure SL/TP replay, no trailing, no manual close.
                   This is the TRUE baseline for trailing comparison (what happens
                   if we don't manually close or trail — let SL/TP decide).
    kind="actual":  use original_pnl from DB (includes manual close).
    """
    if len(bars) == 0:
        return t.original_pnl, "no_bars"

    if cfg.kind == "actual":
        return t.original_pnl, t.original_exit_reason or "max_holding"

    sl_pct, tp_pct = sl_tp_dist_pct(t)
    entry = t.entry_price
    is_buy = t.direction == "BUY"
    sl_price = t.stop_loss
    tp_price = t.take_profit

    # Trailing state
    peak = entry  # best price reached in favor
    armed = False
    be_armed = False  # break-even armed
    trail_pct = cfg.trail_pct

    # ATR-based trail distance (in price % terms relative to entry)
    if cfg.kind == "atr" and t.atr_at_entry > 0:
        trail_pct = (cfg.atr_mult * t.atr_at_entry) / entry * 100

    for i in range(len(bars)):
        bar_high = bars.iloc[i]["high"]
        bar_low = bars.iloc[i]["low"]

        if is_buy:
            # 1. Adverse first (conservative): SL or trailing-trigger using pre-bar peak
            if armed:
                trail_level = peak * (1 - trail_pct / 100.0)
                if bar_low <= trail_level:
                    return pnl_dollars((trail_level - entry) / entry * 100, entry, t.lot_size), "trailing_tp"
            elif cfg.kind != "let_run":
                if bar_low <= sl_price:
                    return -pnl_dollars(sl_pct, entry, t.lot_size), "stop_loss"
            else:
                if bar_low <= sl_price:
                    return -pnl_dollars(sl_pct, entry, t.lot_size), "stop_loss"
            # 2. Break-even: if armed and bar_low <= entry (now SL is at entry), exit at entry
            if cfg.kind == "breakeven" and be_armed and bar_low <= entry:
                return 0.0, "breakeven"
            # 3. Favorable: TP
            if bar_high >= tp_price:
                return +pnl_dollars(tp_pct, entry, t.lot_size), "take_profit"
            # 4. Update peak, arm trailing
            if bar_high > peak:
                peak = bar_high
            gain_pct = (peak - entry) / entry * 100
            # Break-even arming
            if cfg.kind == "breakeven" and not be_armed and gain_pct >= cfg.breakeven_arm_pct:
                be_armed = True
            # Multi-stage: pick trail_pct based on gain
            if cfg.kind == "multi" and cfg.levels:
                for arm_pct, lvl_trail in cfg.levels:
                    if gain_pct >= arm_pct:
                        trail_pct = lvl_trail
                        armed = True
            # Simple arm
            if cfg.kind in ("simple", "atr") and not armed and gain_pct >= cfg.activation_pct:
                armed = True
            if cfg.kind == "breakeven" and be_armed and not armed:
                # After BE armed, also arm trailing
                if gain_pct >= cfg.activation_pct:
                    armed = True
            if cfg.kind == "regime":
                # Regime-aware arming + trail distance
                if t.regime == "trending":
                    act, tr = cfg.activation_pct, cfg.trail_pct * 1.5  # wider trail
                elif t.regime == "volatile":
                    act, tr = cfg.activation_pct * 0.8, cfg.trail_pct * 0.8  # tighter
                else:  # ranging or unknown
                    act, tr = cfg.activation_pct * 0.7, cfg.trail_pct * 0.7
                trail_pct = tr
                if gain_pct >= act:
                    armed = True
        else:  # SELL
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
            if bar_low < peak:
                peak = bar_low
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
                if gain_pct >= act:
                    armed = True

    # Reached end of holding window without SL/TP/trailing — max_holding
    # For let_run: use last bar's close as exit (what we'd get if we close at end of window)
    if cfg.kind == "let_run":
        last_close = bars.iloc[-1]["close"]
        return pnl_dollars((last_close - entry) / entry * 100 * (1 if is_buy else -1),
                           entry, t.lot_size), "max_holding"
    return t.original_pnl, "max_holding"


# ---------- Data loading ----------

def load_bars(csv_path: str) -> pd.DataFrame:
    """Load M5 CSV, set timestamp index, sort. Strips tz to match DB timestamps."""
    df = pd.read_csv(csv_path)
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
    df = df.rename(columns={"time": "timestamp"})
    df = df.sort_values("timestamp").set_index("timestamp")
    return df


def load_trades(db_path: str, since: str = "2026-05-04", account_id: int = 1) -> list[Trade]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT id, timestamp, direction, entry_price, stop_loss, take_profit,
               lot_size, atr_at_entry, regime, exit_time, pnl, exit_reason
        FROM live_trades
        WHERE account_id = ? AND timestamp >= ?
          AND entry_price > 0 AND stop_loss > 0 AND take_profit > 0
          AND lot_size > 0
        ORDER BY timestamp
    """, (account_id, since))
    trades = []
    for r in cur.fetchall():
        try:
            entry_time = pd.Timestamp(r["timestamp"])
            if entry_time.tzinfo is not None:
                entry_time = entry_time.tz_localize(None)
            exit_time = pd.Timestamp(r["exit_time"]) if r["exit_time"] else None
            if exit_time is not None and exit_time.tzinfo is not None:
                exit_time = exit_time.tz_localize(None)
        except Exception:
            continue
        trades.append(Trade(
            id=r["id"], direction=r["direction"],
            entry_price=r["entry_price"], stop_loss=r["stop_loss"],
            take_profit=r["take_profit"], lot_size=r["lot_size"],
            atr_at_entry=r["atr_at_entry"] or 0,
            regime=r["regime"] or "unknown",
            entry_time=entry_time, exit_time=exit_time,
            original_pnl=r["pnl"] or 0,
            original_exit_reason=r["exit_reason"] or "",
        ))
    conn.close()
    return trades


def get_trade_bars(bars: pd.DataFrame, t: Trade, max_bars: int = 240) -> pd.DataFrame:
    """Get M5 bars covering entry → exit (or +max_bars bars if still open).

    Bars start strictly AFTER entry bar (entry bar is the signal bar —
    position opens at next bar's open, but we approximate using entry_price).
    Actually include the entry bar since entry happens intra-bar.
    """
    if t.exit_time is not None:
        end = t.exit_time
    else:
        end = t.entry_time + pd.Timedelta(minutes=5 * max_bars)
    # Include the entry bar itself
    mask = (bars.index >= t.entry_time - pd.Timedelta(minutes=5)) & (bars.index <= end + pd.Timedelta(minutes=5))
    sub = bars[mask]
    if len(sub) > max_bars:
        sub = sub.iloc[:max_bars]
    return sub


# ---------- Variants ----------

VARIANTS = [
    TrailingConfig(name="Actual (with manual close)", kind="actual"),
    TrailingConfig(name="Let-run (no trail, no manual)", kind="let_run"),
    TrailingConfig(name="D-simple 0.30/0.15", kind="simple", activation_pct=0.30, trail_pct=0.15),
    TrailingConfig(name="D-simple 0.20/0.10", kind="simple", activation_pct=0.20, trail_pct=0.10),
    TrailingConfig(name="D-simple 0.40/0.20", kind="simple", activation_pct=0.40, trail_pct=0.20),
    TrailingConfig(name="ATR trail k=1.0 act=0.30", kind="atr", activation_pct=0.30, atr_mult=1.0),
    TrailingConfig(name="ATR trail k=1.5 act=0.30", kind="atr", activation_pct=0.30, atr_mult=1.5),
    TrailingConfig(name="ATR trail k=0.5 act=0.20", kind="atr", activation_pct=0.20, atr_mult=0.5),
    TrailingConfig(name="Breakeven 0.20 + trail 0.15", kind="breakeven",
                   breakeven_arm_pct=0.20, activation_pct=0.30, trail_pct=0.15),
    TrailingConfig(name="Multi-stage (0.2/0.2, 0.4/0.15, 0.6/0.1)", kind="multi",
                   levels=((0.2, 0.20), (0.4, 0.15), (0.6, 0.10))),
    TrailingConfig(name="Regime-aware (trend wide)", kind="regime",
                   activation_pct=0.30, trail_pct=0.15),
]


def run_variant(trades: list[Trade], bars: pd.DataFrame, cfg: TrailingConfig) -> VariantResult:
    res = VariantResult(name=cfg.name)
    for t in trades:
        sub = get_trade_bars(bars, t)
        if cfg.kind == "none":
            pnl, reason = t.original_pnl, t.original_exit_reason or "max_holding"
        else:
            pnl, reason = replay_trade(t, sub, cfg)
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
    parser.add_argument("--db", default="/tmp/oracle_engine.db")
    parser.add_argument("--bars", default="/tmp/exness_m5_bars.csv")
    parser.add_argument("--since", default="2026-05-04")
    parser.add_argument("--account", type=int, default=1)
    parser.add_argument("--premium", action="store_true",
                        help="Use premium M5 CSV (synthetic entries via existing backtest engine)")
    args = parser.parse_args()

    print(f"=== Trailing replay — bar-by-bar ===")
    print(f"DB: {args.db}")
    print(f"Bars: {args.bars}")
    print(f"Since: {args.since}\n")

    bars = load_bars(args.bars)
    print(f"Loaded {len(bars)} M5 bars, range {bars.index.min()} -> {bars.index.max()}")

    trades = load_trades(args.db, args.since, args.account)
    print(f"Loaded {len(trades)} trades for account {args.account}\n")

    # Filter trades to those within bars range
    in_range = [t for t in trades if t.entry_time >= bars.index.min() and t.entry_time <= bars.index.max()]
    print(f"Trades in bars range: {len(in_range)}")
    no_bars_count = len(trades) - len(in_range)
    if no_bars_count > 0:
        print(f"  ({no_bars_count} trades outside bars range — skipped)")

    print("\n=== VARIANT COMPARISON (bar-by-bar, conservative) ===")
    for cfg in VARIANTS:
        res = run_variant(in_range, bars, cfg)
        print(res.report())

    # Risk-adjusted summary
    print("\n=== RISK-ADJUSTED (PnL / MaxDD) ===")
    print(f"  {'variant':<30} {'PnL':>9} {'MaxDD':>8} {'PnL/MaxDD':>10}")
    for cfg in VARIANTS:
        res = run_variant(in_range, bars, cfg)
        max_dd = 0.0; peak = 0.0; cum = 0.0
        for p in res.pnl_list:
            cum += p
            if cum > peak: peak = cum
            dd = peak - cum
            if dd > max_dd: max_dd = dd
        ratio = res.total_pnl / max_dd if max_dd > 0 else float('inf')
        print(f"  {cfg.name:<30} {res.total_pnl:>+9.2f} {max_dd:>8.2f} {ratio:>10.2f}")

    # Regime breakdown for best variant
    print("\n=== REGIME BREAKDOWN (D-simple 0.30/0.15) ===")
    cfg = TrailingConfig(name="D-simple 0.30/0.15", kind="simple",
                         activation_pct=0.30, trail_pct=0.15)
    by_regime = {}
    for t in in_range:
        by_regime.setdefault(t.regime, []).append(t)
    for regime, rt in sorted(by_regime.items()):
        res = run_variant(rt, bars, cfg)
        print(f"  {regime:<10} n={len(rt):>4}  PnL={res.total_pnl:>+8.2f} "
              f"WR={100*res.wins/max(1,res.wins+res.losses+res.zero):>5.1f}% "
              f"PF={[None if sum(p for p in res.pnl_list if p<0)==0 else round(sum(p for p in res.pnl_list if p>0)/-sum(p for p in res.pnl_list if p<0),2)][0]} "
              f"trail={res.trail_exits} SL={res.sl_hits} TP={res.tp_hits}")


if __name__ == "__main__":
    main()