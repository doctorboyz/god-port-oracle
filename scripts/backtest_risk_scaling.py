#!/usr/bin/env python3
"""Backtest optimal risk-scaling threshold pairs for ML position sizing.

Tests different (full_size_threshold, skip_threshold) pairs to find the
optimal win-rate / participation trade-off. Currently production uses
(0.50, 0.85) chosen by intuition (ISSUE-002).

Risk-scaling logic (mirrors broky.ml.trade_outcome_predictor.get_risk_multiplier):
  P(LOSS) <= full_size:  multiplier = 1.0 (full size)
  P(LOSS) >= skip:       multiplier = 0.0 (skip trade)
  full_size < P(LOSS) < skip:  linear scaling, multiplier = (skip - p) / (skip - full)

The backtest scales each trade's PnL by its multiplier and reports:
  - Scaled PnL (sum of pnl * multiplier)
  - Scaled WR and PF (wins/losses weighted by multiplier)
  - Participation = avg multiplier (= effective number of trades taken)
  - Skip rate = trades with multiplier == 0

Usage:
    python scripts/backtest_risk_scaling.py --model data/models/trade_outcome_v4 \\
        --pairs 0.50,0.85 0.45,0.80 0.55,0.85 0.50,0.90 0.40,0.75 \\
        --start 2025-10-01 --account all
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from broky.data.loader import load_timeframe
from broky.backtest.engine import BacktestEngine, BacktestTrade
from broky.ml.trade_outcome_predictor import TradeOutcomePredictor, compute_features_from_candles
from broky.indicators.ema import calculate_ema
from shared.models import SignalType

# Reuse account configs + feature computation from backtest_ml_filter
from scripts.backtest_ml_filter import (
    ACCOUNTS, AccountConfig, compute_trade_features,
)


def parse_pair(s: str) -> tuple[float, float]:
    """Parse '0.50,0.85' -> (0.50, 0.85)."""
    parts = s.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"expected 'full,skip' got '{s}'")
    full = float(parts[0])
    skip = float(parts[1])
    if not (0.0 <= full < skip <= 1.0):
        raise argparse.ArgumentTypeError(f"require 0 <= full < skip <= 1, got ({full},{skip})")
    return full, skip


def risk_multiplier(proba_loss: float, full: float, skip: float) -> float:
    """Mirror of TradeOutcomePredictor.get_risk_multiplier linear scaling."""
    if proba_loss <= full:
        return 1.0
    if proba_loss >= skip:
        return 0.0
    return (skip - proba_loss) / (skip - full)


def apply_risk_scaling(
    trades: list[BacktestTrade],
    features_list: list[dict | None],
    predictor: TradeOutcomePredictor,
    full: float,
    skip: float,
) -> dict:
    """Apply risk-multiplier scaling to trades and return aggregate stats."""
    scaled_pnl = 0.0
    weighted_wins = 0.0  # sum of multipliers on profitable trades
    weighted_losses = 0.0  # sum of multipliers on losing trades
    gross_profit = 0.0
    gross_loss = 0.0
    participation = 0.0  # sum of multipliers (effective trades taken)
    skipped = 0
    full_size = 0
    partial = 0
    no_prediction = 0
    no_features = 0

    for trade, features in zip(trades, features_list):
        if features is None:
            no_features += 1
            # No features → keep full size (conservative, matches predictor)
            mult = 1.0
        else:
            regime = features.get("regime")
            direction = (
                trade.direction.value.upper()
                if hasattr(trade.direction, "value")
                else str(trade.direction).upper()
            )
            proba, _ = predictor.predict_loss_proba(
                features=features, regime=regime, direction=direction,
            )
            if proba is None:
                no_prediction += 1
                mult = 1.0
            else:
                mult = risk_multiplier(proba, full, skip)
                if mult == 0.0:
                    skipped += 1
                elif mult == 1.0:
                    full_size += 1
                else:
                    partial += 1

        participation += mult
        scaled_pnl += trade.pnl * mult
        if trade.pnl > 0:
            weighted_wins += mult
            gross_profit += trade.pnl * mult
        elif trade.pnl < 0:
            weighted_losses += mult
            gross_loss += abs(trade.pnl) * mult

    n = len(trades)
    eff_trades = participation  # effective trade count after scaling
    # WR weighted by participation: share of effective trades that were wins
    scaled_wr = weighted_wins / participation if participation > 0 else 0.0
    scaled_pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    scaled_avg = scaled_pnl / eff_trades if eff_trades > 0 else 0.0

    return {
        "n_trades": n,
        "eff_trades": eff_trades,
        "skipped": skipped,
        "full_size": full_size,
        "partial": partial,
        "no_prediction": no_prediction,
        "no_features": no_features,
        "scaled_pnl": scaled_pnl,
        "scaled_wr": scaled_wr,
        "scaled_pf": scaled_pf,
        "scaled_avg_pnl": scaled_avg,
        "participation_rate": participation / n if n else 0.0,
        "skip_rate": skipped / n if n else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backtest optimal risk-scaling threshold pairs (ISSUE-002)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--model", required=True, help="Model directory (e.g., data/models/trade_outcome_v4)")
    parser.add_argument(
        "--pairs", nargs="+", type=parse_pair, default=[(0.50, 0.85)],
        help="Threshold pairs 'full,skip' (default: 0.50,0.85). Example: 0.45,0.80 0.50,0.85 0.55,0.90",
    )
    parser.add_argument("--start", default="2024-01-01", help="Start date (default: 2024-01-01)")
    parser.add_argument("--equity", type=float, default=1000.0, help="Initial equity (default: 1000)")
    parser.add_argument("--risk", type=float, default=0.02, help="Risk per trade (default: 0.02)")
    parser.add_argument("--account", choices=["A", "B", "C", "all"], default="all", help="Account to test (default: all)")
    args = parser.parse_args()

    model_dir = args.model
    if not Path(model_dir).exists():
        print(f"❌ Model directory not found: {model_dir}")
        return 1

    if args.account == "all":
        accounts = ACCOUNTS
    else:
        accounts = [a for a in ACCOUNTS if a.name == args.account]

    # Load data
    data_dir = "data/xau-data"
    print("\n📊 Loading data...")
    df_h1 = load_timeframe(data_dir, "H1")
    df_d1 = load_timeframe(data_dir, "D1")
    m5 = None
    try:
        m5 = load_timeframe(data_dir, "M5")
    except Exception:
        pass
    h4_data = None
    try:
        h4_data = load_timeframe(data_dir, "H4")
    except Exception:
        pass

    print(f"  H1: {len(df_h1)} candles ({df_h1.index[0]} → {df_h1.index[-1]})")

    candle_data = {}
    if m5 is not None:
        m5_lower = m5.copy()
        m5_lower.columns = [c.lower() for c in m5_lower.columns]
        candle_data["M5"] = m5_lower
    h1_lower = df_h1.copy()
    h1_lower.columns = [c.lower() for c in h1_lower.columns]
    candle_data["H1"] = h1_lower
    d1_lower = df_d1.copy()
    d1_lower.columns = [c.lower() for c in d1_lower.columns]
    candle_data["D1"] = d1_lower
    if h4_data is not None:
        h4_lower = h4_data.copy()
        h4_lower.columns = [c.lower() for c in h4_lower.columns]
        candle_data["H4"] = h4_lower

    d1_trend_series = None
    if len(df_d1) >= 200:
        ema50 = calculate_ema(df_d1["close"], 50)
        ema200 = calculate_ema(df_d1["close"], 200)
        d1_trend_series = pd.Series(index=df_d1.index, dtype=object)
        for i in range(len(df_d1)):
            if pd.notna(ema50.iloc[i]) and pd.notna(ema200.iloc[i]):
                d1_trend_series.iloc[i] = "bullish" if ema50.iloc[i] > ema200.iloc[i] else "bearish"
            else:
                d1_trend_series.iloc[i] = None
        d1_trend_series = d1_trend_series.dropna()

    # Load predictor
    name = Path(model_dir).name
    predictor = TradeOutcomePredictor(model_dir=model_dir, loss_threshold=1.0)
    if not predictor.enabled:
        print(f"❌ Failed to load {name}")
        return 1
    print(f"  ✅ Loaded {name}: {len(predictor._models)} models")

    cutoff = pd.Timestamp(args.start)
    df_h1_filtered = df_h1[df_h1.index >= cutoff].copy()
    df_d1_filtered = df_d1[df_d1.index >= cutoff - pd.Timedelta(days=400)].copy()

    print(f"\n{'='*90}")
    print(f"  RISK-SCALING BACKTEST — {name}")
    print(f"  Start: {args.start} | Equity: ${args.equity:,.0f} | Risk: {args.risk:.0%}")
    print(f"  Pairs: {', '.join(f'({f:.2f},{s:.2f})' for f, s in args.pairs)}")
    print(f"{'='*90}")

    rows = []
    for account in accounts:
        engine = BacktestEngine(
            initial_equity=args.equity,
            risk_per_trade=args.risk,
            atr_multiplier=account.atr_multiplier,
            risk_reward_ratio=account.risk_reward_ratio,
            min_confidence=account.min_confidence,
            spread_buffer=2.5,
            max_holding_bars=48,
            cooldown_bars=12,
            strategy="swing",
        )
        result = engine.run(df_h1_filtered, warmup=200, d1_df=df_d1_filtered)
        trades = result.trades
        if not trades:
            print(f"  ⚠️  Account {account.name}: no trades")
            continue

        # Unfiltered baseline
        unfiltered_pnl = sum(t.pnl for t in trades)
        wins = sum(1 for t in trades if t.pnl > 0)
        losses = sum(1 for t in trades if t.pnl <= 0)
        gp = sum(t.pnl for t in trades if t.pnl > 0)
        gl = abs(sum(t.pnl for t in trades if t.pnl < 0))
        unfiltered = {
            "trades": len(trades),
            "pnl": unfiltered_pnl,
            "wr": wins / len(trades) if trades else 0,
            "pf": gp / gl if gl > 0 else float("inf"),
        }
        print(f"\n  Account {account.name}: {len(trades)} trades, "
              f"PnL=${unfiltered_pnl:,.2f}, WR={unfiltered['wr']:.1%}, PF={unfiltered['pf']:.2f}")

        # Compute features once per account
        features_list = [
            compute_trade_features(t, df_h1_filtered, candle_data, d1_trend_series)
            for t in trades
        ]

        for full, skip in args.pairs:
            r = apply_risk_scaling(trades, features_list, predictor, full, skip)
            rows.append({
                "account": account.name,
                "full": full,
                "skip": skip,
                **r,
                "unfiltered_pnl": unfiltered_pnl,
                "delta_pnl": r["scaled_pnl"] - unfiltered_pnl,
            })

    # Grand summary
    if not rows:
        print("\n❌ No results.")
        return 1

    print(f"\n\n{'='*100}")
    print(f"  GRAND SUMMARY — RISK SCALING")
    print(f"{'='*100}")
    print(f"\n  {'Acct':<5} {'(full,skip)':<12} {'N':>4} {'Eff':>6} {'Skip%':>6} "
          f"{'Part%':>6} {'PnL':>10} {'WR':>6} {'PF':>6} {'Δ PnL':>10}")
    print(f"  {'-'*5} {'-'*12} {'-'*4} {'-'*6} {'-'*6} {'-'*6} {'-'*10} {'-'*6} {'-'*6} {'-'*10}")

    for r in rows:
        pair = f"({r['full']:.2f},{r['skip']:.2f})"
        print(f"  {r['account']:<5} {pair:<12} {r['n_trades']:>4} {r['eff_trades']:>6.1f} "
              f"{r['skip_rate']:>5.0%} {r['participation_rate']:>5.0%} "
              f"${r['scaled_pnl']:>9,.0f} {r['scaled_wr']:>5.0%} "
              f"{r['scaled_pf']:>5.2f} ${r['delta_pnl']:>+9,.0f}")

    # Best pair per account (max scaled PnL)
    print(f"\n  🏆 Best pair per account (by scaled PnL):")
    best_by_account = {}
    for r in rows:
        a = r["account"]
        if a not in best_by_account or r["scaled_pnl"] > best_by_account[a]["scaled_pnl"]:
            best_by_account[a] = r
    for a, r in best_by_account.items():
        print(f"     Account {a}: ({r['full']:.2f},{r['skip']:.2f}) → "
              f"PnL=${r['scaled_pnl']:,.0f}, PF={r['scaled_pf']:.2f}, "
              f"participation={r['participation_rate']:.0%}")

    return 0


if __name__ == "__main__":
    sys.exit(main())