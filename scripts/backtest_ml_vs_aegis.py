#!/usr/bin/env python3
"""ML filter + AEGIS reversal gate — 3-way comparison.

Question: when ML filter pairs with the AEGIS reversal gate, do they reinforce
each other (ML cuts trades the gate lets through but that would lose) or
contradict (ML blocks legitimate reversal trades the gate approved)?

Configs (mirror VPS production):
  - no ML          : AEGIS reversal gate only (baseline)
  - v4 single      : AEGIS + TradeOutcomePredictor(v4) @ ML_LOSS_THRESHOLD=0.55  (Real-A)
  - v4+v6 ensemble : AEGIS + EnsemblePredictor(v4,v6, mode=or, thresh=0.45)       (Demo B/D)

Datasets:
  - premium : data/processed/xauusd_m5_indicators.parquet (200k bars 2023-2026, in-sample)
  - exness  : /Users/doctorboyz/Documents/xau-data/xauusd_5m.csv (13k bars Feb-Apr 2026, OOS)

Environment: Exness Standard (spread $0.20, leverage 1:100, lot step 0.01) —
reuses run_live_backtest() which already includes trend-aligned reversal entry
+ trailing TP + DrawdownProtector + CircuitBreaker + TradeBlocker.

Usage:
  python3 scripts/backtest_ml_vs_aegis.py
  python3 scripts/backtest_ml_vs_aegis.py --data premium
  python3 scripts/backtest_ml_vs_aegis.py --balances 100,1000 --method H3
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from broky.indicators.ema import calculate_ema
from broky.ml.trade_outcome_predictor import TradeOutcomePredictor, compute_features_from_candles
from broky.ml.ensemble_predictor import EnsemblePredictor

from scripts.backtest_live_monte_carlo import (
    run_live_backtest, LiveResult, SPREAD_TYPICAL_USD,
)
from scripts.trend_aligned_reversal_eval import (
    classify_trend, find_swings, find_trend_aligned_reversals, resample_timeframe,
)


V4_DIR = "data/models/trade_outcome_v4"
V6_DIR = "data/models/trade_outcome_v6"


# ---------- Feature computation (mirrors backtest_ml_filter.compute_trade_features) ----------

def build_candle_data(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Lowercase-column candle_data dict (M5/H1/H4/D1) for compute_features_from_candles."""
    cd: dict[str, pd.DataFrame] = {}
    for tf in ("M5", "H1", "H4", "D1"):
        src = df if tf == "M5" else resample_timeframe(df, tf)
        if src is None or len(src) == 0:
            continue
        low = src.copy()
        low.columns = [c.lower() for c in low.columns]
        cd[tf] = low
    return cd


def build_d1_trend_series(df: pd.DataFrame) -> pd.Series:
    """bullish/bearish D1 trend via EMA50 vs EMA200, indexed by D1 timestamp."""
    d1 = resample_timeframe(df, "D1")
    if d1 is None or len(d1) < 200:
        return pd.Series(dtype=object)
    ema50 = calculate_ema(d1["close"], 50)
    ema200 = calculate_ema(d1["close"], 200)
    s = pd.Series(index=d1.index, dtype=object)
    for i in range(len(d1)):
        if pd.notna(ema50.iloc[i]) and pd.notna(ema200.iloc[i]):
            s.iloc[i] = "bullish" if ema50.iloc[i] > ema200.iloc[i] else "bearish"
        else:
            s.iloc[i] = None
    return s.dropna()


def _session_from_hour(hour: int) -> str:
    if 13 <= hour <= 16:
        return "overlap"
    if 8 <= hour <= 16:
        return "london"
    if 13 <= hour <= 22:
        return "new_york"
    if 0 <= hour <= 8:
        return "asian"
    return "overlap"


def _regime_from_features(features: dict) -> str:
    adx = float(features.get("adx", 20.0))
    boll_bw = float(features.get("boll_bw", 0.02))
    if adx >= 25 and boll_bw > 0.035:
        return "volatile"
    if adx >= 25:
        return "trending"
    return "ranging"


def compute_signal_features(sig_bar: int, sig_dir: str, df: pd.DataFrame,
                             candle_data: dict[str, pd.DataFrame],
                             d1_trend_series: pd.Series) -> dict | None:
    """ML features at signal entry (no lookahead). Returns None on failure."""
    try:
        entry_ts = df.index[sig_bar]
    except (IndexError, KeyError):
        return None

    d1_trend = "neutral"
    if d1_trend_series is not None and len(d1_trend_series) > 0:
        valid = d1_trend_series[d1_trend_series.index <= entry_ts]
        if len(valid) > 0:
            d1_trend = valid.iloc[-1]

    h4_trend = "unknown"
    if "H4" in candle_data and len(candle_data["H4"]) >= 200:
        h4_valid = candle_data["H4"][candle_data["H4"].index <= entry_ts].tail(500)
        if len(h4_valid) >= 200:
            h4_ema50 = calculate_ema(h4_valid["close"], 50)
            h4_ema200 = calculate_ema(h4_valid["close"], 200)
            if pd.notna(h4_ema50.iloc[-1]) and pd.notna(h4_ema200.iloc[-1]):
                h4_trend = "bullish" if h4_ema50.iloc[-1] > h4_ema200.iloc[-1] else "bearish"

    hour = entry_ts.hour if hasattr(entry_ts, "hour") else 0
    session = _session_from_hour(hour)

    entry_slices: dict[str, pd.DataFrame] = {}
    for tf_name, df_tf in candle_data.items():
        valid = df_tf[df_tf.index <= entry_ts].tail(500)
        if len(valid) >= 50:
            entry_slices[tf_name] = valid

    direction = "BUY" if sig_dir == "UP" else "SELL"
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
    features["regime"] = _regime_from_features(features)
    features["d1_trend"] = d1_trend
    features["h4_trend"] = h4_trend
    return features


# ---------- ML filtering ----------

def filter_signals_ml(signals: list[tuple[int, str]],
                      features_per_signal: list[dict | None],
                      predictor) -> tuple[list[tuple[int, str]], int, dict]:
    """Apply ML filter to precomputed features. Returns (kept, n_blocked, reasons).

    predictor None → baseline (no filtering). predictor.should_skip(features,
    regime, direction) returns (skip: bool, reason: str).
    """
    if predictor is None:
        return signals, 0, {}
    kept: list[tuple[int, str]] = []
    blocked = 0
    reasons: dict[str, int] = {}
    for sig, feats in zip(signals, features_per_signal):
        if feats is None:
            # No features → conservative: keep (mirrors production no_prediction path)
            kept.append(sig)
            continue
        regime = feats.get("regime", "ranging")
        direction = "BUY" if sig[1] == "UP" else "SELL"
        try:
            skip, reason = predictor.should_skip(features=feats, regime=regime, direction=direction)
        except Exception:
            skip, reason = False, "predictor_error"
        if skip:
            blocked += 1
            tag = reason.split(":")[0][:50].strip()
            reasons[tag] = reasons.get(tag, 0) + 1
        else:
            kept.append(sig)
    return kept, blocked, reasons


# ---------- Config runner ----------

@dataclass
class ConfigResult:
    label: str
    results: dict[float, LiveResult]
    n_blocked: int
    block_reasons: dict


def run_config(signals, df, revs, balances, risk_pct, method,
              predictor, label, features_per_signal) -> ConfigResult:
    kept, n_blocked, reasons = filter_signals_ml(signals, features_per_signal, predictor)
    print(f"\n  CONFIG: {label}")
    print(f"    signals: {len(signals)} → kept {len(kept)} (ML blocked {n_blocked})")
    if reasons:
        print(f"    ML block reasons: {reasons}")
    results: dict[float, LiveResult] = {}
    for bal in balances:
        r = run_live_backtest(kept, df, revs, bal,
                              risk_pct=risk_pct, method=method,
                              spread_usd=SPREAD_TYPICAL_USD, slippage_usd=0.0)
        results[bal] = r
        print(f"    ${bal:>8,.0f}  trades={r.n_trades:>4}  WR={r.win_rate:>5.1f}%  "
              f"PF={r.profit_factor:>5.2f}  PnL={r.total_pnl:>+9,.2f}  "
              f"MaxDD=${r.max_dd:,.2f} ({r.max_dd_pct:>5.1f}%)  "
              f"blocks={dict(r.block_counts)}  exits={dict(r.exit_reasons)}")
    return ConfigResult(label, results, n_blocked, reasons)


# ---------- Dataset loading ----------

def load_dataset(name: str, exness_csv: str) -> pd.DataFrame:
    if name == "premium":
        return pd.read_parquet("data/processed/xauusd_m5_indicators.parquet")
    raw = pd.read_csv(exness_csv)
    raw = raw.rename(columns={c: c.lower() for c in raw.columns})
    raw["timestamp"] = pd.to_datetime(raw["date"])
    df = raw.set_index("timestamp").sort_index()
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            df[col] = 0.0
    return df


# ---------- Main ----------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--method", choices=["H2", "H3"], default="H2")
    p.add_argument("--risk-pct", type=float, default=0.01)
    p.add_argument("--balances", default="100,200,500,1000,10000")
    p.add_argument("--data", choices=["premium", "exness", "both"], default="both")
    p.add_argument("--exness-csv", default="/Users/doctorboyz/Documents/xau-data/xauusd_5m.csv")
    p.add_argument("--v4-dir", default=V4_DIR)
    p.add_argument("--v6-dir", default=V6_DIR)
    p.add_argument("--v4-thresh", type=float, default=0.55,
                   help="ML_LOSS_THRESHOLD for v4 single (Real-A production: 0.55)")
    p.add_argument("--ensemble-thresh", type=float, default=0.45,
                   help="ML_ENSEMBLE_THRESH for v4+v6 OR-gate (Demo B/D production: 0.45)")
    args = p.parse_args()

    balances = [float(x) for x in args.balances.split(",")]
    method_thr = 0.40 if args.method == "H2" else 0.50

    print("=" * 78)
    print("ML FILTER + AEGIS REVERSAL GATE — 3-WAY COMPARISON")
    print("=" * 78)
    print(f"  Env:        Exness Standard (XAUUSD)")
    print(f"  Spread:     ${SPREAD_TYPICAL_USD:.2f}  Leverage: 1:100")
    print(f"  Entry:      {args.method} (pullback ≥ {method_thr}%)")
    print(f"  Risk:       {args.risk_pct*100:.1f}%/trade")
    print(f"  Balances:   {balances}")
    print(f"  Datasets:   {args.data}")
    print(f"  Configs:    no ML | v4 single @ {args.v4_thresh} | v4+v6 ensemble OR @ {args.ensemble_thresh}")

    # ── Load predictors ──
    print(f"\nLoading predictors...", flush=True)
    v4 = TradeOutcomePredictor(model_dir=args.v4_dir, loss_threshold=args.v4_thresh)
    print(f"  v4: enabled={v4.enabled} models={len(v4._models) if v4.enabled else 0}")
    ensemble = EnsemblePredictor(
        model_dirs=[args.v4_dir, args.v6_dir],
        mode="or",
        loss_threshold=args.ensemble_thresh,
    )
    enabled_members = sum(1 for _, pred in ensemble.members if pred.enabled)
    print(f"  ensemble v4+v6 (or): enabled={ensemble.enabled} members={enabled_members}/{len(ensemble.members)}")

    datasets = ["premium", "exness"] if args.data == "both" else [args.data]
    all_summary: list[tuple[str, ConfigResult, ConfigResult, ConfigResult]] = []

    for ds in datasets:
        print(f"\n{'='*78}")
        print(f"DATASET: {ds.upper()}")
        print(f"{'='*78}")
        print(f"Loading {ds} M5...", flush=True)
        try:
            df = load_dataset(ds, args.exness_csv)
        except FileNotFoundError as e:
            print(f"  ⚠️ dataset not found: {e}")
            continue
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
            print("  ⚠️ No signals. Skipping dataset.")
            continue

        # ── Pre-compute features once per signal (reused by both ML configs) ──
        print(f"Computing features for {len(signals)} signals...", flush=True)
        candle_data = build_candle_data(df)
        d1_trend_series = build_d1_trend_series(df)
        features_per_signal = [
            compute_signal_features(sb, sd, df, candle_data, d1_trend_series)
            for sb, sd in signals
        ]
        n_with_feats = sum(1 for f in features_per_signal if f is not None)
        print(f"  features computed: {n_with_feats}/{len(signals)}")

        # ── Run 3 configs ──
        cfg_none = run_config(signals, df, revs, balances, args.risk_pct, args.method,
                              None, f"no ML (baseline)", features_per_signal)
        cfg_v4 = run_config(signals, df, revs, balances, args.risk_pct, args.method,
                            v4, f"v4 single @ {args.v4_thresh}", features_per_signal)
        cfg_ens = run_config(signals, df, revs, balances, args.risk_pct, args.method,
                            ensemble, f"v4+v6 ensemble OR @ {args.ensemble_thresh}",
                            features_per_signal)

        all_summary.append((ds, cfg_none, cfg_v4, cfg_ens))

        # ── Per-dataset comparison table ──
        print(f"\n{'─'*78}")
        print(f"COMPARISON — {ds.upper()}")
        print(f"{'─'*78}")
        print(f"  {'balance':>9}  {'config':<34}  {'trades':>6}  {'WR':>6}  "
              f"{'PF':>5}  {'PnL':>10}  {'MaxDD%':>7}")
        print(f"  {'-'*9}  {'-'*34}  {'-'*6}  {'-'*6}  {'-'*5}  {'-'*10}  {'-'*7}")
        for cfg in (cfg_none, cfg_v4, cfg_ens):
            for bal in balances:
                r = cfg.results[bal]
                pf = "∞" if r.profit_factor == float("inf") else f"{r.profit_factor:.2f}"
                print(f"  ${bal:>8,.0f}  {cfg.label:<34}  {r.n_trades:>6}  "
                      f"{r.win_rate:>5.1f}%  {pf:>5}  {r.total_pnl:>+9,.2f}  "
                      f"{r.max_dd_pct:>6.1f}%")

    # ── Final cross-dataset summary ──
    if not all_summary:
        print("\n⚠️ No datasets produced signals. Nothing to compare.")
        return

    print(f"\n{'='*78}")
    print(f"FINAL SUMMARY — ML filter vs AEGIS reversal gate")
    print(f"{'='*78}")
    print("  Question: do ML filter + AEGIS gate reinforce or contradict?")
    print()
    for ds, cfg_none, cfg_v4, cfg_ens in all_summary:
        print(f"  ── {ds.upper()} ──")
        for bal in balances:
            n = cfg_none.results[bal]
            v = cfg_v4.results[bal]
            e = cfg_ens.results[bal]
            print(f"    ${bal:>8,.0f}  "
                  f"no-ML PnL={n.total_pnl:>+9,.2f} (WR {n.win_rate:>4.1f}% PF {n.profit_factor:>5.2f})  |  "
                  f"v4 PnL={v.total_pnl:>+9,.2f} (WR {v.win_rate:>4.1f}% PF {v.profit_factor:>5.2f})  |  "
                  f"ens PnL={e.total_pnl:>+9,.2f} (WR {e.win_rate:>4.1f}% PF {e.profit_factor:>5.2f})")
        print()

    # ── Verdict ──
    print(f"{'='*78}")
    print(f"VERDICT")
    print(f"{'='*78}")
    for ds, cfg_none, cfg_v4, cfg_ens in all_summary:
        v4_delta = sum(cfg_v4.results[b].total_pnl - cfg_none.results[b].total_pnl for b in balances)
        ens_delta = sum(cfg_ens.results[b].total_pnl - cfg_none.results[b].total_pnl for b in balances)
        v4_blocks = cfg_v4.n_blocked
        ens_blocks = cfg_ens.n_blocked
        print(f"  {ds.upper()}:")
        print(f"    v4 single:  Δ PnL = {v4_delta:+,.2f} across balances (blocked {v4_blocks} signals)")
        print(f"    v4+v6 ens:  Δ PnL = {ens_delta:+,.2f} across balances (blocked {ens_blocks} signals)")
        if v4_delta > 0 and ens_delta > 0:
            print(f"    → ML reinforces AEGIS (both configs improve PnL)")
        elif v4_delta < 0 and ens_delta < 0:
            print(f"    → ML contradicts AEGIS (both configs worsen PnL — blocking good reversal trades)")
        elif v4_delta > 0 > ens_delta:
            print(f"    → v4 helps, ensemble hurts (ensemble OR-gate too aggressive)")
        elif ens_delta > 0 > v4_delta:
            print(f"    → ensemble helps, v4 alone hurts")
        else:
            print(f"    → mixed signal — inspect per-balance results")


if __name__ == "__main__":
    main()