#!/usr/bin/env python3
"""Backtest Option C (TP1 → Scale-In) vs Current Strategy (Full TP).

For each historical trade:
1. Find the trade's entry point in M5 candle data
2. Walk forward through candles to find MFE (max favorable excursion)
3. Check if price reached TP1 (50% of TP distance) before SL
4. Simulate Option C: close at TP1, reopen with new SL from final TP RR ratio
5. Compare PnL vs current full TP/SL strategy

For trades without candle data (most SL trades), uses Monte Carlo simulation
based on random walk probability of reaching TP1 before SL.

Usage:
    python scripts/backtest_partial_tp.py
    python scripts/backtest_partial_tp.py --tp1-ratio 0.5 --rr-new 2.5 --mc-runs 200
"""

import argparse
import glob
import random
import sqlite3
from pathlib import Path

import pandas as pd


def load_m5_data(data_dir: str = "data/xau-data") -> pd.DataFrame:
    """Load M5 candle data."""
    m5_files = glob.glob(f"{data_dir}/*M5*")
    if not m5_files:
        raise FileNotFoundError(f"No M5 data found in {data_dir}")

    df = pd.read_csv(m5_files[0], names=["Time", "Open", "High", "Low", "Close", "Volume"])
    df["Time"] = pd.to_datetime(df["Time"])
    df = df.set_index("Time").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df


def estimate_mfe_from_trade(trade: dict) -> dict:
    """Estimate MFE from trade outcome data (when candle data is unavailable).

    For TP trades: price definitely reached TP1 (it's between entry and TP).
    For max_holding trades: MFE ≈ max(PnL-derived distance, proportional estimate).
    For SL trades: CANNOT determine if TP1 was reached from PnL alone,
        since all SL trades have PnL = exactly the SL loss (ratio = 1.0).
        Uses probabilistic assignment via Monte Carlo in the main loop.
    """
    direction = trade["direction"]
    entry = trade["entry_price"]
    tp = trade["take_profit"]
    sl = trade["stop_loss"]
    pnl = trade.get("pnl") or 0
    exit_reason = trade.get("exit_reason", "")
    lot_size = trade.get("lot_size", 0.01)
    contract_size = 100

    if not tp or not sl or not entry:
        return {
            "mfe": 0, "mae": 0, "mfe_pct": 0, "mae_pct": 0,
            "reached_tp1": False, "reached_tp": False, "reached_sl": False,
            "tp1_price": 0, "source": "no_data",
        }

    tp_distance = abs(tp - entry)
    sl_distance = abs(sl - entry)
    tp1_price = entry + tp_distance * 0.5 if direction == "BUY" else entry - tp_distance * 0.5
    tp1_distance = abs(tp1_price - entry)

    if exit_reason == "take_profit":
        # TP hit → price definitely reached both TP1 and TP
        mfe = tp_distance
        mae = max(0, sl_distance * 0.3)  # Conservative MAE estimate
        return {
            "mfe": round(mfe, 2), "mae": round(mae, 2),
            "mfe_pct": round(mfe / entry * 100, 4), "mae_pct": round(mae / entry * 100, 4),
            "reached_tp1": True, "reached_tp": True, "reached_sl": False,
            "tp1_price": round(tp1_price, 2), "source": "tp_exit",
        }

    if exit_reason == "max_holding":
        # Max holding → trade ended by time, not by TP or SL
        # Use PnL to estimate how far price went
        if pnl > 0:
            # Positive PnL → price went in our direction
            pnl_distance = abs(pnl) / (lot_size * contract_size) if lot_size > 0 else 0
            mfe = max(pnl_distance, tp_distance * 0.5)  # At least TP1 if profitable
            reached_tp1 = mfe >= tp1_distance
            reached_tp = mfe >= tp_distance * 0.95  # Within 5% of TP
        else:
            # Negative PnL → price went against us
            pnl_distance = abs(pnl) / (lot_size * contract_size) if lot_size > 0 else sl_distance
            mfe = tp_distance * 0.3  # Some favorable movement before reversing
            reached_tp1 = False
            reached_tp = False

        mae = max(sl_distance * 0.5, pnl_distance if pnl < 0 else 0)
        return {
            "mfe": round(mfe, 2), "mae": round(mae, 2),
            "mfe_pct": round(mfe / entry * 100, 4), "mae_pct": round(mae / entry * 100, 4),
            "reached_tp1": reached_tp1, "reached_tp": reached_tp, "reached_sl": False,
            "tp1_price": round(tp1_price, 2), "source": "holding_exit",
        }

    # SL trades → cannot determine from PnL alone, needs Monte Carlo
    # Return raw data with probability calculation
    # P(reach TP1 before SL) for random walk ≈ sl_dist / (tp1_dist + sl_dist)
    tp1_prob = sl_distance / (tp1_distance + sl_distance) if (tp1_distance + sl_distance) > 0 else 0

    return {
        "mfe": 0,  # Unknown — will be simulated
        "mae": sl_distance,
        "mfe_pct": 0, "mae_pct": round(sl_distance / entry * 100, 4),
        "reached_tp1": False,  # Will be overridden by Monte Carlo
        "reached_tp": False,
        "reached_sl": True,
        "tp1_price": round(tp1_price, 2),
        "source": "sl_exit_mc",
        "tp1_prob": round(tp1_prob, 4),  # Probability of reaching TP1 before SL
    }


def load_trades(db_path: str = "data/oracle_vps.db") -> list[dict]:
    """Load closed trades with TP/SL from DB."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, direction, entry_price, take_profit, stop_loss,
               pnl, pnl_pct, exit_reason, regime, d1_trend, confidence,
               timestamp, exit_time, lot_size
        FROM live_trades
        WHERE is_open = 0
          AND pnl IS NOT NULL
          AND take_profit IS NOT NULL
          AND stop_loss IS NOT NULL
          AND entry_price IS NOT NULL
        ORDER BY id
    """)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def find_mfe_for_trade(
    trade: dict,
    m5: pd.DataFrame,
    max_bars: int = 500,
) -> dict:
    """Walk forward through M5 candles to find MFE/MAE and check if TP1 was reached."""
    direction = trade["direction"]
    entry = trade["entry_price"]
    tp = trade["take_profit"]
    sl = trade["stop_loss"]

    if not tp or not sl or not entry:
        return {"mfe": 0, "mae": 0, "mfe_pct": 0, "mae_pct": 0,
                "reached_tp1": False, "reached_tp": False,
                "bars_to_tp1": None, "bars_to_tp": None, "bars_to_sl": None}

    entry_ts = pd.Timestamp(trade["timestamp"])
    if entry_ts.tzinfo:
        entry_ts = entry_ts.tz_localize(None)

    try:
        future = m5[m5.index >= entry_ts].head(max_bars)
        if future.empty:
            future = m5[m5.index >= entry_ts.tz_localize(None)].head(max_bars)
    except Exception:
        return {"mfe": 0, "mae": 0, "mfe_pct": 0, "mae_pct": 0,
                "reached_tp1": False, "reached_tp": False,
                "bars_to_tp1": None, "bars_to_tp": None, "bars_to_sl": None}

    if future.empty:
        return {"mfe": 0, "mae": 0, "mfe_pct": 0, "mae_pct": 0,
                "reached_tp1": False, "reached_tp": False,
                "bars_to_tp1": None, "bars_to_tp": None, "bars_to_sl": None}

    tp_distance = abs(tp - entry)
    if direction == "BUY":
        tp1 = entry + tp_distance * 0.5
    else:
        tp1 = entry - tp_distance * 0.5

    mfe = 0.0
    mae = 0.0
    reached_tp1 = False
    reached_tp = False
    reached_sl = False
    bars_to_tp1 = None
    bars_to_tp = None
    bars_to_sl = None

    for i, (ts, row) in enumerate(future.iterrows()):
        high = float(row["High"])
        low = float(row["Low"])

        if direction == "BUY":
            favorable = high - entry
            adverse = entry - low
            if high >= tp1 and not reached_tp1:
                reached_tp1 = True
                bars_to_tp1 = i + 1
            if high >= tp and not reached_tp:
                reached_tp = True
                bars_to_tp = i + 1
            if low <= sl and not reached_sl:
                reached_sl = True
                bars_to_sl = i + 1
        else:
            favorable = entry - low
            adverse = high - entry
            if low <= tp1 and not reached_tp1:
                reached_tp1 = True
                bars_to_tp1 = i + 1
            if low <= tp and not reached_tp:
                reached_tp = True
                bars_to_tp = i + 1
            if high >= sl and not reached_sl:
                reached_sl = True
                bars_to_sl = i + 1

        mfe = max(mfe, favorable)
        mae = max(mae, adverse)

        if reached_tp and reached_sl:
            break
        if i >= max_bars:
            break

    entry_price = entry if entry > 0 else 1
    return {
        "mfe": round(mfe, 2), "mae": round(mae, 2),
        "mfe_pct": round(mfe / entry_price * 100, 4),
        "mae_pct": round(mae / entry_price * 100, 4),
        "reached_tp1": reached_tp1, "reached_tp": reached_tp,
        "bars_to_tp1": bars_to_tp1, "bars_to_tp": bars_to_tp,
        "bars_to_sl": bars_to_sl,
        "tp1_price": round(tp1, 2), "source": "candles",
    }


def simulate_option_c(
    trade: dict,
    mfe_data: dict,
    tp1_ratio: float = 0.5,
    rr_new: float = 2.5,
) -> dict:
    """Simulate Option C: TP1 → Scale-In strategy.

    Logic:
    1. If price reaches TP1 before SL: close at TP1 (partial win)
    2. Reopen position at TP1 price with new SL calculated from final TP RR ratio
    3. If price reaches final TP: full win on second position
    4. If price reverses to new SL: small loss on second position (proportional to RR)
    5. If price never reaches TP1: same as current (full SL loss)
    """
    direction = trade["direction"]
    entry = trade["entry_price"]
    tp = trade["take_profit"]
    sl = trade["stop_loss"]
    lot_size = trade.get("lot_size", 0.01)
    contract_size = 100

    tp_distance = abs(tp - entry)
    sl_distance = abs(sl - entry)
    if tp_distance == 0 or sl_distance == 0:
        return {"current_pnl": trade["pnl"], "option_c_pnl": trade["pnl"], "scenario": "no_data"}

    current_pnl = trade["pnl"]

    # TP1 price
    if direction == "BUY":
        tp1 = entry + tp_distance * tp1_ratio
    else:
        tp1 = entry - tp_distance * tp1_ratio

    # Scale-in SL: based on remaining distance and target RR
    remaining_tp_distance = abs(tp - tp1)
    if direction == "BUY":
        new_sl = tp1 - remaining_tp_distance / rr_new
    else:
        new_sl = tp1 + remaining_tp_distance / rr_new

    new_sl_distance = abs(tp1 - new_sl)

    if not mfe_data.get("reached_tp1", False):
        # Never reached TP1 → same as current strategy
        scenario = "never_tp1"
        option_c_pnl = current_pnl

    elif mfe_data.get("reached_tp", False) and not mfe_data.get("reached_sl", False):
        # Reached final TP without hitting SL
        tp1_profit = abs(tp1 - entry) * lot_size * contract_size
        scale_in_profit = remaining_tp_distance * lot_size * contract_size
        option_c_pnl = round(tp1_profit + scale_in_profit, 2)
        scenario = "tp1_then_tp"

    elif mfe_data.get("reached_tp1", False) and mfe_data.get("reached_sl", False):
        # Reached TP1, then hit SL
        # Position 1: win at TP1
        # Position 2: loss at new SL (proportional to remaining TP distance / RR)
        tp1_profit = abs(tp1 - entry) * lot_size * contract_size
        scale_in_loss = new_sl_distance * lot_size * contract_size
        option_c_pnl = round(tp1_profit - scale_in_loss, 2)
        scenario = "tp1_then_sl"

    elif mfe_data.get("reached_tp1", False) and not mfe_data.get("reached_tp", False) and not mfe_data.get("reached_sl", False):
        # Reached TP1, ended by max_holding
        tp1_profit = abs(tp1 - entry) * lot_size * contract_size
        if current_pnl > 0:
            remaining_pnl = current_pnl - tp1_profit
            option_c_pnl = round(tp1_profit + max(remaining_pnl, -scale_in_loss if 'scale_in_loss' in dir() else 0), 2)
        else:
            option_c_pnl = round(tp1_profit - new_sl_distance * lot_size * contract_size, 2)
        scenario = "tp1_then_holding"
    else:
        scenario = "unknown"
        option_c_pnl = current_pnl

    return {
        "current_pnl": current_pnl,
        "option_c_pnl": option_c_pnl,
        "scenario": scenario,
        "tp1_price": tp1,
        "new_sl": new_sl,
        "tp1_profit": abs(tp1 - entry) * lot_size * contract_size,
        "new_sl_loss": new_sl_distance * lot_size * contract_size,
    }


def monte_carlo_sl_trade(trade: dict, tp1_ratio: float = 0.5) -> dict:
    """Monte Carlo simulation for a single SL trade.

    Uses random walk probability: P(reach TP1 before SL) ≈ sl_dist / (tp1_dist + sl_dist).
    Then simulates Option C outcome based on whether TP1 was reached.
    """
    direction = trade["direction"]
    entry = trade["entry_price"]
    tp = trade["take_profit"]
    sl = trade["stop_loss"]
    lot_size = trade.get("lot_size", 0.01)
    contract_size = 100

    tp_distance = abs(tp - entry)
    sl_distance = abs(sl - entry)

    if direction == "BUY":
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
        "tp1_price": tp1,
        "current_pnl": trade["pnl"],
    }


def main():
    parser = argparse.ArgumentParser(description="Backtest Option C (TP1 → Scale-In)")
    parser.add_argument("--db", default="data/oracle_vps.db", help="Path to trade DB")
    parser.add_argument("--m5-dir", default="data/xau-data", help="Path to M5 candle data")
    parser.add_argument("--tp1-ratio", type=float, default=0.5, help="TP1 as ratio of TP distance")
    parser.add_argument("--rr-new", type=float, default=2.5, help="RR ratio for scale-in position")
    parser.add_argument("--mc-runs", type=int, default=200, help="Monte Carlo runs for SL trades")
    args = parser.parse_args()

    random.seed(42)  # Reproducible

    print("=" * 70)
    print("  BACKTEST: Option C (TP1 → Scale-In) vs Current Strategy")
    print(f"  TP1 ratio: {args.tp1_ratio} | Scale-in RR: {args.rr_new} | MC runs: {args.mc_runs}")
    print("=" * 70)

    # Load data
    m5 = None
    try:
        print("\nLoading M5 candle data...")
        m5 = load_m5_data(args.m5_dir)
        print(f"  Loaded {len(m5)} M5 candles ({m5.index[0]} to {m5.index[-1]})")
    except Exception as e:
        print(f"  No M5 data available: {e}")

    print("\nLoading trades from DB...")
    trades = load_trades(args.db)
    print(f"  Loaded {len(trades)} closed trades")

    # Categorize trades
    tp_trades = [t for t in trades if t["exit_reason"] == "take_profit"]
    sl_trades = [t for t in trades if t["exit_reason"] == "stop_loss"]
    mh_trades = [t for t in trades if t["exit_reason"] == "max_holding"]
    other_trades = [t for t in trades if t["exit_reason"] not in ("take_profit", "stop_loss", "max_holding")]

    print(f"\n  Exit reason distribution:")
    print(f"    take_profit:  {len(tp_trades):>5} trades")
    print(f"    stop_loss:    {len(sl_trades):>5} trades")
    print(f"    max_holding:  {len(mh_trades):>5} trades")
    print(f"    other:        {len(other_trades):>5} trades")

    # ====== Phase 1: Deterministic trades (TP + max_holding) ======
    deterministic_results = []
    candle_match = 0
    estimate_count = 0

    for trade in tp_trades + mh_trades:
        mfe_data = None
        if m5 is not None:
            mfe_data = find_mfe_for_trade(trade, m5, max_bars=500)
            if mfe_data["mfe"] > 0 or mfe_data["mae"] > 0:
                candle_match += 1
            else:
                mfe_data = None

        if mfe_data is None:
            mfe_data = estimate_mfe_from_trade(trade)
            estimate_count += 1

        if mfe_data["mfe"] == 0 and mfe_data["mae"] == 0:
            continue

        sim = simulate_option_c(trade, mfe_data, args.tp1_ratio, args.rr_new)
        sim["mfe"] = mfe_data["mfe"]
        sim["mfe_pct"] = mfe_data["mfe_pct"]
        sim["reached_tp1"] = mfe_data["reached_tp1"]
        sim["reached_tp"] = mfe_data["reached_tp"]
        sim["reached_sl"] = mfe_data.get("reached_sl", False)
        sim["source"] = mfe_data.get("source", "unknown")
        deterministic_results.append(sim)

    # ====== Phase 2: Monte Carlo for SL trades ======
    mc_all_results = []
    for run in range(args.mc_runs):
        run_results = []
        for trade in sl_trades:
            mc_info = monte_carlo_sl_trade(trade, args.tp1_ratio)
            tp1_prob = mc_info["tp1_prob"]

            # Randomly decide if this SL trade reached TP1 before SL
            reached_tp1 = random.random() < tp1_prob

            mfe_data = {
                "reached_tp1": reached_tp1,
                "reached_tp": False,  # SL trade, never reached final TP
                "reached_sl": True,
                "mfe": mc_info["tp1_distance"] if reached_tp1 else mc_info["sl_distance"] * 0.3,
                "mae": mc_info["sl_distance"],
                "source": "sl_mc",
            }

            sim = simulate_option_c(trade, mfe_data, args.tp1_ratio, args.rr_new)
            run_results.append(sim)

        mc_all_results.append(run_results)

    # ====== Aggregate results ======
    print(f"\n{'=' * 70}")
    print(f"  DETERMINISTIC RESULTS (TP + max_holding: {len(deterministic_results)} trades)")
    print(f"  Data: {candle_match} candle-based, {estimate_count} estimated")
    print(f"{'=' * 70}")

    # Deterministic summary
    det_scenarios = {}
    det_total_current = 0
    det_total_option_c = 0
    for r in deterministic_results:
        scenario = r["scenario"]
        if scenario not in det_scenarios:
            det_scenarios[scenario] = {"count": 0, "current_pnl": 0, "option_c_pnl": 0}
        det_scenarios[scenario]["count"] += 1
        det_scenarios[scenario]["current_pnl"] += r["current_pnl"] or 0
        det_scenarios[scenario]["option_c_pnl"] += r["option_c_pnl"] or 0
        det_total_current += r["current_pnl"] or 0
        det_total_option_c += r["option_c_pnl"] or 0

    det_count = len(deterministic_results)
    if det_count > 0:
        det_current_wins = sum(1 for r in deterministic_results if (r["current_pnl"] or 0) > 0)
        det_option_c_wins = sum(1 for r in deterministic_results if (r["option_c_pnl"] or 0) > 0)
        print(f"\n  {'Metric':<30} {'Current':>12} {'Option C':>12} {'Diff':>12}")
        print(f"  {'-'*30} {'-'*12} {'-'*12} {'-'*12}")
        print(f"  {'Total PnL':<30} {'${:>11,.2f}'.format(det_total_current)} {'${:>11,.2f}'.format(det_total_option_c)} {'${:>11,.2f}'.format(det_total_option_c - det_total_current)}")
        print(f"  {'Win Rate':<30} {'{:>11.1f}%'.format(det_current_wins/det_count*100)} {'{:>11.1f}%'.format(det_option_c_wins/det_count*100)} {'{:>+11.1f}%'.format((det_option_c_wins-det_current_wins)/det_count*100)}")
        print(f"  {'Avg PnL/trade':<30} {'${:>11.2f}'.format(det_total_current/det_count)} {'${:>11.2f}'.format(det_total_option_c/det_count)} {'${:>11.2f}'.format((det_total_option_c-det_total_current)/det_count)}")

        print(f"\n  {'Scenario':<25} {'Count':>7} {'%':>7} {'Current PnL':>14} {'Option C PnL':>14} {'Diff':>10}")
        print(f"  {'-'*25} {'-'*7} {'-'*7} {'-'*14} {'-'*14} {'-'*10}")
        for scenario, data in sorted(det_scenarios.items(), key=lambda x: -x[1]["count"]):
            pct = data["count"] / det_count * 100
            diff = data["option_c_pnl"] - data["current_pnl"]
            print(f"  {scenario:<25} {data['count']:>7} {pct:>6.1f}% {'${:>13,.2f}'.format(data['current_pnl'])} {'${:>13,.2f}'.format(data['option_c_pnl'])} {'${:>9,.2f}'.format(diff)}")

    # Monte Carlo summary for SL trades
    print(f"\n{'=' * 70}")
    print(f"  MONTE CARLO RESULTS (SL trades: {len(sl_trades)} trades × {args.mc_runs} runs)")
    print(f"{'=' * 70}")

    # Average TP1 probability across all SL trades
    sl_tp1_probs = [monte_carlo_sl_trade(t, args.tp1_ratio)["tp1_prob"] for t in sl_trades]
    avg_tp1_prob = sum(sl_tp1_probs) / len(sl_tp1_probs) if sl_tp1_probs else 0
    print(f"\n  Avg P(reach TP1 before SL): {avg_tp1_prob:.1%}")
    print(f"  Interpretation: {avg_tp1_prob:.0%} of SL trades likely reached TP1 before reversing to SL")

    # Aggregate MC results
    mc_total_current = sum(t["pnl"] for t in sl_trades if t.get("pnl"))
    mc_pnl_diffs = []
    mc_scenario_counts = {"never_tp1": 0, "tp1_then_sl": 0}

    for run_results in mc_all_results:
        run_option_c = sum(r["option_c_pnl"] for r in run_results if r["option_c_pnl"] is not None)
        mc_pnl_diffs.append(run_option_c - mc_total_current)
        for r in run_results:
            mc_scenario_counts[r["scenario"]] = mc_scenario_counts.get(r["scenario"], 0) + 1

    # Average scenario distribution across MC runs
    avg_never_tp1 = mc_scenario_counts.get("never_tp1", 0) / args.mc_runs
    avg_tp1_then_sl = mc_scenario_counts.get("tp1_then_sl", 0) / args.mc_runs

    mc_mean_diff = sum(mc_pnl_diffs) / len(mc_pnl_diffs) if mc_pnl_diffs else 0
    mc_min_diff = min(mc_pnl_diffs) if mc_pnl_diffs else 0
    mc_max_diff = max(mc_pnl_diffs) if mc_pnl_diffs else 0
    mc_p5 = sorted(mc_pnl_diffs)[int(len(mc_pnl_diffs) * 0.05)] if mc_pnl_diffs else 0
    mc_p95 = sorted(mc_pnl_diffs)[int(len(mc_pnl_diffs) * 0.95)] if mc_pnl_diffs else 0

    # Current SL total
    print(f"\n  SL trades (current strategy): ${mc_total_current:,.2f}")
    print(f"\n  Option C impact on SL trades (Monte Carlo, {args.mc_runs} runs):")
    print(f"  {'Metric':<30} {'Value':>15}")
    print(f"  {'-'*30} {'-'*15}")
    print(f"  {'Mean PnL diff':<30} {'${:>14,.2f}'.format(mc_mean_diff)}")
    print(f"  {'5th percentile':<30} {'${:>14,.2f}'.format(mc_p5)}")
    print(f"  {'95th percentile':<30} {'${:>14,.2f}'.format(mc_p95)}")
    print(f"  {'Best case':<30} {'${:>14,.2f}'.format(mc_max_diff)}")
    print(f"  {'Worst case':<30} {'${:>14,.2f}'.format(mc_min_diff)}")
    print(f"\n  Avg scenario split across MC runs:")
    print(f"    never_tp1 (same as current):    {avg_never_tp1:>5.1f} / {len(sl_trades)} trades")
    print(f"    tp1_then_sl (Option C wins TP1): {avg_tp1_then_sl:>5.1f} / {len(sl_trades)} trades")

    # ====== Combined results ======
    print(f"\n{'=' * 70}")
    print(f"  COMBINED RESULTS (All {len(trades)} trades)")
    print(f"{'=' * 70}")

    # Deterministic part
    combined_current = det_total_current + mc_total_current
    # MC part: use mean
    mean_sl_option_c = mc_total_current + mc_mean_diff
    combined_option_c_best = det_total_option_c + mean_sl_option_c

    # Use the mean MC result for the combined estimate
    combined_option_c = det_total_option_c + mean_sl_option_c

    total_wins_current = sum(1 for r in deterministic_results if (r["current_pnl"] or 0) > 0)
    total_wins_current += sum(1 for t in sl_trades if (t["pnl"] or 0) > 0)

    # For Option C wins, count deterministic + estimate for SL
    total_wins_option_c = sum(1 for r in deterministic_results if (r["option_c_pnl"] or 0) > 0)
    # SL trades that reached TP1 become wins (or partial wins) under Option C
    sl_wins_option_c = int(avg_tp1_prob * len(sl_trades))  # Approximate
    total_wins_option_c += sl_wins_option_c

    print(f"\n  {'Metric':<30} {'Current':>12} {'Option C':>12} {'Diff':>12}")
    print(f"  {'-'*30} {'-'*12} {'-'*12} {'-'*12}")
    print(f"  {'Total PnL':<30} {'${:>11,.2f}'.format(combined_current)} {'${:>11,.2f}'.format(combined_option_c)} {'${:>11,.2f}'.format(combined_option_c - combined_current)}")
    print(f"  {'PnL diff (mean MC)':<30} {'':>12} {'':>12} {'${:>11,.2f}'.format(mc_mean_diff)}")

    # Key insight
    tp1_reached_det = sum(1 for r in deterministic_results if r.get("reached_tp1"))
    tp1_not_tp = sum(1 for r in deterministic_results
                     if r.get("reached_tp1") and not r.get("reached_tp"))
    tp_and_tp1 = sum(1 for r in deterministic_results
                     if r.get("reached_tp1") and r.get("reached_tp"))
    never_tp1_det = sum(1 for r in deterministic_results if not r.get("reached_tp1"))

    print(f"\n  {'='*70}")
    print(f"  KEY INSIGHT: Partial TP value")
    print(f"  {'='*70}")
    print(f"  Deterministic trades ({len(deterministic_results)} trades):")
    print(f"    Reached TP1:            {tp1_reached_det:>5} ({tp1_reached_det/max(len(deterministic_results),1)*100:.1f}%)")
    print(f"    Reached both TP1 & TP:  {tp_and_tp1:>5} ({tp_and_tp1/max(len(deterministic_results),1)*100:.1f}%)")
    print(f"    Reached TP1, NOT TP:     {tp1_not_tp:>5} ({tp1_not_tp/max(len(deterministic_results),1)*100:.1f}%)")
    print(f"    Never reached TP1:       {never_tp1_det:>5} ({never_tp1_det/max(len(deterministic_results),1)*100:.1f}%)")
    print(f"\n  SL trades ({len(sl_trades)} trades):")
    print(f"    Avg P(reach TP1 before SL): {avg_tp1_prob:.1%}")
    print(f"    Estimated TP1 reachers:    ~{int(avg_tp1_prob * len(sl_trades))} / {len(sl_trades)}")
    print(f"    These trades: currently LOSE full SL")
    print(f"    Under Option C: win TP1 profit minus small scale-in loss")

    # Per-strategy breakdown
    print(f"\n  {'='*70}")
    print(f"  STRATEGY RECOMMENDATION BY TRADE TYPE")
    print(f"  {'='*70}")

    # Swing trades (higher RR, more likely to benefit from partial TP)
    swing_trades = [t for t in trades if t.get("regime") == "trending"]
    scalp_trades = [t for t in trades if t.get("regime") != "trending"]

    for label, subset in [("All trades", trades), ("Trending regime", swing_trades), ("Other regimes", scalp_trades)]:
        if not subset:
            continue
        tp_count = sum(1 for t in subset if t["exit_reason"] == "take_profit")
        sl_count = sum(1 for t in subset if t["exit_reason"] == "stop_loss")
        mh_count = sum(1 for t in subset if t["exit_reason"] == "max_holding")
        total_pnl = sum(t.get("pnl", 0) or 0 for t in subset)

        # Estimate average RR ratio
        rrs = []
        for t in subset:
            if t["take_profit"] and t["stop_loss"] and t["entry_price"]:
                tp_dist = abs(t["take_profit"] - t["entry_price"])
                sl_dist = abs(t["stop_loss"] - t["entry_price"])
                if sl_dist > 0:
                    rrs.append(tp_dist / sl_dist)
        avg_rr = sum(rrs) / len(rrs) if rrs else 0

        # Estimate TP1 probability for SL trades
        sl_subset = [t for t in subset if t["exit_reason"] == "stop_loss"]
        sl_tp1_probs = [monte_carlo_sl_trade(t, args.tp1_ratio)["tp1_prob"] for t in sl_subset]
        avg_sl_tp1_prob = sum(sl_tp1_probs) / len(sl_tp1_probs) if sl_tp1_probs else 0

        print(f"\n  {label} ({len(subset)} trades):")
        print(f"    TP: {tp_count} | SL: {sl_count} | MaxHold: {mh_count}")
        print(f"    Total PnL: ${total_pnl:,.2f}")
        print(f"    Avg RR: {avg_rr:.2f}")
        print(f"    SL trades P(TP1 before SL): {avg_sl_tp1_prob:.1%}")

    print(f"\n  Overall: Option C {'IMPROVES' if combined_option_c > combined_current else 'WORSENS'} PnL by ~${abs(combined_option_c - combined_current):,.2f}")
    print(f"  (Monte Carlo {args.mc_runs} runs, mean estimate for SL trades)")


if __name__ == "__main__":
    main()