#!/usr/bin/env python3
"""Compare V4 / V5 / V6 / V4+v6-OR-ensemble across accounts, periods, thresholds.

Single-pass sweep that computes per-kept-trade metrics so we can pick the
best model + feature set per account. Outputs a flat TSV the agent can
read aloud + a markdown summary.

Metrics per (model, account, period, threshold):
  - PF (profit factor)             — gross_profit / gross_loss on kept trades
  - WR (win rate)                  — kept_wins / kept
  - Kept / Total                   — trade count surviving filter
  - PnL                            — sum of kept pnls
  - MaxDD%                         — max drawdown from kept-trades equity curve
  - Avg PnL / trade                — kept_pnl / kept
  - Avg R-multiple                 — mean(pnl / initial_risk) on kept trades

Usage:
    python3 scripts/compare_models.py \\
        --accounts B C D \\
        --periods 2025-10-01 2024-01-01 \\
        --thresholds 0.45 0.50 0.55 0.60 0.65 \\
        --models V4 V5 V6 ENSEMBLE \\
        --equity 1000 --risk 0.02
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from broky.data.loader import load_timeframe
from broky.backtest.engine import BacktestEngine, BacktestTrade
from broky.ml.trade_outcome_predictor import (
    TradeOutcomePredictor,
    compute_features_from_candles,
)
from broky.ml.ensemble_predictor import EnsemblePredictor
from broky.indicators.ema import calculate_ema
from shared.models import SignalType

from scripts.backtest_ml_filter import (
    ACCOUNTS, AccountConfig, compute_trade_features,
)


# ─── Model registry ─────────────────────────────────────────────────────────

MODEL_DIRS = {
    "V4": "data/models/trade_outcome_v4",
    "V5": "data/models/trade_outcome_v5",
    "V6": "data/models/trade_outcome_v6",
}
ENSEMBLE_DIRS = ["data/models/trade_outcome_v4", "data/models/trade_outcome_v6"]


@dataclass
class ModelSpec:
    name: str
    kind: str  # "single" or "ensemble"
    model_dir: str | None = None
    member_dirs: list[str] | None = None


def build_models(selected: list[str]) -> list[ModelSpec]:
    specs: list[ModelSpec] = []
    for s in selected:
        if s.upper() == "ENSEMBLE":
            specs.append(ModelSpec(name="V4+v6-OR", kind="ensemble",
                                   member_dirs=ENSEMBLE_DIRS))
        elif s.upper() in MODEL_DIRS:
            specs.append(ModelSpec(name=s.upper(), kind="single",
                                   model_dir=MODEL_DIRS[s.upper()]))
        else:
            raise ValueError(f"unknown model {s!r} (V4|V5|V6|ENSEMBLE)")
    return specs


def load_predictor(spec: ModelSpec) -> object:
    if spec.kind == "ensemble":
        # Internal loss_threshold is unused — apply_filter compares the gated
        # P(LOSS) externally. 0.99 is a valid placeholder (< 1.0 requirement).
        return EnsemblePredictor(model_dirs=spec.member_dirs, mode="or",
                                 loss_threshold=0.99)
    return TradeOutcomePredictor(model_dir=spec.model_dir, loss_threshold=1.0)


# ─── Metrics on kept trades ─────────────────────────────────────────────────


def initial_risk_per_trade(trade: BacktestTrade) -> float:
    """Dollar risk at entry = lot_size * |entry - stop|."""
    return trade.lot_size * abs(trade.entry_price - trade.stop_loss)


def kept_metrics(
    trades: list[BacktestTrade],
    kept_mask: list[bool],
    initial_equity: float,
) -> dict:
    kept = [t for t, k in zip(trades, kept_mask) if k]
    n = len(kept)
    if n == 0:
        return {
            "kept": 0, "pnl": 0.0, "pf": 0.0, "wr": 0.0,
            "max_dd_pct": 0.0, "avg_pnl": 0.0, "avg_r": 0.0,
        }
    pnls = [t.pnl for t in kept]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    pnl = sum(pnls)
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Equity curve from kept trades only — MaxDD from peak
    equity = initial_equity
    peak = initial_equity
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd:
                max_dd = dd

    # R-multiple = pnl / initial_risk
    rs: list[float] = []
    for t in kept:
        r = initial_risk_per_trade(t)
        if r > 0:
            rs.append(t.pnl / r)
    avg_r = sum(rs) / len(rs) if rs else 0.0

    return {
        "kept": n,
        "pnl": pnl,
        "pf": pf,
        "wr": wins / n,
        "max_dd_pct": max_dd,
        "avg_pnl": pnl / n,
        "avg_r": avg_r,
    }


def apply_filter(
    trades: list[BacktestTrade],
    features_list: list[dict | None],
    predictor: object,
    threshold: float,
) -> list[bool]:
    """Return kept_mask per trade. Ensemble OR-gate uses its own threshold;
    for single model we compare predict_loss_proba > threshold."""
    kept_mask: list[bool] = []
    for trade, features in zip(trades, features_list):
        if features is None:
            kept_mask.append(True)  # conservative: keep when no features
            continue
        regime = features.get("regime")
        direction = (
            trade.direction.value.upper()
            if hasattr(trade.direction, "value")
            else str(trade.direction).upper()
        )
        try:
            proba, _ = predictor.predict_loss_proba(
                features=features, regime=regime, direction=direction,
            )
        except Exception:
            proba = None
        if proba is None:
            kept_mask.append(True)  # keep when no prediction
            continue
        # Block if P(LOSS) > threshold (strict >)
        kept_mask.append(proba <= threshold)
    return kept_mask


# ─── Per-account engine + features cache ────────────────────────────────────


@dataclass
class AccountRun:
    trades: list[BacktestTrade]
    features_list: list[dict | None]
    unfiltered: dict


def run_engine(
    account: AccountConfig, df_h1: pd.DataFrame, df_d1: pd.DataFrame,
    candle_data: dict, d1_trend_series, start: str, equity: float, risk: float,
) -> AccountRun:
    cutoff = pd.Timestamp(start)
    df_h1_f = df_h1[df_h1.index >= cutoff].copy()
    df_d1_f = df_d1[df_d1.index >= cutoff - pd.Timedelta(days=400)].copy()
    engine = BacktestEngine(
        initial_equity=equity, risk_per_trade=risk,
        atr_multiplier=account.atr_multiplier,
        risk_reward_ratio=account.risk_reward_ratio,
        min_confidence=account.min_confidence,
        spread_buffer=2.5, max_holding_bars=48, cooldown_bars=12,
        strategy="swing",
    )
    result = engine.run(df_h1_f, warmup=200, d1_df=df_d1_f)
    trades = result.trades
    features_list = [
        compute_trade_features(t, df_h1_f, candle_data, d1_trend_series)
        for t in trades
    ]
    unfiltered = kept_metrics(trades, [True] * len(trades), equity)
    return AccountRun(trades=trades, features_list=features_list,
                      unfiltered=unfiltered)


# ─── Main sweep ─────────────────────────────────────────────────────────────


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accounts", nargs="+", default=["B", "C", "D"],
                   choices=["A", "B", "C", "D"])
    p.add_argument("--periods", nargs="+", default=["2025-10-01", "2024-01-01"],
                   help="Start dates. First = in-sample, rest = OOS.")
    p.add_argument("--thresholds", nargs="+", type=float,
                   default=[0.45, 0.50, 0.55, 0.60, 0.65])
    p.add_argument("--models", nargs="+", default=["V4", "V5", "V6", "ENSEMBLE"])
    p.add_argument("--equity", type=float, default=1000.0)
    p.add_argument("--risk", type=float, default=0.02)
    p.add_argument("--out", default="ψ/lab/compare-models-2026-06-29/result.tsv",
                   help="TSV output path")
    args = p.parse_args()

    specs = build_models(args.models)
    accounts = [a for a in ACCOUNTS if a.name in args.accounts]

    # Load data once
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
                d1_trend_series.iloc[i] = (
                    "bullish" if ema50.iloc[i] > ema200.iloc[i] else "bearish"
                )
            else:
                d1_trend_series.iloc[i] = None
        d1_trend_series = d1_trend_series.dropna()

    # Load predictors once
    print("🧠 Loading predictors...")
    predictors: dict[str, object] = {}
    for spec in specs:
        predictors[spec.name] = load_predictor(spec)
        ok = getattr(predictors[spec.name], "enabled", False)
        print(f"  {spec.name}: enabled={ok}")

    # Cache engine runs per (account, period)
    cache: dict[tuple[str, str], AccountRun] = {}
    rows: list[dict] = []

    for account in accounts:
        for period in args.periods:
            key = (account.name, period)
            if key not in cache:
                print(f"  → engine run {account.name} start={period} ...", end=" ")
                cache[key] = run_engine(
                    account, df_h1, df_d1, candle_data, d1_trend_series,
                    period, args.equity, args.risk,
                )
                n = len(cache[key].trades)
                print(f"{n} trades, unfiltered PF={cache[key].unfiltered['pf']:.2f}")
            run = cache[key]

            # Unfiltered baseline row
            rows.append({
                "account": account.name, "period": period,
                "model": "UNFILTERED", "threshold": "—",
                **run.unfiltered,
            })

            for spec in specs:
                pred = predictors[spec.name]
                for thr in args.thresholds:
                    mask = apply_filter(run.trades, run.features_list, pred, thr)
                    m = kept_metrics(run.trades, mask, args.equity)
                    rows.append({
                        "account": account.name, "period": period,
                        "model": spec.name, "threshold": f"{thr:.2f}",
                        **m,
                    })

    # Output TSV
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["account", "period", "model", "threshold",
            "kept", "pnl", "pf", "wr", "max_dd_pct", "avg_pnl", "avg_r"]
    with out_path.open("w") as f:
        f.write("\t".join(cols) + "\n")
        for r in rows:
            f.write("\t".join(str(r.get(c, "")) for c in cols) + "\n")
    print(f"\n✅ Wrote {len(rows)} rows → {out_path}")

    # Also dump JSON for downstream pretty-print
    json_path = out_path.with_suffix(".json")
    with json_path.open("w") as f:
        json.dump(rows, f, indent=2, default=str)
    print(f"✅ Wrote JSON → {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())