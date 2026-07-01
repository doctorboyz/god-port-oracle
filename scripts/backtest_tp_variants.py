#!/usr/bin/env python3
"""Backtest TP narrowing (B: RR=1.5) + trailing stop (D) on Account A June data.

Uses historical trades from oracle.db (June 2026) with MFE/MAE data.
Simulates what would have happened if TP=1.5×SL instead of 2.0×SL,
with optional trailing stop that locks profit when price moves in favor.

Variables per trade:
  - entry_price, stop_loss, take_profit, direction, lot_size, atr_at_entry
  - mfe_pct: max favorable excursion as % of entry (price went this far in favor)
  - mae_pct: max adverse excursion as % of entry (price went this far against)
  - atr_multiplier, rr_ratio: geometric config in use

Logic per variant:
  baseline (RR=2.0):
    - if mfe_pct >= tp_dist_pct: TP hit, PnL = +tp_dist_pct
    - elif mae_pct >= sl_dist_pct: SL hit, PnL = -sl_dist_pct
    - else: max_holding, PnL = original PnL (use the actual recorded PnL)

  B (RR=1.5):
    - new_tp_dist_pct = 1.5 * sl_dist_pct
    - if mfe_pct >= new_tp_dist_pct: TP hit, PnL = +new_tp_dist_pct
    - elif mae_pct >= sl_dist_pct: SL hit, PnL = -sl_dist_pct
    - else: max_holding, PnL = original PnL

  D (trailing only, RR=2.0):
    - trail_activation = 0.30% (price must move 0.3% in favor to arm trail)
    - trail_distance = 0.15% (lock profit 0.15% below peak)
    - if mfe_pct >= trail_activation:
        exit_pnl_pct = mfe_pct - trail_distance  # close at peak - trail
        if exit_pnl_pct > 0: WIN
      else: original outcome

  B+D (RR=1.5 + trailing):
    - First check trailing (if MFE arms trail, exit at peak-trail)
    - Else check TP at 1.5×SL
    - Else SL
    - Else max_holding

Output: per variant — n_trades, wins, losses, WR%, total PnL, avg PnL, max DD, PF
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Optional


CONTRACT_SIZE = 100.0  # XAUUSD 1 lot = 100 oz
ACCOUNT_ID = 1
TRAIL_ACTIVATION_PCT = 0.30   # arm trailing when MFE >= 0.30%
TRAIL_DISTANCE_PCT = 0.15     # lock profit 0.15% below peak


@dataclass
class Trade:
    id: int
    direction: str  # BUY / SELL
    entry_price: float
    stop_loss: float
    take_profit: float
    lot_size: float
    atr_at_entry: float
    atr_multiplier: float
    rr_ratio: float
    mfe_pct: Optional[float]
    mae_pct: Optional[float]
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
    max_holding: int = 0

    def report(self) -> str:
        wr = 100 * self.wins / max(1, self.wins + self.losses + self.zero)
        avg = self.total_pnl / max(1, self.n)
        # Profit factor
        gross_profit = sum(p for p in self.pnl_list if p > 0)
        gross_loss = -sum(p for p in self.pnl_list if p < 0)
        pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        # Max drawdown from cumulative PnL
        max_dd = 0.0
        peak = 0.0; cum = 0.0
        for p in self.pnl_list:
            cum += p
            if cum > peak: peak = cum
            dd = peak - cum
            if dd > max_dd: max_dd = dd
        return (f"  {self.name:<22} n={self.n:>3} W={self.wins:>3} L={self.losses:>3} "
                f"0={self.zero:>3} WR={wr:>5.1f}% PnL={self.total_pnl:>+8.2f} "
                f"avg={avg:>+6.2f} PF={pf:>4.2f} MaxDD={max_dd:>7.2f} "
                f"TP={self.tp_hits} SL={self.sl_hits} trail={self.trail_exits} mh={self.max_holding}")


def load_trades(db_path: str, since: str = "2026-06-01") -> list[Trade]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT id, direction, entry_price, stop_loss, take_profit,
               lot_size, atr_at_entry, atr_multiplier, rr_ratio,
               mfe_pct, mae_pct, pnl, exit_reason
        FROM live_trades
        WHERE account_id = ? AND timestamp >= ?
          AND exit_reason NOT LIKE '%inferred%'   -- skip phantom/no-data trades
          AND mfe_pct IS NOT NULL                 -- need MFE for simulation
          AND entry_price > 0 AND stop_loss > 0 AND take_profit > 0
        ORDER BY timestamp
    """, (ACCOUNT_ID, since))
    trades = [Trade(
        id=r["id"], direction=r["direction"],
        entry_price=r["entry_price"], stop_loss=r["stop_loss"],
        take_profit=r["take_profit"], lot_size=r["lot_size"] or 0.01,
        atr_at_entry=r["atr_at_entry"] or 0,
        atr_multiplier=r["atr_multiplier"] or 0,
        rr_ratio=r["rr_ratio"] or 0,
        mfe_pct=r["mfe_pct"], mae_pct=r["mae_pct"],
        original_pnl=r["pnl"] or 0,
        original_exit_reason=r["exit_reason"] or "",
    ) for r in cur.fetchall()]
    conn.close()
    return trades


def pnl_pct_to_dollars(pnl_pct: float, entry_price: float, lot_size: float) -> float:
    """Convert PnL% to USD: pnl_pct * entry_price * lot_size * CONTRACT_SIZE / 100."""
    return pnl_pct * entry_price * lot_size * CONTRACT_SIZE / 100.0


def simulate_baseline(t: Trade) -> tuple[float, str]:
    """RR=2.0 (current). Use MFE/MAE vs TP/SL distance."""
    if t.direction == "BUY":
        sl_dist_pct = (t.entry_price - t.stop_loss) / t.entry_price * 100
        tp_dist_pct = (t.take_profit - t.entry_price) / t.entry_price * 100
    else:
        sl_dist_pct = (t.stop_loss - t.entry_price) / t.entry_price * 100
        tp_dist_pct = (t.entry_price - t.take_profit) / t.entry_price * 100
    mfe = t.mfe_pct or 0
    mae = t.mae_pct or 0
    # If both TP and SL reached, assume SL hit first (conservative)
    if mae >= sl_dist_pct:
        return -pnl_pct_to_dollars(sl_dist_pct, t.entry_price, t.lot_size), "stop_loss"
    if mfe >= tp_dist_pct:
        return +pnl_pct_to_dollars(tp_dist_pct, t.entry_price, t.lot_size), "take_profit"
    # max_holding — use original PnL
    return t.original_pnl, "max_holding"


def simulate_b(t: Trade, rr: float = 1.5) -> tuple[float, str]:
    """RR=1.5 (variant B)."""
    if t.direction == "BUY":
        sl_dist_pct = (t.entry_price - t.stop_loss) / t.entry_price * 100
    else:
        sl_dist_pct = (t.stop_loss - t.entry_price) / t.entry_price * 100
    new_tp_dist_pct = rr * sl_dist_pct
    mfe = t.mfe_pct or 0
    mae = t.mae_pct or 0
    if mae >= sl_dist_pct:
        return -pnl_pct_to_dollars(sl_dist_pct, t.entry_price, t.lot_size), "stop_loss"
    if mfe >= new_tp_dist_pct:
        return +pnl_pct_to_dollars(new_tp_dist_pct, t.entry_price, t.lot_size), "take_profit"
    return t.original_pnl, "max_holding"


def simulate_d(t: Trade, activation: float = TRAIL_ACTIVATION_PCT,
               trail: float = TRAIL_DISTANCE_PCT) -> tuple[float, str]:
    """Trailing stop only (variant D). RR stays at 2.0 baseline TP."""
    # baseline outcome first
    base_pnl, base_reason = simulate_baseline(t)
    mfe = t.mfe_pct or 0
    if mfe >= activation:
        # Trail arms — exit at peak - trail_distance
        exit_pnl_pct = mfe - trail
        if exit_pnl_pct > 0:
            return +pnl_pct_to_dollars(exit_pnl_pct, t.entry_price, t.lot_size), "trailing_tp"
        # Trail exit would be loss — fall back to baseline
        return base_pnl, base_reason
    # Not armed — baseline
    return base_pnl, base_reason


def simulate_bd(t: Trade, rr: float = 1.5,
                activation: float = TRAIL_ACTIVATION_PCT,
                trail: float = TRAIL_DISTANCE_PCT) -> tuple[float, str]:
    """B + D: trailing first, else TP at RR=1.5, else SL, else max_holding."""
    if t.direction == "BUY":
        sl_dist_pct = (t.entry_price - t.stop_loss) / t.entry_price * 100
    else:
        sl_dist_pct = (t.stop_loss - t.entry_price) / t.entry_price * 100
    new_tp_dist_pct = rr * sl_dist_pct
    mfe = t.mfe_pct or 0
    mae = t.mae_pct or 0

    # Trail check first — if MFE arms trail, exit at peak - trail
    if mfe >= activation:
        exit_pnl_pct = mfe - trail
        if exit_pnl_pct > 0:
            return +pnl_pct_to_dollars(exit_pnl_pct, t.entry_price, t.lot_size), "trailing_tp"

    # Else standard B logic
    if mae >= sl_dist_pct:
        return -pnl_pct_to_dollars(sl_dist_pct, t.entry_price, t.lot_size), "stop_loss"
    if mfe >= new_tp_dist_pct:
        return +pnl_pct_to_dollars(new_tp_dist_pct, t.entry_price, t.lot_size), "take_profit"
    return t.original_pnl, "max_holding"


def run_variant(trades: list[Trade], name: str, sim_fn) -> VariantResult:
    res = VariantResult(name=name)
    for t in trades:
        pnl, reason = sim_fn(t)
        res.n += 1
        res.pnl_list.append(pnl)
        res.total_pnl += pnl
        if pnl > 0: res.wins += 1
        elif pnl < 0: res.losses += 1
        else: res.zero += 1
        if reason == "take_profit": res.tp_hits += 1
        elif reason == "stop_loss": res.sl_hits += 1
        elif reason == "trailing_tp": res.trail_exits += 1
        elif reason == "max_holding": res.max_holding += 1
    return res


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/app/data/oracle.db")
    parser.add_argument("--since", default="2026-06-01")
    args = parser.parse_args()

    trades = load_trades(args.db, args.since)
    print(f"=== Backtest TP variants — Account A since {args.since} ===")
    print(f"Loaded {len(trades)} trades with MFE/MAE data")
    print(f"Trail config: activation={TRAIL_ACTIVATION_PCT}%, trail_distance={TRAIL_DISTANCE_PCT}%\n")

    # Also try several trail params for B+D
    print("=== VARIANT COMPARISON ===")
    for name, fn in [
        ("Baseline (RR=2.0)", simulate_baseline),
        ("B (RR=1.5)", lambda t: simulate_b(t, 1.5)),
        ("B (RR=1.0)", lambda t: simulate_b(t, 1.0)),
        ("D (trail 0.30/0.15)", simulate_d),
        ("B+D (RR=1.5, trail)", lambda t: simulate_bd(t, 1.5)),
        ("B+D (RR=1.0, trail)", lambda t: simulate_bd(t, 1.0)),
    ]:
        res = run_variant(trades, name, fn)
        print(res.report())

    # Sensitivity sweep for trail params with B (RR=1.5)
    print("\n=== B+D TRAIL SENSITIVITY (RR=1.5) ===")
    print(f"  {'activation':>10} {'trail':>7}  result")
    for act in [0.20, 0.30, 0.40, 0.50]:
        for trail in [0.10, 0.15, 0.20]:
            res = run_variant(trades, f"act={act}/tr={trail}",
                              lambda t, a=act, tr=trail: simulate_bd(t, 1.5, a, tr))
            print(f"  {act:>10.2f} {trail:>7.2f}  PnL={res.total_pnl:>+8.2f} "
                  f"WR={100*res.wins/max(1,res.wins+res.losses+res.zero):>5.1f}% "
                  f"trail_exits={res.trail_exits} TP={res.tp_hits} SL={res.sl_hits}")


if __name__ == "__main__":
    main()