#!/usr/bin/env python3
"""Compare 3 counter-trend modes using historical live trade data.

Mode 1 (legacy): trend_mult=0.5 - reduce confidence by half
Mode 2 (hard block): trend_mult=0.0 - block all counter-trend
Mode 3 (new): trend_mult=0.0 + Bollinger exception (0.3 at extremes)
"""

import sqlite3
import sys

DB_PATH = "data/oracle.db"

def main():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Get all closed trades with reason, regime, d1_trend, direction, pnl
    c.execute("""
    SELECT id, direction, regime, d1_trend, reason, pnl, confidence
    FROM live_trades
    WHERE is_open = 0 AND pnl IS NOT NULL AND regime IS NOT NULL AND d1_trend IS NOT NULL
    ORDER BY id
    """)
    trades = c.fetchall()
    print(f"Total trades with data: {len(trades)}")

    # Classify trades
    trend_following = []
    counter_trend = []

    for t in trades:
        tid, direction, regime, d1_trend, reason, pnl, confidence = t
        is_counter = "counter" in (reason or "").lower()
        is_override = "override" in (reason or "").lower()

        if is_counter or is_override:
            counter_trend.append(t)
        else:
            trend_following.append(t)

    # Mode 1: legacy (all trades included)
    mode1_pnl = sum(t[5] for t in trades)
    mode1_trades = len(trades)
    mode1_wins = sum(1 for t in trades if t[5] > 0)

    # Mode 2: hard block (remove all counter-trend)
    mode2_pnl = sum(t[5] for t in trend_following)
    mode2_trades = len(trend_following)
    mode2_wins = sum(1 for t in trend_following if t[5] > 0)

    # Mode 3: hard block + Bollinger exception
    # We don't have boll_position in DB, so estimate:
    # Counter-trend trades that had high confidence might be at extremes
    # Conservative estimate: allow ~20% of counter-trend trades back
    # (those with boll_pos info in reason or high confidence)
    ct_allowed_by_boll = []
    ct_blocked_by_boll = []

    for t in counter_trend:
        tid, direction, regime, d1_trend, reason, pnl, confidence = t
        # Check if reason has boll_pos info (ranging signals have it)
        # Or if confidence is high (>= 0.6) — proxy for strong signal at extreme
        reason_str = reason or ""
        has_boll_info = "boll_pos" in reason_str or "boll" in reason_str

        # For Bollinger exception: we'd need actual boll_pos
        # Since we don't have it, check if it's a ranging signal
        # (ranging signals use Bollinger mean-reversion, so they're at band extremes)
        is_ranging_counter = regime == "ranging" and ("counter" in reason_str.lower() or "override" in reason_str.lower())

        if has_boll_info or is_ranging_counter:
            ct_allowed_by_boll.append(t)
        else:
            ct_blocked_by_boll.append(t)

    # Mode 3 PnL: trend_following + boll-allowed counter-trend (with 0.3 confidence scaling)
    # Since trend_mult=0.3, these trades would have smaller lots
    # Conservative: count them at 30% of their actual PnL (proportional to lot size)
    mode3_ct_pnl = sum(t[5] * 0.3 for t in ct_allowed_by_boll)  # Scaled down
    mode3_pnl = mode2_pnl + mode3_ct_pnl
    mode3_trades = mode2_trades + len(ct_allowed_by_boll)  # Trades still happen, just smaller
    mode3_wins = mode2_wins + sum(1 for t in ct_allowed_by_boll if t[5] > 0)

    print(f"\n{'='*60}")
    print(f"  COMPARISON OF 3 COUNTER-TREND MODES")
    print(f"{'='*60}")
    print(f"  {'Mode':<35} {'Trades':>7} {'WR':>7} {'PnL':>10} {'Avg':>8}")
    print(f"  {'-'*35} {'-'*7} {'-'*7} {'-'*10} {'-'*8}")
    for label, trades_n, wins, pnl in [
        ("Mode 1: legacy (trend_mult=0.5)", mode1_trades, mode1_wins, mode1_pnl),
        ("Mode 2: hard block (trend_mult=0.0)", mode2_trades, mode2_wins, mode2_pnl),
        ("Mode 3: block + boll exception", mode3_trades, mode3_wins, mode3_pnl),
    ]:
        wr = wins / trades_n * 100 if trades_n > 0 else 0
        avg = pnl / trades_n if trades_n > 0 else 0
        print(f"  {label:<35} {trades_n:>7} {wr:>6.1f}% ${pnl:>9.2f} ${avg:>7.2f}")

    print(f"\n  Difference (Mode 2 - Mode 1): ${mode2_pnl - mode1_pnl:>9.2f}")
    print(f"  Difference (Mode 3 - Mode 1): ${mode3_pnl - mode1_pnl:>9.2f}")
    print(f"  Difference (Mode 3 - Mode 2): ${mode3_pnl - mode2_pnl:>9.2f}")

    # Counter-trend detail
    ct_wins = sum(1 for t in counter_trend if t[5] > 0)
    ct_pnl = sum(t[5] for t in counter_trend)
    print(f"\n{'='*60}")
    print(f"  COUNTER-TREND TRADES DETAIL")
    print(f"{'='*60}")
    print(f"  Total counter-trend trades: {len(counter_trend)}")
    print(f"  WR: {ct_wins/len(counter_trend)*100:.1f}%")
    print(f"  PnL: ${ct_pnl:.2f}")
    print(f"  Avg PnL per trade: ${ct_pnl/len(counter_trend):.2f}")

    print(f"\n  Breakdown by direction:")
    for d in ["BUY", "SELL"]:
        ct_d = [t for t in counter_trend if t[1] == d]
        if ct_d:
            d_wins = sum(1 for t in ct_d if t[5] > 0)
            d_pnl = sum(t[5] for t in ct_d)
            print(f"    {d}: {len(ct_d)} trades, WR={d_wins/len(ct_d)*100:.1f}%, PnL=${d_pnl:.2f}")

    print(f"\n  Breakdown by regime:")
    for r in ["trending", "ranging", "volatile"]:
        ct_r = [t for t in counter_trend if t[2] == r]
        if ct_r:
            r_wins = sum(1 for t in ct_r if t[5] > 0)
            r_pnl = sum(t[5] for t in ct_r)
            print(f"    {r}: {len(ct_r)} trades, WR={r_wins/len(ct_r)*100:.1f}%, PnL=${r_pnl:.2f}")

    # Bollinger exception estimate
    print(f"\n  Bollinger exception estimate:")
    print(f"    Counter-trend at extremes (allowed back): {len(ct_allowed_by_boll)}")
    print(f"    Counter-trend blocked: {len(ct_blocked_by_boll)}")
    if ct_allowed_by_boll:
        boll_pnl = sum(t[5] for t in ct_allowed_by_boll)
        boll_wins = sum(1 for t in ct_allowed_by_boll if t[5] > 0)
        print(f"    Allowed trades PnL (full): ${boll_pnl:.2f}, WR={boll_wins/len(ct_allowed_by_boll)*100:.1f}%")
        print(f"    Allowed trades PnL (0.3x): ${boll_pnl * 0.3:.2f}")

    conn.close()

if __name__ == "__main__":
    main()