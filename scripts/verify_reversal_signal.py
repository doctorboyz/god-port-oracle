#!/usr/bin/env python3
"""Verify has_reversal_signal accuracy on historical live trades.

Computes reversal signal for each historical trade using stored indicator data,
then checks if reversal trades (has_reversal=True) performed better than
plain counter-trend trades (has_reversal=False).

Usage:
    python scripts/verify_reversal_signal.py [--db data/oracle.db]
"""

import json
import sqlite3
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from broky.signals.generator import (
    compute_reversal_signal,
    compute_trend_alignment_value,
    REVERSAL_OB_RSI, REVERSAL_OS_RSI,
    REVERSAL_OB_STOCH, REVERSAL_OS_STOCH,
    REVERSAL_OB_BOLL, REVERSAL_OS_BOLL,
    REVERSAL_OB_MFI, REVERSAL_OS_MFI,
)


def get_trades(db_path: str) -> list[dict]:
    """Fetch closed trades with indicator data from DB."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT
            t.id, t.direction, t.pnl, t.pnl_pct, t.exit_reason,
            t.regime, t.d1_trend, t.confidence,
            t.indicator_scores_json,
            o.features_json,
            t.tp_level, t.strategy_id, t.trading_mode
        FROM live_trades t
        LEFT JOIN trade_outcomes o ON o.trade_id = t.id
        WHERE t.is_open = 0 AND t.pnl IS NOT NULL
        ORDER BY t.exit_time ASC
    """)

    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def extract_indicators(trade: dict) -> dict:
    """Extract raw indicator values from trade data for reversal computation.

    Tries features_json first (richer data), falls back to indicator_scores_json.
    """
    indicators = {}

    # Parse features_json (from trade_outcomes)
    features = {}
    if trade.get("features_json"):
        try:
            features = json.loads(trade["features_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    # Parse indicator_scores_json (from live_trades)
    scores = {}
    if trade.get("indicator_scores_json"):
        try:
            scores = json.loads(trade["indicator_scores_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    # Merge: features take precedence over scores (richer data)
    merged = {**scores, **features}

    # Extract values needed for reversal signal
    indicators["rsi"] = _float(merged.get("rsi"))
    indicators["stoch_k"] = _float(merged.get("stoch_k"))
    indicators["boll_pct_b"] = _float(merged.get("boll_pct_b"))
    indicators["mfi"] = _float(merged.get("mfi"))
    indicators["macd_hist"] = _float(merged.get("macd_hist"))
    indicators["plus_di"] = _float(merged.get("plus_di"))
    indicators["minus_di"] = _float(merged.get("minus_di"))
    indicators["boll_bw"] = _float(merged.get("boll_bw"))

    # Trend context
    indicators["d1_trend"] = trade.get("d1_trend") or merged.get("d1_trend")
    indicators["h4_trend"] = merged.get("h4_trend")

    return indicators


def _float(val) -> float | None:
    """Convert value to float, return None if not possible."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def classify_trade(trade: dict) -> str:
    """Classify a trade as trend-aligned, reversal, counter-trend, or neutral."""
    direction = trade.get("direction", "")
    d1_trend = trade.get("d1_trend")

    if not d1_trend or d1_trend == "unknown":
        return "neutral"

    if d1_trend == "bullish" and direction == "BUY":
        return "trend_aligned"
    if d1_trend == "bearish" and direction == "SELL":
        return "trend_aligned"
    # Counter-trend — need to check if reversal
    return "counter_trend"  # Will be refined by has_reversal


def main():
    db_path = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--db" else "data/oracle.db"
    db_path = Path(db_path)
    if not db_path.exists():
        print(f"DB not found: {db_path}")
        sys.exit(1)

    trades = get_trades(str(db_path))
    print(f"\n{'='*80}")
    print(f"REVERSAL SIGNAL VERIFICATION — {len(trades)} trades from {db_path.name}")
    print(f"{'='*80}")
    print(f"\nThresholds: RSI OB>{REVERSAL_OB_RSI}/OS<{REVERSAL_OS_RSI}, "
          f"Stoch OB>{REVERSAL_OB_STOCH}/OS<{REVERSAL_OS_STOCH}, "
          f"Boll OB>={REVERSAL_OB_BOLL}/OS<={REVERSAL_OS_BOLL}, "
          f"MFI OB>{REVERSAL_OB_MFI}/OS<{REVERSAL_OS_MFI}")

    # ── Categorize trades ──
    categories = {
        "trend_aligned": {"trades": [], "wins": 0, "pnl": 0.0},
        "reversal": {"trades": [], "wins": 0, "pnl": 0.0},
        "counter_trend": {"trades": [], "wins": 0, "pnl": 0.0},
        "neutral": {"trades": [], "wins": 0, "pnl": 0.0},
    }

    missing_indicators = 0
    has_reversal_count = 0
    has_counter_no_reversal = 0

    for trade in trades:
        direction = trade.get("direction", "")
        d1_trend = trade.get("d1_trend")
        pnl = trade.get("pnl", 0) or 0
        is_win = pnl > 0

        # Extract indicators
        ind = extract_indicators(trade)

        # Compute reversal signal
        has_reversal, reversal_strength = compute_reversal_signal(
            direction=direction,
            d1_trend=d1_trend,
            h4_trend=ind.get("h4_trend"),
            rsi=ind.get("rsi"),
            stoch_k=ind.get("stoch_k"),
            boll_pct_b=ind.get("boll_pct_b"),
            mfi=ind.get("mfi"),
            macd_hist=ind.get("macd_hist"),
            plus_di=ind.get("plus_di"),
            minus_di=ind.get("minus_di"),
            boll_bw=ind.get("boll_bw"),
        )

        trend_alignment = compute_trend_alignment_value(
            direction=direction,
            d1_trend=d1_trend,
            h4_trend=ind.get("h4_trend"),
            has_reversal=has_reversal,
        )

        # Track missing data
        has_data = any(v is not None for v in [ind.get("rsi"), ind.get("stoch_k"), ind.get("boll_pct_b")])
        if not has_data:
            missing_indicators += 1

        # Categorize
        if not d1_trend or d1_trend == "unknown":
            cat = "neutral"
        elif (d1_trend == "bullish" and direction == "BUY") or (d1_trend == "bearish" and direction == "SELL"):
            cat = "trend_aligned"
        elif has_reversal:
            cat = "reversal"
            has_reversal_count += 1
        else:
            cat = "counter_trend"
            has_counter_no_reversal += 1

        categories[cat]["trades"].append(trade)
        categories[cat]["pnl"] += pnl
        if is_win:
            categories[cat]["wins"] += 1

    # ── Print results ──
    print(f"\n{'─'*80}")
    print(f"{'Category':<20} {'Trades':>8} {'Wins':>8} {'WR%':>8} {'PnL':>12} {'Avg PnL':>10}")
    print(f"{'─'*80}")

    total_trades = 0
    total_wins = 0
    total_pnl = 0.0

    for cat in ["trend_aligned", "reversal", "counter_trend", "neutral"]:
        c = categories[cat]
        n = len(c["trades"])
        wr = (c["wins"] / n * 100) if n > 0 else 0
        avg = (c["pnl"] / n) if n > 0 else 0
        print(f"{cat:<20} {n:>8} {c['wins']:>8} {wr:>7.1f}% ${c['pnl']:>10.2f} ${avg:>8.2f}")
        total_trades += n
        total_wins += c["wins"]
        total_pnl += c["pnl"]

    print(f"{'─'*80}")
    total_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
    total_avg = (total_pnl / total_trades) if total_trades > 0 else 0
    print(f"{'TOTAL':<20} {total_trades:>8} {total_wins:>8} {total_wr:>7.1f}% ${total_pnl:>10.2f} ${total_avg:>8.2f}")
    print(f"{'─'*80}")

    # ── Reversal vs Counter-trend comparison ──
    print(f"\n{'='*80}")
    print("REVERSAL vs COUNTER-TREND ANALYSIS")
    print(f"{'='*80}")

    rev = categories["reversal"]
    ct = categories["counter_trend"]

    if rev["trades"]:
        rev_n = len(rev["trades"])
        rev_wr = (rev["wins"] / rev_n * 100) if rev_n > 0 else 0
        rev_avg = (rev["pnl"] / rev_n) if rev_n > 0 else 0
        print(f"\n  Reversal trades:     {rev_n} trades, WR={rev_wr:.1f}%, Avg=${rev_avg:.2f}, PnL=${rev['pnl']:.2f}")
    else:
        print(f"\n  Reversal trades:     0 trades (no counter-trend trades had OB/OS + divergence)")

    if ct["trades"]:
        ct_n = len(ct["trades"])
        ct_wr = (ct["wins"] / ct_n * 100) if ct_n > 0 else 0
        ct_avg = (ct["pnl"] / ct_n) if ct_n > 0 else 0
        print(f"  Counter-trend trades: {ct_n} trades, WR={ct_wr:.1f}%, Avg=${ct_avg:.2f}, PnL=${ct['pnl']:.2f}")
    else:
        print(f"  Counter-trend trades: 0 trades (all counter-trend had reversal evidence)")

    if rev["trades"] and ct["trades"]:
        print(f"\n  ✅ Reversal WR {rev_wr:.1f}% vs Counter-trend WR {ct_wr:.1f}% → "
              f"{'Reversal BETTER' if rev_wr > ct_wr else 'Counter-trend BETTER (unexpected!)'}")
        print(f"  ✅ Reversal Avg ${rev_avg:.2f} vs Counter-trend Avg ${ct_avg:.2f} → "
              f"{'Reversal MORE PROFITABLE' if rev_avg > ct_avg else 'Counter-trend MORE PROFITABLE (unexpected!)'}")

    # ── Direction breakdown ──
    print(f"\n{'='*80}")
    print("DIRECTION BREAKDOWN")
    print(f"{'='*80}")

    for direction in ["BUY", "SELL"]:
        for d1_trend in ["bullish", "bearish"]:
            subset = [t for t in trades
                      if t.get("direction") == direction and t.get("d1_trend") == d1_trend]
            if not subset:
                continue

            # Compute reversal for each
            rev_trades = []
            ct_trades = []
            for t in subset:
                ind = extract_indicators(t)
                has_rev, _ = compute_reversal_signal(
                    direction=direction, d1_trend=d1_trend,
                    h4_trend=ind.get("h4_trend"),
                    rsi=ind.get("rsi"), stoch_k=ind.get("stoch_k"),
                    boll_pct_b=ind.get("boll_pct_b"), mfi=ind.get("mfi"),
                    macd_hist=ind.get("macd_hist"),
                    plus_di=ind.get("plus_di"), minus_di=ind.get("minus_di"),
                    boll_bw=ind.get("boll_bw"),
                )
                if has_rev:
                    rev_trades.append(t)
                else:
                    ct_trades.append(t)

            is_counter = (direction == "SELL" and d1_trend == "bullish") or (direction == direction == "BUY" and d1_trend == "bearish")
            label = f"{direction} in {d1_trend} D1"
            total = len(subset)
            total_pnl_d = sum(t.get("pnl", 0) or 0 for t in subset)
            total_wins_d = sum(1 for t in subset if (t.get("pnl", 0) or 0) > 0)
            wr_d = (total_wins_d / total * 100) if total > 0 else 0

            print(f"\n  {label} ({'COUNTER-TREND' if is_counter else 'TREND-ALIGNED'}): "
                  f"{total} trades, WR={wr_d:.1f}%, PnL=${total_pnl_d:.2f}")

            if is_counter and (rev_trades or ct_trades):
                rev_n = len(rev_trades)
                ct_n = len(ct_trades)
                rev_pnl = sum(t.get("pnl", 0) or 0 for t in rev_trades)
                ct_pnl = sum(t.get("pnl", 0) or 0 for t in ct_trades)
                rev_wr = (sum(1 for t in rev_trades if (t.get("pnl", 0) or 0) > 0) / rev_n * 100) if rev_n > 0 else 0
                ct_wr = (sum(1 for t in ct_trades if (t.get("pnl", 0) or 0) > 0) / ct_n * 100) if ct_n > 0 else 0
                print(f"    Reversal:      {rev_n} trades, WR={rev_wr:.1f}%, PnL=${rev_pnl:.2f}")
                print(f"    No reversal:   {ct_n} trades, WR={ct_wr:.1f}%, PnL=${ct_pnl:.2f}")

    # ── Missing data ──
    print(f"\n{'='*80}")
    print(f"DATA QUALITY")
    print(f"{'='*80}")
    print(f"  Trades with missing indicator data: {missing_indicators}/{len(trades)} "
          f"({missing_indicators/len(trades)*100:.1f}%)" if len(trades) > 0 else "")

    print(f"\n{'='*80}")
    print("CONCLUSION")
    print(f"{'='*80}")
    if categories["reversal"]["trades"] and categories["counter_trend"]["trades"]:
        rev_wr = categories["reversal"]["wins"] / len(categories["reversal"]["trades"]) * 100
        ct_wr = categories["counter_trend"]["wins"] / len(categories["counter_trend"]["trades"]) * 100
        if rev_wr > ct_wr:
            print(f"  ✅ has_reversal_signal is ACCURATE: reversal trades (WR={rev_wr:.1f}%) "
                  f"outperform plain counter-trend (WR={ct_wr:.1f}%)")
        else:
            print(f"  ⚠️  has_reversal_signal needs TUNING: counter-trend without reversal (WR={ct_wr:.1f}%) "
                  f"outperforms reversal (WR={rev_wr:.1f}%)")
    elif not categories["reversal"]["trades"]:
        print(f"  ⚠️  No reversal trades found — thresholds may be too strict, or data lacks counter-trend trades with OB/OS")
    else:
        print(f"  ✅ All counter-trend trades have reversal evidence — no bad counter-trend trades in data")


if __name__ == "__main__":
    main()