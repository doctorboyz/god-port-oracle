"""Backtest V4+v6 ensemble — average loss probabilities from both models.

Hypothesis: V4 (32 features, no one-hot) and v6 (65 features, one-hot regime)
have decorrelated errors. Averaging loss_probas could lift PF above either
model alone.

Usage:
    python scripts/backtest_ensemble.py \
        --models-a data/models/trade_outcome_v4 \
        --models-b data/models/trade_outcome_v6 \
        --thresholds 0.55 0.60 0.65 0.70 \
        --start 2025-10-01 --account all
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from broky.data.loader import load_timeframe
from broky.backtest.engine import BacktestEngine
from broky.ml.trade_outcome_predictor import TradeOutcomePredictor
from broky.indicators.ema import calculate_ema
from scripts.backtest_ml_filter import ACCOUNTS, compute_trade_features


def main():
    parser = argparse.ArgumentParser(description="Ensemble backtest V4+v6")
    parser.add_argument("--models-a", required=True, help="First model dir (e.g. v4)")
    parser.add_argument("--models-b", required=True, help="Second model dir (e.g. v6)")
    parser.add_argument("--thresholds", type=float, nargs="+", default=[0.55, 0.60, 0.65, 0.70])
    parser.add_argument("--start", default="2025-10-01")
    parser.add_argument("--equity", type=float, default=1000.0)
    parser.add_argument("--risk", type=float, default=0.02)
    parser.add_argument("--account", default="all")
    parser.add_argument("--mode", default="avg", choices=["avg", "and", "or", "max"],
                        help="avg=mean(loss), and=block if both>thr, or=block if either>thr, max=block if max>thr")
    args = parser.parse_args()

    pred_a = TradeOutcomePredictor(model_dir=args.models_a, loss_threshold=1.0)
    pred_b = TradeOutcomePredictor(model_dir=args.models_b, loss_threshold=1.0)
    if not pred_a.enabled or not pred_b.enabled:
        print("❌ Failed to load one or both models")
        sys.exit(1)
    print(f"✅ Loaded {args.models_a} ({len(pred_a._models)} models) + {args.models_b} ({len(pred_b._models)} models)")

    accounts = ACCOUNTS if args.account == "all" else [a for a in ACCOUNTS if a.name == args.account]

    data_dir = "data/xau-data"
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

    cutoff = pd.Timestamp(args.start)
    df_h1_filtered = df_h1[df_h1.index >= cutoff].copy()
    df_d1_filtered = df_d1[df_d1.index >= cutoff - pd.Timedelta(days=400)].copy()

    print("=" * 100)
    print(f"ENSEMBLE BACKTEST: avg({args.models_a}, {args.models_b})")
    print(f"  Method: avg(loss_proba_a, loss_proba_b), skip if avg > threshold")
    print(f"  Period: {args.start} onward")
    print("=" * 100)

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
            print(f"\n{account.name}: no trades")
            continue

        unfiltered_pnl = sum(t.pnl for t in trades)
        unfiltered_gp = sum(t.pnl for t in trades if t.pnl > 0)
        unfiltered_gl = abs(sum(t.pnl for t in trades if t.pnl < 0))
        unfiltered_pf = unfiltered_gp / unfiltered_gl if unfiltered_gl > 0 else float("inf")
        unfiltered_wr = sum(1 for t in trades if t.pnl > 0) / len(trades)
        print(f"\n{account.name}: unfiltered {len(trades)} trades, PnL ${unfiltered_pnl:.2f}, "
              f"WR {unfiltered_wr*100:.1f}%, PF {unfiltered_pf:.2f}")

        features_list = []
        for trade in trades:
            feats = compute_trade_features(trade, df_h1_filtered, candle_data, d1_trend_series)
            features_list.append(feats)

        for thr in args.thresholds:
            kept, blocked = [], 0
            for trade, features in zip(trades, features_list):
                if features is None:
                    blocked += 1
                    continue
                regime = features.get("regime")
                direction = (
                    trade.direction.value.upper()
                    if hasattr(trade.direction, "value")
                    else str(trade.direction).upper()
                )
                loss_a, _ = pred_a.predict_loss_proba(features=features, regime=regime, direction=direction)
                loss_b, _ = pred_b.predict_loss_proba(features=features, regime=regime, direction=direction)
                if loss_a is None or loss_b is None:
                    kept.append(trade)
                    continue
                if args.mode == "avg":
                    score = (loss_a + loss_b) / 2.0
                    block = score > thr
                elif args.mode == "and":
                    block = (loss_a > thr) and (loss_b > thr)
                elif args.mode == "or":
                    block = (loss_a > thr) or (loss_b > thr)
                elif args.mode == "max":
                    block = max(loss_a, loss_b) > thr
                if block:
                    blocked += 1
                else:
                    kept.append(trade)

            kept_pnl = sum(t.pnl for t in kept)
            kept_gp = sum(t.pnl for t in kept if t.pnl > 0)
            kept_gl = abs(sum(t.pnl for t in kept if t.pnl < 0))
            kept_pf = kept_gp / kept_gl if kept_gl > 0 else float("inf")
            kept_wr = sum(1 for t in kept if t.pnl > 0) / len(kept) if kept else 0
            print(f"  ensemble @{thr:.2f}: kept {len(kept)}, PnL ${kept_pnl:.2f}, "
                  f"WR {kept_wr*100:.1f}%, PF {kept_pf:.2f}, blocked {blocked}")


if __name__ == "__main__":
    main()