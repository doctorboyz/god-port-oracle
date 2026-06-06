#!/usr/bin/env python3
"""Backtest matching current paper trading config per account (A, B, C).

Runs the BacktestEngine with each account's live parameters, then simulates
Option C (TP1 → Scale-In) on the resulting trades using M5 candle data for MFE.

Current VPS configs:
  A: ATR=3.0, RR=3.0, conf=0.35, partial_tp=ON, tp1_ratio=0.5, rr_scale_in=2.5
  B: ATR=2.5, RR=2.5, conf=0.45, partial_tp=ON, tp1_ratio=0.5, rr_scale_in=2.5
  C: ATR=2.0, RR=2.0, conf=0.60, partial_tp=ON, tp1_ratio=0.5, rr_scale_in=2.5

Usage:
    python scripts/backtest_live_config.py
    python scripts/backtest_live_config.py --start 2024-01-01
    python scripts/backtest_live_config.py --start 2024-01-01 --equity 1000
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from broky.data.loader import load_timeframe
from broky.backtest.engine import BacktestEngine, BacktestTrade
from broky.ml.trade_outcome_predictor import compute_features_from_candles
from broky.indicators.ema import calculate_ema
from shared.models import SignalType
from metty.core.db import (
    init_db,
    ensure_synthetic_account,
    insert_synthetic_trade,
    insert_synthetic_trade_outcome,
)


# ─── Account configs matching VPS live settings ───────────────────────────

@dataclass
class AccountConfig:
    name: str
    atr_multiplier: float
    risk_reward_ratio: float
    min_confidence: float
    tp1_ratio: float
    rr_scale_in: float


ACCOUNTS = [
    AccountConfig("A", atr_multiplier=3.0, risk_reward_ratio=3.0, min_confidence=0.35, tp1_ratio=0.5, rr_scale_in=2.5),
    AccountConfig("B", atr_multiplier=2.5, risk_reward_ratio=2.5, min_confidence=0.45, tp1_ratio=0.5, rr_scale_in=2.5),
    AccountConfig("C", atr_multiplier=2.0, risk_reward_ratio=2.0, min_confidence=0.60, tp1_ratio=0.5, rr_scale_in=2.5),
]


# ─── MFE analysis with M5 candles ──────────────────────────────────────────

def find_mfe_for_trade(
    trade: BacktestTrade,
    df_h1: pd.DataFrame,
    m5: pd.DataFrame | None,
    tp1_ratio: float,
    rr_scale_in: float,
) -> dict:
    """Walk forward through candles to find MFE and check if TP1 was reached.

    Returns dict with MFE data and Option C simulation results.
    """
    direction = trade.direction
    entry = trade.entry_price
    tp = trade.take_profit
    sl = trade.stop_loss

    if not tp or not sl or not entry:
        return {"reached_tp1": False, "reached_tp": False, "reached_sl": False,
                "option_c_pnl": trade.pnl, "scenario": "no_data"}

    tp_distance = abs(tp - entry)
    sl_distance = abs(sl - entry)

    # TP1 price
    if direction == SignalType.BUY:
        tp1 = entry + tp_distance * tp1_ratio
    else:
        tp1 = entry - tp_distance * tp1_ratio

    tp1_distance = abs(tp1 - entry)

    # Scale-in SL
    remaining_distance = abs(tp - tp1)
    if direction == SignalType.BUY:
        new_sl = tp1 - remaining_distance / rr_scale_in
    else:
        new_sl = tp1 + remaining_distance / rr_scale_in
    new_sl_distance = abs(tp1 - new_sl)

    # Walk forward from entry bar
    entry_idx = trade.entry_idx
    exit_idx = trade.exit_idx if trade.exit_idx else entry_idx + 48

    reached_tp1 = False
    reached_tp = False
    reached_sl = False
    mfe = 0.0

    # Use M5 data if available, otherwise use H1 data
    if m5 is not None:
        entry_ts = df_h1.index[entry_idx]
        future = m5[m5.index >= entry_ts].head((exit_idx - entry_idx + 1) * 12)
    else:
        future = df_h1.iloc[entry_idx:exit_idx + 1]

    for _, row in future.iterrows():
        high = float(row["high"])
        low = float(row["low"])

        if direction == SignalType.BUY:
            favorable = high - entry
            adverse = entry - low
            if high >= tp1 and not reached_tp1:
                reached_tp1 = True
            if high >= tp and not reached_tp:
                reached_tp = True
            if low <= sl and not reached_sl:
                reached_sl = True
        else:
            favorable = entry - low
            adverse = high - entry
            if low <= tp1 and not reached_tp1:
                reached_tp1 = True
            if low <= tp and not reached_tp:
                reached_tp = True
            if high >= sl and not reached_sl:
                reached_sl = True

        mfe = max(mfe, favorable)

        if reached_tp and reached_sl:
            break

    # ─── Simulate Option C ──────────────────────────────────────────────
    current_pnl = trade.pnl
    contract_size = 100.0
    lot_size = trade.lot_size

    if not reached_tp1:
        # Never reached TP1 → same as current strategy
        scenario = "never_tp1"
        option_c_pnl = current_pnl

    elif reached_tp and not reached_sl:
        # Reached both TP1 and final TP → win on both positions
        tp1_profit = tp1_distance * lot_size * contract_size
        scale_in_profit = remaining_distance * lot_size * contract_size
        option_c_pnl = round(tp1_profit + scale_in_profit, 2)
        scenario = "tp1_then_tp"

    elif reached_tp1 and reached_sl:
        # Reached TP1 then SL → win TP1, lose scale-in
        tp1_profit = tp1_distance * lot_size * contract_size
        scale_in_loss = new_sl_distance * lot_size * contract_size
        option_c_pnl = round(tp1_profit - scale_in_loss, 2)
        scenario = "tp1_then_sl"

    elif reached_tp1 and not reached_tp and not reached_sl:
        # Reached TP1 but no final outcome → max_holding or still open
        tp1_profit = tp1_distance * lot_size * contract_size
        if current_pnl > 0:
            option_c_pnl = round(tp1_profit + max(0, current_pnl - tp1_profit), 2)
        else:
            option_c_pnl = round(tp1_profit - new_sl_distance * lot_size * contract_size, 2)
        scenario = "tp1_then_holding"
    else:
        scenario = "unknown"
        option_c_pnl = current_pnl

    return {
        "reached_tp1": reached_tp1,
        "reached_tp": reached_tp,
        "reached_sl": reached_sl,
        "option_c_pnl": option_c_pnl,
        "current_pnl": current_pnl,
        "scenario": scenario,
        "tp1_price": round(tp1, 2),
        "new_sl": round(new_sl, 2),
        "tp1_profit": round(tp1_distance * lot_size * contract_size, 2),
        "new_sl_loss": round(new_sl_distance * lot_size * contract_size, 2),
    }


def monte_carlo_sl_trade(trade: BacktestTrade, tp1_ratio: float) -> dict:
    """Monte Carlo probability for SL trades (no M5 data for MFE)."""
    direction = trade.direction
    entry = trade.entry_price
    tp = trade.take_profit
    sl = trade.stop_loss

    tp_distance = abs(tp - entry)
    sl_distance = abs(sl - entry)

    if direction == SignalType.BUY:
        tp1 = entry + tp_distance * tp1_ratio
    else:
        tp1 = entry - tp_distance * tp1_ratio

    tp1_distance = abs(tp1 - entry)

    # Random walk probability of reaching TP1 before SL
    tp1_prob = sl_distance / (tp1_distance + sl_distance) if (tp1_distance + sl_distance) > 0 else 0

    return {
        "tp1_prob": tp1_prob,
        "tp1_distance": tp1_distance,
        "sl_distance": sl_distance,
    }


# ─── Main ───────────────────────────────────────────────────────────────────

def run_account_backtest(
    account: AccountConfig,
    df_h1: pd.DataFrame,
    df_d1: pd.DataFrame,
    m5: pd.DataFrame | None,
    start_date: str,
    initial_equity: float,
    risk_per_trade: float,
    mc_runs: int,
) -> dict:
    """Run backtest for a single account config with and without Option C."""

    # Filter data by start date
    cutoff = pd.Timestamp(start_date)
    df_h1_filtered = df_h1[df_h1.index >= cutoff].copy()
    df_d1_filtered = df_d1[df_d1.index >= cutoff - pd.Timedelta(days=400)].copy()

    print(f"\n  Running backtest for Account {account.name}...")
    print(f"    ATR={account.atr_multiplier}, RR={account.risk_reward_ratio}, "
          f"Conf={account.min_confidence}, TP1 ratio={account.tp1_ratio}, "
          f"RR scale-in={account.rr_scale_in}")
    print(f"    Data: {len(df_h1_filtered)} H1 candles from {df_h1_filtered.index[0]} → {df_h1_filtered.index[-1]}")

    engine = BacktestEngine(
        initial_equity=initial_equity,
        risk_per_trade=risk_per_trade,
        atr_multiplier=account.atr_multiplier,
        risk_reward_ratio=account.risk_reward_ratio,
        min_confidence=account.min_confidence,
        spread_buffer=2.5,
        max_holding_bars=48,
        cooldown_bars=12,
        strategy="swing",
    )

    result = engine.run(df_h1_filtered, warmup=200, d1_df=df_d1_filtered)

    # ─── Current strategy results ────────────────────────────────────────
    trades = result.trades
    if not trades:
        print(f"    ⚠️  No trades generated for Account {account.name}")
        return {
            "account": account.name,
            "trades": 0,
            "current": None,
            "option_c": None,
            "details": [],
        }

    tp_trades = [t for t in trades if t.exit_reason == "take_profit"]
    sl_trades = [t for t in trades if t.exit_reason == "stop_loss"]
    mh_trades = [t for t in trades if t.exit_reason == "max_holding"]

    # ─── Option C simulation ────────────────────────────────────────────
    option_c_details = []
    for trade in trades:
        mfe_data = find_mfe_for_trade(trade, df_h1_filtered, m5, account.tp1_ratio, account.rr_scale_in)
        option_c_details.append(mfe_data)

    # ─── Monte Carlo for SL trades (if no M5 data or MFE unknown) ──────
    random.seed(42)
    sl_mc_results = []
    sl_with_mfe = 0
    sl_without_mfe = 0

    for trade in sl_trades:
        detail = option_c_details[trades.index(trade)]
        if detail.get("reached_tp1", False) or detail.get("reached_sl", False):
            # MFE data was available (from candles)
            sl_with_mfe += 1
            sl_mc_results.append(detail)
        else:
            sl_without_mfe += 1
            mc_info = monte_carlo_sl_trade(trade, account.tp1_ratio)
            sl_mc_results.append(mc_info)

    # ─── Aggregate results ───────────────────────────────────────────────
    # Current strategy
    current_pnl = sum(t.pnl for t in trades)
    current_wins = sum(1 for t in trades if t.pnl > 0)
    current_losses = sum(1 for t in trades if t.pnl <= 0)

    # Option C: deterministic trades (TP + max_holding)
    option_c_pnl_det = 0
    for trade in tp_trades + mh_trades:
        idx = trades.index(trade)
        detail = option_c_details[idx]
        option_c_pnl_det += detail.get("option_c_pnl", trade.pnl)

    # Option C: SL trades — use MFE data where available, Monte Carlo where not
    option_c_pnl_sl = 0
    for trade in sl_trades:
        idx = trades.index(trade)
        detail = option_c_details[idx]
        if detail.get("reached_tp1", False) or detail.get("reached_sl", False):
            # Deterministic MFE
            option_c_pnl_sl += detail.get("option_c_pnl", trade.pnl)
        else:
            # Monte Carlo estimate
            mc_info = monte_carlo_sl_trade(trade, account.tp1_ratio)
            tp1_prob = mc_info["tp1_prob"]
            tp1_distance = mc_info["tp1_distance"]
            sl_distance = mc_info["sl_distance"]
            remaining_distance = abs(trade.take_profit - (trade.entry_price + tp1_distance if trade.direction == SignalType.BUY else trade.entry_price - tp1_distance))
            new_sl_distance = remaining_distance / account.rr_scale_in
            lot_size = trade.lot_size
            contract_size = 100.0

            tp1_profit = tp1_distance * lot_size * contract_size
            scale_in_loss = new_sl_distance * lot_size * contract_size

            # Expected value: P(TP1 reached) * (tp1_profit - scale_in_loss) + P(not reached) * sl_loss
            if tp1_prob > 0:
                expected_option_c = tp1_prob * (tp1_profit - scale_in_loss) + (1 - tp1_prob) * trade.pnl
            else:
                expected_option_c = trade.pnl
            option_c_pnl_sl += expected_option_c

    option_c_pnl = option_c_pnl_det + option_c_pnl_sl

    # Option C wins estimate
    option_c_wins = sum(1 for t in tp_trades + mh_trades
                       if option_c_details[trades.index(t)].get("option_c_pnl", 0) > 0)
    # SL trades that would have been wins under Option C
    for trade in sl_trades:
        idx = trades.index(trade)
        detail = option_c_details[idx]
        if detail.get("option_c_pnl", trade.pnl) > 0:
            option_c_wins += 1
        elif not detail.get("reached_tp1", False) and not detail.get("reached_sl", False):
            mc_info = monte_carlo_sl_trade(trade, account.tp1_ratio)
            if mc_info["tp1_prob"] > 0.5:  # More likely than not reached TP1
                option_c_wins += 1

    # Scenario breakdown
    scenarios = {}
    for idx, trade in enumerate(trades):
        scenario = option_c_details[idx].get("scenario", "unknown")
        if scenario not in scenarios:
            scenarios[scenario] = {"count": 0, "current_pnl": 0, "option_c_pnl": 0}
        scenarios[scenario]["count"] += 1
        scenarios[scenario]["current_pnl"] += trade.pnl
        scenarios[scenario]["option_c_pnl"] += option_c_details[idx].get("option_c_pnl", trade.pnl)

    # TP1 reach rate for deterministic trades
    reached_tp1_count = sum(1 for d in option_c_details if d.get("reached_tp1", False))

    return {
        "account": account.name,
        "config": account,
        "trades": len(trades),
        "current": {
            "pnl": current_pnl,
            "pnl_pct": result.total_pnl_pct,
            "wins": current_wins,
            "losses": current_losses,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "max_dd": result.max_drawdown_pct,
            "sharpe": result.sharpe_ratio,
            "avg_pnl": result.avg_trade_pnl,
            "max_consec_wins": result.max_consecutive_wins,
            "max_consec_losses": result.max_consecutive_losses,
            "tp": len(tp_trades),
            "sl": len(sl_trades),
            "mh": len(mh_trades),
        },
        "option_c": {
            "pnl": option_c_pnl,
            "wins": option_c_wins,
            "pnl_diff": option_c_pnl - current_pnl,
        },
        "scenarios": scenarios,
        "reached_tp1_count": reached_tp1_count,
        "sl_with_mfe": sl_with_mfe,
        "sl_without_mfe": sl_without_mfe,
        "result": result,
    }


def print_account_results(label: str, data: dict):
    """Print formatted results for one account."""
    if data["current"] is None:
        print(f"\n  ⚠️  {label}: No trades generated")
        return

    c = data["current"]
    oc = data["option_c"]

    print(f"\n{'='*72}")
    print(f"  Account {label}: ATR={data['config'].atr_multiplier}, "
          f"RR={data['config'].risk_reward_ratio}, Conf={data['config'].min_confidence}")
    print(f"{'='*72}")

    print(f"\n  {'Metric':<30} {'Current':>14} {'Option C':>14} {'Diff':>14}")
    print(f"  {'-'*30} {'-'*14} {'-'*14} {'-'*14}")
    print(f"  {'Total trades':<30} {c['trades'] if 'trades' in c else data['trades']:>14} {'':>14} {'':>14}")
    print(f"  {'Total PnL':<30} {'${:>13,.2f}'.format(c['pnl']):>14} {'${:>13,.2f}'.format(oc['pnl']):>14} {'${:>13,.2f}'.format(oc['pnl_diff']):>14}")
    print(f"  {'PnL %':<30} {'{:>13.1f}%'.format(c['pnl_pct']):>14} {'':>14} {'':>14}")
    print(f"  {'Win Rate':<30} {'{:>13.1%}'.format(c['win_rate']):>14} {'':>14} {'':>14}")
    print(f"  {'Profit Factor':<30} {'{:>13.2f}'.format(c['profit_factor']):>14} {'':>14} {'':>14}")
    print(f"  {'Max Drawdown':<30} {'{:>13.1f}%'.format(c['max_dd']):>14} {'':>14} {'':>14}")
    print(f"  {'Sharpe Ratio':<30} {'{:>13.2f}'.format(c['sharpe']):>14} {'':>14} {'':>14}")
    print(f"  {'Avg PnL/trade':<30} {'${:>13.2f}'.format(c['avg_pnl']):>14} {'':>14} {'':>14}")
    print(f"  {'Max Consec Wins':<30} {c['max_consec_wins']:>14} {'':>14} {'':>14}")
    print(f"  {'Max Consec Losses':<30} {c['max_consec_losses']:>14} {'':>14} {'':>14}")
    print(f"  {'TP trades':<30} {c['tp']:>14} {'':>14} {'':>14}")
    print(f"  {'SL trades':<30} {c['sl']:>14} {'':>14} {'':>14}")
    print(f"  {'Max holding trades':<30} {c['mh']:>14} {'':>14} {'':>14}")

    # TP1 reach rate
    total_det = data["trades"] - data["sl_without_mfe"]
    if total_det > 0:
        tp1_rate = data["reached_tp1_count"] / total_det * 100
        print(f"  {'TP1 reach rate':<30} {'{:>13.1f}%'.format(tp1_rate):>14} {'':>14} {'':>14}")

    # Scenario breakdown
    print(f"\n  {'Scenario Breakdown':}")
    print(f"  {'Scenario':<25} {'Count':>7} {'%':>7} {'Current PnL':>14} {'Option C PnL':>14} {'Diff':>10}")
    print(f"  {'-'*25} {'-'*7} {'-'*7} {'-'*14} {'-'*14} {'-'*10}")
    total = data["trades"]
    for scenario, sdata in sorted(data["scenarios"].items(), key=lambda x: -x[1]["count"]):
        pct = sdata["count"] / total * 100 if total > 0 else 0
        diff = sdata["option_c_pnl"] - sdata["current_pnl"]
        print(f"  {scenario:<25} {sdata['count']:>7} {pct:>6.1f}% "
              f"{'${:>13,.2f}'.format(sdata['current_pnl']):>14} "
              f"{'${:>13,.2f}'.format(sdata['option_c_pnl']):>14} "
              f"{'${:>9,.2f}'.format(diff):>10}")


def save_trades_to_db(
    results: list[dict],
    df_h1: pd.DataFrame,
    df_d1: pd.DataFrame,
    m5: pd.DataFrame | None,
    db_path: Path,
) -> int:
    """Save backtest trades + computed features to DB for ML training.

    For each trade, computes features at entry point using multi-TF candle data,
    then saves as synthetic trade + trade_outcome with account config parameters.

    Returns number of trades saved.
    """
    init_db(db_path)
    ensure_synthetic_account(db_path)

    # Prepare D1 trend series for feature computation
    d1_trend_series = None
    if df_d1 is not None and len(df_d1) >= 200:
        ema50 = calculate_ema(df_d1["close"], 50)
        ema200 = calculate_ema(df_d1["close"], 200)
        d1_trend_series = pd.Series(index=df_d1.index, dtype=object)
        for i in range(len(df_d1)):
            if pd.notna(ema50.iloc[i]) and pd.notna(ema200.iloc[i]):
                d1_trend_series.iloc[i] = "bullish" if ema50.iloc[i] > ema200.iloc[i] else "bearish"
            else:
                d1_trend_series.iloc[i] = None
        d1_trend_series = d1_trend_series.dropna()

    # Build candle lookup dict for feature computation
    candle_data = {}
    if m5 is not None:
        candle_data["M5"] = m5.copy()
        candle_data["M5"].columns = [c.lower() for c in candle_data["M5"].columns]
    h1_lower = df_h1.copy()
    h1_lower.columns = [c.lower() for c in h1_lower.columns]
    candle_data["H1"] = h1_lower
    if df_d1 is not None:
        d1_lower = df_d1.copy()
        d1_lower.columns = [c.lower() for c in d1_lower.columns]
        candle_data["D1"] = d1_lower
    h4_data = load_timeframe("data/xau-data", "H4") if Path("data/xau-data").exists() else None
    if h4_data is not None:
        h4_lower = h4_data.copy()
        h4_lower.columns = [c.lower() for c in h4_lower.columns]
        candle_data["H4"] = h4_lower

    saved = 0
    for r in results:
        if r.get("current") is None:
            continue
        account: AccountConfig = r["config"]
        result: BacktestResult = r["result"]

        strategy_id = f"backtest_swing_{account.name.lower()}"

        for trade in result.trades:
            if trade.exit_idx is None or trade.entry_idx is None:
                continue

            # Get entry timestamp
            try:
                entry_ts = df_h1.index[trade.entry_idx]
                exit_ts = df_h1.index[trade.exit_idx]
            except (IndexError, KeyError):
                continue

            # Determine D1 trend at entry
            d1_trend = "neutral"
            if d1_trend_series is not None:
                valid = d1_trend_series[d1_trend_series.index <= entry_ts]
                if len(valid) > 0:
                    d1_trend = valid.iloc[-1]

            # Determine H4 trend at entry
            h4_trend = "unknown"
            if "H4" in candle_data and len(candle_data["H4"]) >= 200:
                h4_valid = candle_data["H4"][candle_data["H4"].index <= entry_ts].tail(500)
                if len(h4_valid) >= 200:
                    h4_ema50 = calculate_ema(h4_valid["close"], 50)
                    h4_ema200 = calculate_ema(h4_valid["close"], 200)
                    if pd.notna(h4_ema50.iloc[-1]) and pd.notna(h4_ema200.iloc[-1]):
                        h4_trend = "bullish" if h4_ema50.iloc[-1] > h4_ema200.iloc[-1] else "bearish"

            # Classify session
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

            # Slice candles for feature computation (no lookahead)
            entry_slices = {}
            for tf_name, df in candle_data.items():
                valid = df[df.index <= entry_ts].tail(500)
                if len(valid) >= 50:
                    entry_slices[tf_name] = valid

            # Compute features
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
                continue

            # Enrich features with regime + exit context
            enriched_features = {**features, "regime": r.get("scenarios", {})}
            enriched_features["d1_trend"] = d1_trend
            enriched_features["h4_trend"] = h4_trend
            enriched_features["atr_multiplier"] = account.atr_multiplier
            enriched_features["rr_ratio"] = account.risk_reward_ratio
            enriched_features["min_confidence_threshold"] = account.min_confidence

            # Determine exit context
            exit_d1_trend = d1_trend
            if d1_trend_series is not None:
                valid = d1_trend_series[d1_trend_series.index <= exit_ts]
                if len(valid) > 0:
                    exit_d1_trend = valid.iloc[-1]

            # Classify regime from features
            adx = features.get("adx", 20.0)
            boll_bw = features.get("boll_bw", 0.02)
            try:
                adx = float(adx)
                boll_bw = float(boll_bw)
            except (ValueError, TypeError):
                adx, boll_bw = 20.0, 0.02

            if adx >= 25 and boll_bw > 0.035:
                regime = "volatile"
            elif adx >= 25:
                regime = "trending"
            else:
                regime = "ranging"

            # Compute holding time
            bar_minutes = 60  # H1 candles
            holding_minutes = (trade.exit_idx - trade.entry_idx) * bar_minutes

            # Outcome label
            profit = trade.pnl
            profit_pct = trade.pnl_pct
            outcome_label = "WIN" if profit > 0 else ("LOSS" if profit < 0 else "BREAKEVEN")

            features_json = json.dumps(enriched_features, separators=(",", ":"))

            try:
                trade_id = insert_synthetic_trade(
                    direction=direction,
                    entry_price=trade.entry_price,
                    exit_price=trade.exit_price or 0.0,
                    pnl=profit,
                    pnl_pct=profit_pct,
                    d1_trend=d1_trend,
                    session=session,
                    regime=regime,
                    strategy_id=strategy_id,
                    entry_time=entry_ts.isoformat(),
                    exit_time=exit_ts.isoformat(),
                    exit_reason=trade.exit_reason,
                    trading_mode="backtest",
                    h4_trend=h4_trend,
                    atr_multiplier=account.atr_multiplier,
                    rr_ratio=account.risk_reward_ratio,
                    min_confidence_threshold=account.min_confidence,
                    db_path=db_path,
                )

                insert_synthetic_trade_outcome(
                    trade_id=trade_id,
                    direction=direction,
                    entry_price=trade.entry_price,
                    exit_price=trade.exit_price or 0.0,
                    profit=profit,
                    profit_pct=profit_pct,
                    outcome_label=outcome_label,
                    holding_minutes=holding_minutes,
                    exit_reason=trade.exit_reason,
                    features_json=features_json,
                    regime=regime,
                    d1_trend=d1_trend,
                    h4_trend=h4_trend,
                    session=session,
                    strategy_id=strategy_id,
                    trading_mode="backtest",
                    mfe=0.0,
                    mae=0.0,
                    mfe_pct=0.0,
                    mae_pct=0.0,
                    exit_regime=regime,
                    exit_d1_trend=exit_d1_trend,
                    exit_h4_trend=h4_trend,
                    atr_multiplier=account.atr_multiplier,
                    rr_ratio=account.risk_reward_ratio,
                    min_confidence_threshold=account.min_confidence,
                    db_path=db_path,
                )
                saved += 1
            except Exception as e:
                print(f"  ⚠️  Failed to save trade to DB: {e}")

    return saved


def main():
    parser = argparse.ArgumentParser(description="Backtest matching live paper trading config")
    parser.add_argument("--start", default="2024-01-01", help="Start date for backtest (default: 2024-01-01)")
    parser.add_argument("--equity", type=float, default=1000.0, help="Initial equity (default: 1000)")
    parser.add_argument("--risk", type=float, default=0.02, help="Risk per trade (default: 0.02)")
    parser.add_argument("--mc-runs", type=int, default=200, help="Monte Carlo runs for SL trades (default: 200)")
    parser.add_argument("--tp1-ratio", type=float, default=0.5, help="TP1 ratio of TP distance (default: 0.5)")
    parser.add_argument("--rr-scale-in", type=float, default=2.5, help="RR ratio for scale-in position (default: 2.5)")
    parser.add_argument("--no-m5", action="store_true", help="Skip M5 candle analysis (faster but less accurate)")
    parser.add_argument("--save-to-db", action="store_true", help="Save backtest trades to DB for ML training")
    parser.add_argument("--db", default="data/oracle.db", help="DB path when using --save-to-db (default: data/oracle.db)")
    args = parser.parse_args()

    print("=" * 72)
    print("  BACKTEST: Current Paper Trading Config (A/B/C) vs Option C")
    print(f"  Start: {args.start} | Equity: ${args.equity:,.0f} | Risk: {args.risk:.0%}")
    print(f"  TP1 ratio: {args.tp1_ratio} | RR scale-in: {args.rr_scale_in}")
    print("=" * 72)

    # ─── Load data ───────────────────────────────────────────────────────
    data_dir = "data/xau-data"
    print("\n📊 Loading data...")
    df_h1 = load_timeframe(data_dir, "H1")
    df_d1 = load_timeframe(data_dir, "D1")

    m5 = None
    if not args.no_m5:
        try:
            m5 = load_timeframe(data_dir, "M5")
            print(f"  H1: {len(df_h1)} candles ({df_h1.index[0]} → {df_h1.index[-1]})")
            print(f"  D1: {len(df_d1)} candles ({df_d1.index[0]} → {df_d1.index[-1]})")
            print(f"  M5: {len(m5)} candles ({m5.index[0]} → {m5.index[-1]})")
        except Exception as e:
            print(f"  ⚠️  M5 data unavailable: {e}")
            m5 = None

    if m5 is None:
        print(f"  H1: {len(df_h1)} candles ({df_h1.index[0]} → {df_h1.index[-1]})")
        print(f"  D1: {len(df_d1)} candles ({df_d1.index[0]} → {df_d1.index[-1]})")

    # ─── Run backtests per account ──────────────────────────────────────
    results = []
    for account in ACCOUNTS:
        # Override tp1_ratio and rr_scale_in from args
        account.tp1_ratio = args.tp1_ratio
        account.rr_scale_in = args.rr_scale_in

        data = run_account_backtest(
            account=account,
            df_h1=df_h1,
            df_d1=df_d1,
            m5=m5,
            start_date=args.start,
            initial_equity=args.equity,
            risk_per_trade=args.risk,
            mc_runs=args.mc_runs,
        )
        results.append(data)
        print_account_results(account.name, data)

    # ─── Combined summary ───────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  COMBINED SUMMARY (All Accounts)")
    print(f"{'='*72}")

    total_current_pnl = sum(r["current"]["pnl"] for r in results if r["current"])
    total_option_c_pnl = sum(r["option_c"]["pnl"] for r in results if r["current"])
    total_diff = total_option_c_pnl - total_current_pnl

    print(f"\n  {'Account':<12} {'Config':<25} {'Trades':>7} {'Current PnL':>14} {'Option C PnL':>14} {'Diff':>10}")
    print(f"  {'-'*12} {'-'*25} {'-'*7} {'-'*14} {'-'*14} {'-'*10}")

    for r in results:
        if r["current"] is None:
            continue
        cfg = f"ATR={r['config'].atr_multiplier} RR={r['config'].risk_reward_ratio} C={r['config'].min_confidence}"
        print(f"  {r['account']:<12} {cfg:<25} {r['trades']:>7} "
              f"{'${:>13,.2f}'.format(r['current']['pnl']):>14} "
              f"{'${:>13,.2f}'.format(r['option_c']['pnl']):>14} "
              f"{'${:>9,.2f}'.format(r['option_c']['pnl_diff']):>10}")

    print(f"  {'-'*12} {'-'*25} {'-'*7} {'-'*14} {'-'*14} {'-'*10}")
    total_trades = sum(r["trades"] for r in results if r["current"])
    print(f"  {'TOTAL':<12} {'':<25} {total_trades:>7} "
          f"{'${:>13,.2f}'.format(total_current_pnl):>14} "
          f"{'${:>13,.2f}'.format(total_option_c_pnl):>14} "
          f"{'${:>9,.2f}'.format(total_diff):>10}")

    # Verdict
    print(f"\n  🏆 Option C {'IMPROVES' if total_diff > 0 else 'WORSENS'} total PnL by "
          f"${abs(total_diff):,.2f} across {total_trades} trades")
    for r in results:
        if r["current"] is None:
            continue
        verdict = "✅ better" if r["option_c"]["pnl_diff"] > 0 else "❌ worse"
        print(f"     Account {r['account']}: {verdict} "
              f"(diff: ${r['option_c']['pnl_diff']:,.2f})")

    # ─── Save to DB for ML training ───────────────────────────────────────
    if args.save_to_db:
        db_path = Path(args.db)
        print(f"\n💾 Saving trades to DB for ML training: {db_path}")
        saved = save_trades_to_db(results, df_h1, df_d1, m5, db_path)
        print(f"  ✅ Saved {saved} trades to DB")
        print(f"  Each trade tagged with atr_multiplier, rr_ratio, min_confidence_threshold")
        print(f"  Use these params in ML training to compare across account configs")


if __name__ == "__main__":
    main()