#!/usr/bin/env python3
"""Sweep risk config (ATR multiplier × RR ratio) on V4+v6-OR ensemble @0.45.

Model + threshold are fixed (winner of compare_models.py). Varies the
trade geometry to find the optimal SL/TP distance per account.

Sweep grid:
  ATR multiplier: 1.5, 2.0, 2.5, 3.0  (SL distance = atr × ATR)
  RR ratio:       1.5, 2.0, 2.5, 3.0  (TP distance = atr × ATR × RR)
  min_confidence: 0.45 (fixed — live value)
  threshold:      0.45 (fixed — ensemble winner)

Outputs PF / WR / kept / PnL / MaxDD% / AvgR per (account, atr, rr, period)
to ψ/lab/sweep-risk-config-2026-06-29/result.tsv.

Usage:
    python3 scripts/sweep_risk_config.py \\
        --accounts B C D \\
        --periods 2025-10-01 2024-01-01 \\
        --atrs 1.5 2.0 2.5 3.0 \\
        --rrs 1.5 2.0 2.5 3.0
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from broky.data.loader import load_timeframe
from broky.backtest.engine import BacktestEngine, BacktestTrade
from broky.ml.trade_outcome_predictor import compute_features_from_candles
from broky.ml.ensemble_predictor import EnsemblePredictor
from broky.indicators.ema import calculate_ema

from scripts.backtest_ml_filter import compute_trade_features
from scripts.compare_models import (
    apply_filter, kept_metrics, initial_risk_per_trade,
)


@dataclass
class SweepAccount:
    name: str
    min_confidence: float


SWEEP_ACCOUNTS = [
    SweepAccount("B", min_confidence=0.45),
    SweepAccount("C", min_confidence=0.45),
    SweepAccount("D", min_confidence=0.45),
]


def run_engine(atr: float, rr: float, min_conf: float,
               df_h1: pd.DataFrame, df_d1: pd.DataFrame,
               start: str, equity: float, risk: float):
    cutoff = pd.Timestamp(start)
    df_h1_f = df_h1[df_h1.index >= cutoff].copy()
    df_d1_f = df_d1[df_d1.index >= cutoff - pd.Timedelta(days=400)].copy()
    engine = BacktestEngine(
        initial_equity=equity, risk_per_trade=risk,
        atr_multiplier=atr, risk_reward_ratio=rr,
        min_confidence=min_conf,
        spread_buffer=2.5, max_holding_bars=48, cooldown_bars=12,
        strategy="swing",
    )
    return engine.run(df_h1_f, warmup=200, d1_df=df_d1_f)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", nargs="+", default=["B", "C", "D"],
                   choices=["A", "B", "C", "D"])
    p.add_argument("--periods", nargs="+", default=["2025-10-01", "2024-01-01"])
    p.add_argument("--atrs", nargs="+", type=float, default=[1.5, 2.0, 2.5, 3.0])
    p.add_argument("--rrs", nargs="+", type=float, default=[1.5, 2.0, 2.5, 3.0])
    p.add_argument("--threshold", type=float, default=0.45)
    p.add_argument("--equity", type=float, default=1000.0)
    p.add_argument("--risk", type=float, default=0.02)
    p.add_argument("--out", default="ψ/lab/sweep-risk-config-2026-06-29/result.tsv")
    p.add_argument("--model", default="ensemble", choices=["ensemble", "v4"],
                   help="ensemble=V4+v6-OR @thresh; v4=V4 single-model")
    args = p.parse_args()

    accounts = [a for a in SWEEP_ACCOUNTS if a.name in args.accounts]

    print("📊 Loading data...")
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

    candle_data: dict[str, pd.DataFrame] = {}
    if m5 is not None:
        m5_lower = m5.copy(); m5_lower.columns = [c.lower() for c in m5_lower.columns]
        candle_data["M5"] = m5_lower
    h1_lower = df_h1.copy(); h1_lower.columns = [c.lower() for c in h1_lower.columns]
    candle_data["H1"] = h1_lower
    d1_lower = df_d1.copy(); d1_lower.columns = [c.lower() for c in d1_lower.columns]
    candle_data["D1"] = d1_lower
    if h4_data is not None:
        h4_lower = h4_data.copy(); h4_lower.columns = [c.lower() for c in h4_lower.columns]
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

    model_choice = args.model.lower()
    if model_choice == "ensemble":
        print("🧠 Loading V4+v6-OR ensemble predictor...")
        predictor = EnsemblePredictor(
            model_dirs=["data/models/trade_outcome_v4", "data/models/trade_outcome_v6"],
            mode="or", loss_threshold=0.99,
        )
    elif model_choice == "v4":
        from broky.ml.trade_outcome_predictor import TradeOutcomePredictor
        print("🧠 Loading V4 single-model predictor...")
        predictor = TradeOutcomePredictor(
            model_dir="data/models/trade_outcome_v4",
            loss_threshold=1.0,
        )
    else:
        raise ValueError(f"unknown --model {args.model!r} (ensemble|v4)")
    print(f"  predictor enabled={getattr(predictor, 'enabled', False)}  model={model_choice}")

    rows: list[dict] = []
    total = len(accounts) * len(args.periods) * len(args.atrs) * len(args.rrs)
    done = 0
    for account in accounts:
        for period in args.periods:
            for atr, rr in product(args.atrs, args.rrs):
                done += 1
                print(f"  [{done}/{total}] {account.name} atr={atr} rr={rr} "
                      f"start={period} ...", end=" ", flush=True)
                result = run_engine(
                    atr, rr, account.min_confidence,
                    df_h1, df_d1, period, args.equity, args.risk,
                )
                trades = result.trades
                if not trades:
                    print("no trades")
                    continue
                features_list = [
                    compute_trade_features(t, df_h1[df_h1.index >= pd.Timestamp(period)].copy(),
                                           candle_data, d1_trend_series)
                    for t in trades
                ]
                # Unfiltered
                un = kept_metrics(trades, [True] * len(trades), args.equity)
                rows.append({
                    "account": account.name, "period": period,
                    "atr": atr, "rr": rr, "model": "UNFILTERED",
                    "threshold": "—", "min_conf": account.min_confidence,
                    **un,
                })
                # Filtered @ threshold
                mask = apply_filter(trades, features_list, predictor, args.threshold)
                m = kept_metrics(trades, mask, args.equity)
                rows.append({
                    "account": account.name, "period": period,
                    "atr": atr, "rr": rr,
                    "model": "V4+v6-OR" if model_choice == "ensemble" else "V4",
                    "threshold": f"{args.threshold:.2f}",
                    "min_conf": account.min_confidence,
                    **m,
                })
                print(f"unfilt PF={un['pf']:.2f} | filt PF={m['pf']:.2f} "
                      f"kept={m['kept']}/{len(trades)} MaxDD={m['max_dd_pct']:.1f}%")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["account", "period", "atr", "rr", "model", "threshold",
            "min_conf", "kept", "pnl", "pf", "wr", "max_dd_pct", "avg_pnl", "avg_r"]
    with out_path.open("w") as f:
        f.write("\t".join(cols) + "\n")
        for r in rows:
            f.write("\t".join(str(r.get(c, "")) for c in cols) + "\n")
    print(f"\n✅ Wrote {len(rows)} rows → {out_path}")
    json_path = out_path.with_suffix(".json")
    with json_path.open("w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"✅ Wrote JSON → {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())