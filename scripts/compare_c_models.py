#!/usr/bin/env python3
"""Head-to-head comparison: V4 single-model vs V4+v6-OR ensemble on Account C.

Loads data ONCE, runs the engine per (geometry, period) combo, then applies
both filters at multiple thresholds to the same cached trades. This isolates
the model + threshold effect from the geometry effect.

Output: ψ/lab/compare-c-models-2026-06-29/result.tsv

Usage:
    python3 scripts/compare_c_models.py \\
        --geometries 2.0:2.5 2.5:3.0 \\
        --periods 2025-10-01 2024-01-01 \\
        --v4-thresholds 0.55 0.60 0.65 0.70 \\
        --ens-thresholds 0.40 0.45 0.50 0.55 0.60 0.65
"""
from __future__ import annotations

import argparse
import json
import sys
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from broky.data.loader import load_timeframe
from broky.backtest.engine import BacktestEngine
from broky.ml.trade_outcome_predictor import (
    TradeOutcomePredictor,
    compute_features_from_candles,
)
from broky.ml.ensemble_predictor import EnsemblePredictor
from broky.indicators.ema import calculate_ema

from scripts.backtest_ml_filter import compute_trade_features
from scripts.compare_models import kept_metrics, apply_filter


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
    p.add_argument("--geometries", nargs="+", default=["2.0:2.5", "2.5:3.0"],
                   help="atr:rr pairs")
    p.add_argument("--periods", nargs="+", default=["2025-10-01", "2024-01-01"],
                   help="Start dates. First = in-sample, rest = OOS.")
    p.add_argument("--v4-thresholds", nargs="+", type=float,
                   default=[0.55, 0.60, 0.65, 0.70])
    p.add_argument("--ens-thresholds", nargs="+", type=float,
                   default=[0.40, 0.45, 0.50, 0.55, 0.60, 0.65])
    p.add_argument("--equity", type=float, default=1000.0)
    p.add_argument("--risk", type=float, default=0.02)
    p.add_argument("--out", default="ψ/lab/compare-c-models-2026-06-29/result.tsv")
    args = p.parse_args()

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

    print("🧠 Loading V4 single-model predictor...")
    v4_pred = TradeOutcomePredictor(
        model_dir="data/models/trade_outcome_v4",
        loss_threshold=1.0,
    )
    print(f"  V4 enabled={v4_pred.enabled}")
    print("🧠 Loading V4+v6-OR ensemble predictor...")
    ens_pred = EnsemblePredictor(
        model_dirs=["data/models/trade_outcome_v4", "data/models/trade_outcome_v6"],
        mode="or", loss_threshold=0.99,
    )
    print(f"  ensemble enabled={ens_pred.enabled}")

    rows: list[dict] = []
    geos = []
    for g in args.geometries:
        atr_s, rr_s = g.split(":")
        geos.append((float(atr_s), float(rr_s)))

    cache: dict[tuple[float, float, str], tuple] = {}
    for atr, rr in geos:
        for period in args.periods:
            key = (atr, rr, period)
            if key not in cache:
                print(f"  → engine C atr={atr} rr={rr} start={period} ...", end=" ", flush=True)
                result = run_engine(atr, rr, 0.45, df_h1, df_d1, period,
                                    args.equity, args.risk)
                trades = result.trades
                features_list = [
                    compute_trade_features(t, df_h1[df_h1.index >= pd.Timestamp(period)].copy(),
                                           candle_data, d1_trend_series)
                    for t in trades
                ]
                cache[key] = (trades, features_list)
                print(f"{len(trades)} trades")
            trades, features_list = cache[key]

            # Unfiltered baseline
            un = kept_metrics(trades, [True] * len(trades), args.equity)
            rows.append({
                "geometry": f"{atr}:{rr}", "period": period, "model": "UNFILTERED",
                "threshold": "—", **un,
            })

            # V4 single-model at each threshold
            for thr in args.v4_thresholds:
                mask = apply_filter(trades, features_list, v4_pred, thr)
                m = kept_metrics(trades, mask, args.equity)
                rows.append({
                    "geometry": f"{atr}:{rr}", "period": period,
                    "model": "V4", "threshold": f"{thr:.2f}", **m,
                })

            # V4+v6-OR ensemble at each threshold
            for thr in args.ens_thresholds:
                mask = apply_filter(trades, features_list, ens_pred, thr)
                m = kept_metrics(trades, mask, args.equity)
                rows.append({
                    "geometry": f"{atr}:{rr}", "period": period,
                    "model": "V4+v6-OR", "threshold": f"{thr:.2f}", **m,
                })

            # Print summary for this geometry/period
            print(f"  [{atr}:{rr} {period}] unfiltered PF={un['pf']:.2f} kept={un['kept']}")
            v4_best = max(
                (r for r in rows if r["geometry"] == f"{atr}:{rr}"
                 and r["period"] == period and r["model"] == "V4"),
                key=lambda r: float(r["pf"]), default=None,
            )
            ens_best = max(
                (r for r in rows if r["geometry"] == f"{atr}:{rr}"
                 and r["period"] == period and r["model"] == "V4+v6-OR"),
                key=lambda r: float(r["pf"]), default=None,
            )
            if v4_best:
                print(f"    V4 best: thr={v4_best['threshold']} PF={float(v4_best['pf']):.2f} "
                      f"kept={v4_best['kept']} MaxDD={float(v4_best['max_dd_pct']):.1f}%")
            if ens_best:
                print(f"    ENS best: thr={ens_best['threshold']} PF={float(ens_best['pf']):.2f} "
                      f"kept={ens_best['kept']} MaxDD={float(ens_best['max_dd_pct']):.1f}%")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["geometry", "period", "model", "threshold",
            "kept", "pnl", "pf", "wr", "max_dd_pct", "avg_pnl", "avg_r"]
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