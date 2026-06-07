#!/usr/bin/env python3
"""Backtest with ML filter — compare performance across model versions and thresholds.

Runs BacktestEngine, computes features at each trade entry, then applies ML filter
to compare filtered vs unfiltered results. Supports multiple model dirs and thresholds
in a single run for easy A/B comparison.

Usage:
    # Compare v4 vs v5 model at default threshold (0.65)
    python scripts/backtest_ml_filter.py --models data/models/trade_outcome_v4 data/models/trade_outcome_v5

    # Test multiple thresholds
    python scripts/backtest_ml_filter.py --models data/models/trade_outcome_v5 --thresholds 0.55 0.60 0.65 0.70

    # Full comparison: 2 models x 3 thresholds
    python scripts/backtest_ml_filter.py \\
        --models data/models/trade_outcome_v4 data/models/trade_outcome_v5 \\
        --thresholds 0.60 0.65 0.70 \\
        --start 2024-01-01 --equity 1000

    # Single model, single threshold with details
    python scripts/backtest_ml_filter.py \\
        --models data/models/trade_outcome_v5 \\
        --thresholds 0.65 --verbose
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

from broky.data.loader import load_timeframe
from broky.backtest.engine import BacktestEngine, BacktestTrade
from broky.ml.trade_outcome_predictor import TradeOutcomePredictor, compute_features_from_candles
from broky.indicators.ema import calculate_ema
from shared.models import SignalType


# ─── Account configs matching VPS live settings ───────────────────────────

@dataclass
class AccountConfig:
    name: str
    atr_multiplier: float
    risk_reward_ratio: float
    min_confidence: float


ACCOUNTS = [
    AccountConfig("A", atr_multiplier=3.0, risk_reward_ratio=3.0, min_confidence=0.35),
    AccountConfig("B", atr_multiplier=2.5, risk_reward_ratio=2.5, min_confidence=0.45),
    AccountConfig("C", atr_multiplier=2.0, risk_reward_ratio=2.0, min_confidence=0.60),
]


# ─── Feature computation at trade entry ─────────────────────────────────────

def compute_trade_features(
    trade: BacktestTrade,
    df_h1: pd.DataFrame,
    candle_data: dict[str, pd.DataFrame],
    d1_trend_series: pd.Series | None,
) -> dict | None:
    """Compute ML features at trade entry point (no lookahead)."""
    if trade.entry_idx is None:
        return None

    try:
        entry_ts = df_h1.index[trade.entry_idx]
    except (IndexError, KeyError):
        return None

    # D1 trend at entry
    d1_trend = "neutral"
    if d1_trend_series is not None:
        valid = d1_trend_series[d1_trend_series.index <= entry_ts]
        if len(valid) > 0:
            d1_trend = valid.iloc[-1]

    # H4 trend at entry
    h4_trend = "unknown"
    if "H4" in candle_data and len(candle_data["H4"]) >= 200:
        h4_valid = candle_data["H4"][candle_data["H4"].index <= entry_ts].tail(500)
        if len(h4_valid) >= 200:
            h4_ema50 = calculate_ema(h4_valid["close"], 50)
            h4_ema200 = calculate_ema(h4_valid["close"], 200)
            if pd.notna(h4_ema50.iloc[-1]) and pd.notna(h4_ema200.iloc[-1]):
                h4_trend = "bullish" if h4_ema50.iloc[-1] > h4_ema200.iloc[-1] else "bearish"

    # Session
    hour = entry_ts.hour if hasattr(entry_ts, "hour") else 0
    if 13 <= hour <= 16:
        session = "overlap"
    elif 8 <= hour <= 16:
        session = "london"
    elif 13 <= hour <= 22:
        session = "new_york"
    elif 0 <= hour <= 8:
        session = "asian"
    else:
        session = "overlap"

    # Slice candles for feature computation
    entry_slices = {}
    for tf_name, df in candle_data.items():
        valid = df[df.index <= entry_ts].tail(500)
        if len(valid) >= 50:
            entry_slices[tf_name] = valid

    direction = trade.direction.value

    features = compute_features_from_candles(
        candles=entry_slices,
        direction=direction,
        spread=0,
        d1_trend=d1_trend,
        h4_trend=h4_trend,
        session=session,
    )

    if not features:
        return None

    # Classify regime from features
    adx = float(features.get("adx", 20.0))
    boll_bw = float(features.get("boll_bw", 0.02))
    if adx >= 25 and boll_bw > 0.035:
        regime = "volatile"
    elif adx >= 25:
        regime = "trending"
    else:
        regime = "ranging"

    features["regime"] = regime
    features["d1_trend"] = d1_trend
    features["h4_trend"] = h4_trend

    return features


# ─── ML filter application ──────────────────────────────────────────────────

def apply_ml_filter(
    trades: list[BacktestTrade],
    features_list: list[dict | None],
    predictor: TradeOutcomePredictor,
    threshold: float,
    df_h1: pd.DataFrame,
) -> dict:
    """Apply ML filter to trades and return comparison stats."""
    kept_trades = []
    filtered_trades = []
    filter_reasons = {"high_loss_proba": 0, "no_prediction": 0, "no_features": 0}

    for trade, features in zip(trades, features_list):
        if features is None:
            filtered_trades.append(trade)
            filter_reasons["no_features"] += 1
            continue

        regime = features.get("regime")
        direction = trade.direction.value.upper() if hasattr(trade.direction, "value") else str(trade.direction).upper()

        loss_proba, model_used = predictor.predict_loss_proba(
            features=features,
            regime=regime,
            direction=direction,
        )

        if loss_proba is None:
            # No model available — keep trade (conservative: don't block unknown)
            kept_trades.append(trade)
            filter_reasons["no_prediction"] += 1
            continue

        if loss_proba > threshold:
            filtered_trades.append(trade)
            filter_reasons["high_loss_proba"] += 1
        else:
            kept_trades.append(trade)

    # Stats for kept trades
    kept_pnl = sum(t.pnl for t in kept_trades)
    kept_wins = sum(1 for t in kept_trades if t.pnl > 0)
    kept_losses = sum(1 for t in kept_trades if t.pnl <= 0)

    # Stats for filtered (blocked) trades
    filt_pnl = sum(t.pnl for t in filtered_trades)
    filt_wins = sum(1 for t in filtered_trades if t.pnl > 0)
    filt_losses = sum(1 for t in filtered_trades if t.pnl <= 0)

    # Calculate derived metrics
    kept_win_rate = kept_wins / len(kept_trades) if kept_trades else 0
    kept_gross_profit = sum(t.pnl for t in kept_trades if t.pnl > 0)
    kept_gross_loss = abs(sum(t.pnl for t in kept_trades if t.pnl < 0))
    kept_pf = kept_gross_profit / kept_gross_loss if kept_gross_loss > 0 else float("inf")

    return {
        "kept": len(kept_trades),
        "filtered": len(filtered_trades),
        "kept_pnl": kept_pnl,
        "kept_wins": kept_wins,
        "kept_losses": kept_losses,
        "kept_win_rate": kept_win_rate,
        "kept_profit_factor": kept_pf,
        "kept_avg_pnl": kept_pnl / len(kept_trades) if kept_trades else 0,
        "filtered_pnl": filt_pnl,
        "filtered_wins": filt_wins,
        "filtered_losses": filt_losses,
        "filter_reasons": filter_reasons,
        "pnl_saved": -filt_pnl if filt_pnl < 0 else 0,  # loss avoided by filter
        "pnl_lost": filt_pnl if filt_pnl > 0 else 0,    # profit missed by filter
    }


# ─── Results printing ───────────────────────────────────────────────────────

def print_comparison(
    model_name: str,
    threshold: float,
    unfiltered: dict,
    filtered: dict,
    verbose: bool = False,
) -> None:
    """Print side-by-side comparison of unfiltered vs ML-filtered results."""
    print(f"\n{'='*78}")
    print(f"  Model: {model_name} | Threshold: {threshold:.2f}")
    print(f"{'='*78}")

    print(f"\n  {'Metric':<30} {'Unfiltered':>14} {'ML Filtered':>14} {'Diff':>14}")
    print(f"  {'-'*30} {'-'*14} {'-'*14} {'-'*14}")

    def fmt_money(v):
        return f"${v:,.2f}"

    def fmt_pct(v):
        return f"{v:.1%}"

    def fmt_pf(v):
        return f"{v:.2f}" if v != float("inf") else "∞"

    trades_diff = filtered["kept"] - unfiltered["trades"]
    pnl_diff = filtered["kept_pnl"] - unfiltered["pnl"]
    wr_diff = filtered["kept_win_rate"] - unfiltered["win_rate"]
    pf_diff = filtered["kept_profit_factor"] - unfiltered["profit_factor"]

    print(f"  {'Trades':<30} {unfiltered['trades']:>14} {filtered['kept']:>14} {trades_diff:>14}")
    print(f"  {'Filtered (blocked)':<30} {'':>14} {filtered['filtered']:>14} {'':>14}")
    print(f"  {'Total PnL':<30} {fmt_money(unfiltered['pnl']):>14} {fmt_money(filtered['kept_pnl']):>14} {fmt_money(pnl_diff):>14}")
    print(f"  {'Win Rate':<30} {fmt_pct(unfiltered['win_rate']):>14} {fmt_pct(filtered['kept_win_rate']):>14} {fmt_pct(wr_diff):>14}")
    print(f"  {'Profit Factor':<30} {fmt_pf(unfiltered['profit_factor']):>14} {fmt_pf(filtered['kept_profit_factor']):>14} {fmt_pf(pf_diff):>14}")
    print(f"  {'Avg PnL/trade':<30} {fmt_money(unfiltered['avg_pnl']):>14} {fmt_money(filtered['kept_avg_pnl']):>14} {'':>14}")

    # Filter impact
    print(f"\n  {'Filter Impact':}")
    print(f"  {'Blocked trades':<30} {filtered['filtered']:>14}")
    print(f"  {'  - high loss proba':<30} {filtered['filter_reasons']['high_loss_proba']:>14}")
    print(f"  {'  - no prediction':<30} {filtered['filter_reasons']['no_prediction']:>14}")
    print(f"  {'  - no features':<30} {filtered['filter_reasons']['no_features']:>14}")
    print(f"  {'Losses avoided':<30} {fmt_money(filtered['pnl_saved']):>14}")
    print(f"  {'Profits missed':<30} {fmt_money(filtered['pnl_lost']):>14}")

    # Blocked trades breakdown
    if filtered["filtered"] > 0:
        block_wr = filtered["filtered_wins"] / filtered["filtered"] if filtered["filtered"] else 0
        print(f"  {'Blocked trades WR':<30} {fmt_pct(block_wr):>14}")
        # If blocked WR > 50%, filter is blocking too many good trades
        if block_wr > 0.5:
            print(f"  {'⚠️  Filter blocks more wins than losses!':<30}")

    # Verdict
    if pnl_diff > 0:
        verdict = "✅ ML filter IMPROVES PnL"
    elif pnl_diff < -10:
        verdict = "❌ ML filter WORSENS PnL"
    else:
        verdict = "⚖️  ML filter has minimal impact"
    print(f"\n  {verdict} (Δ${pnl_diff:,.2f})")


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Backtest with ML filter — compare model versions and thresholds",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--models", nargs="+", required=True,
        help="Model directories to compare (e.g., data/models/trade_outcome_v4 data/models/trade_outcome_v5)",
    )
    parser.add_argument(
        "--thresholds", nargs="+", type=float, default=[0.65],
        help="Loss probability thresholds to test (default: 0.65)",
    )
    parser.add_argument("--start", default="2024-01-01", help="Start date (default: 2024-01-01)")
    parser.add_argument("--equity", type=float, default=1000.0, help="Initial equity (default: 1000)")
    parser.add_argument("--risk", type=float, default=0.02, help="Risk per trade (default: 0.02)")
    parser.add_argument("--account", choices=["A", "B", "C", "all"], default="all", help="Account to test (default: all)")
    parser.add_argument("--verbose", action="store_true", help="Show per-trade details")

    args = parser.parse_args()

    # Validate model dirs
    for model_dir in args.models:
        if not Path(model_dir).exists():
            print(f"❌ Model directory not found: {model_dir}")
            sys.exit(1)

    # Select accounts
    if args.account == "all":
        accounts = ACCOUNTS
    else:
        accounts = [a for a in ACCOUNTS if a.name == args.account]

    # ─── Load data ───────────────────────────────────────────────────────
    data_dir = "data/xau-data"
    print("\n📊 Loading data...")
    df_h1 = load_timeframe(data_dir, "H1")
    df_d1 = load_timeframe(data_dir, "D1")

    # Load M5 and H4 for feature computation
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
    print(f"  D1: {len(df_d1)} candles ({df_d1.index[0]} → {df_d1.index[-1]})")
    if m5 is not None:
        print(f"  M5: {len(m5)} candles")
    if h4_data is not None:
        print(f"  H4: {len(h4_data)} candles")

    # Prepare candle data for feature computation
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

    # Prepare D1 trend series
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

    # ─── Load predictors ─────────────────────────────────────────────────
    predictors = {}
    for model_dir in args.models:
        name = Path(model_dir).name
        predictor = TradeOutcomePredictor(model_dir=model_dir, loss_threshold=1.0)
        if predictor.enabled:
            n_models = len(predictor._models)
            print(f"  ✅ Loaded {name}: {n_models} models")
            predictors[name] = predictor
        else:
            print(f"  ❌ Failed to load {name}")

    if not predictors:
        print("\n❌ No models loaded. Exiting.")
        sys.exit(1)

    # ─── Run backtests ───────────────────────────────────────────────────
    cutoff = pd.Timestamp(args.start)
    df_h1_filtered = df_h1[df_h1.index >= cutoff].copy()
    df_d1_filtered = df_d1[df_d1.index >= cutoff - pd.Timedelta(days=400)].copy()

    print(f"\n{'='*78}")
    print(f"  BACKTEST: ML Filter Comparison")
    print(f"  Start: {args.start} | Equity: ${args.equity:,.0f} | Risk: {args.risk:.0%}")
    print(f"  Models: {', '.join(predictors.keys())}")
    print(f"  Thresholds: {', '.join(f'{t:.2f}' for t in args.thresholds)}")
    print(f"{'='*78}")

    # Summary table for all combinations
    summary_rows = []

    for account in accounts:
        print(f"\n{'─'*78}")
        print(f"  Account {account.name}: ATR={account.atr_multiplier}, "
              f"RR={account.risk_reward_ratio}, Conf={account.min_confidence}")
        print(f"{'─'*78}")

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
            print(f"  ⚠️  No trades generated")
            continue

        # Unfiltered stats
        wins = sum(1 for t in trades if t.pnl > 0)
        losses = sum(1 for t in trades if t.pnl <= 0)
        gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
        unfiltered = {
            "trades": len(trades),
            "pnl": sum(t.pnl for t in trades),
            "wins": wins,
            "losses": losses,
            "win_rate": wins / len(trades) if trades else 0,
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
            "avg_pnl": sum(t.pnl for t in trades) / len(trades) if trades else 0,
        }

        print(f"  Unfiltered: {len(trades)} trades, PnL=${unfiltered['pnl']:,.2f}, "
              f"WR={unfiltered['win_rate']:.1%}, PF={unfiltered['profit_factor']:.2f}")

        # Compute features for all trades
        print(f"  Computing features for {len(trades)} trades...")
        features_list = []
        for trade in trades:
            feats = compute_trade_features(trade, df_h1_filtered, candle_data, d1_trend_series)
            features_list.append(feats)

        feats_ok = sum(1 for f in features_list if f is not None)
        print(f"  Features computed: {feats_ok}/{len(trades)} trades")

        # Apply each model x threshold combination
        for model_name, predictor in predictors.items():
            for threshold in args.thresholds:
                filtered = apply_ml_filter(
                    trades, features_list, predictor, threshold, df_h1_filtered,
                )

                print_comparison(model_name, threshold, unfiltered, filtered, args.verbose)

                summary_rows.append({
                    "account": account.name,
                    "model": model_name,
                    "threshold": threshold,
                    "unfiltered_trades": unfiltered["trades"],
                    "unfiltered_pnl": unfiltered["pnl"],
                    "unfiltered_wr": unfiltered["win_rate"],
                    "unfiltered_pf": unfiltered["profit_factor"],
                    "kept_trades": filtered["kept"],
                    "filtered_trades": filtered["filtered"],
                    "kept_pnl": filtered["kept_pnl"],
                    "kept_wr": filtered["kept_win_rate"],
                    "kept_pf": filtered["kept_profit_factor"],
                    "pnl_diff": filtered["kept_pnl"] - unfiltered["pnl"],
                    "losses_avoided": filtered["pnl_saved"],
                    "profits_missed": filtered["pnl_lost"],
                })

    # ─── Grand summary table ─────────────────────────────────────────────
    if summary_rows:
        print(f"\n\n{'='*90}")
        print(f"  GRAND SUMMARY")
        print(f"{'='*90}")

        print(f"\n  {'Account':<8} {'Model':<25} {'Thresh':>6} {'Trades':>6} "
              f"{'Kept':>6} {'PnL':>10} {'WR':>6} {'PF':>6} {'Δ PnL':>10}")
        print(f"  {'-'*8} {'-'*25} {'-'*6} {'-'*6} {'-'*6} {'-'*10} {'-'*6} {'-'*6} {'-'*10}")

        for r in summary_rows:
            print(f"  {r['account']:<8} {r['model']:<25} {r['threshold']:>5.2f} "
                  f"{r['unfiltered_trades']:>6} {r['kept_trades']:>6} "
                  f"${r['kept_pnl']:>9,.2f} {r['kept_wr']:>5.1%} {r['kept_pf']:>5.2f} "
                  f"${r['pnl_diff']:>+9,.2f}")

        # Best combination per account
        print(f"\n  🏆 Best ML config per account:")
        for account in accounts:
            acct_rows = [r for r in summary_rows if r["account"] == account.name]
            if acct_rows:
                best = max(acct_rows, key=lambda r: r["kept_pnl"])
                print(f"     Account {account.name}: {best['model']} @ "
                      f"threshold={best['threshold']:.2f} → "
                      f"PnL=${best['kept_pnl']:,.2f} (Δ${best['pnl_diff']:+,.2f})")


if __name__ == "__main__":
    main()