#!/usr/bin/env python3
"""Backfill missing PnL for Account A trades using MT5 deal history.

Targets trades with exit_reason='closed_by_mt5_inferred' AND pnl=0 — these
are orphaned reconciliations where the bridge couldn't fetch deals (No IPC
connection window). Now that deals are fetchable, we can fill the real PnL.

IRON LAW note: this UPDATEs Real-A data (oracle.db) but only to COMPLETE
missing PnL — it does not change entries, directions, or anything that
would alter trading history. Backup is taken first. Dry-run by default;
pass --apply to commit changes.

Usage (inside oracle-engine container):
  python3 scripts/backfill_a_pnl_from_deals.py            # dry-run
  python3 scripts/backfill_a_pnl_from_deals.py --apply    # commit
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, "/app")

ACCOUNT_ID = 1  # Account A in oracle.db
CONTRACT_SIZE = 100.0  # XAUUSD: 1 lot = 100 oz


def map_exit_reason(comment: str, deal_reason: int) -> str:
    """Map MT5 deal comment/reason to our exit_reason vocabulary."""
    c = (comment or "").lower()
    if "[sl" in c or "sl " in c:
        return "stop_loss"
    if "[tp" in c or "tp " in c:
        return "take_profit"
    # MT5 deal reason codes
    # 0=CLIENT(manual), 1=EXPERT(EA), 2=SL, 3=TP, 4=SO(stopout),
    # 5=ROLLOVER, 6=VMARGIN, 7=SPLIT
    if deal_reason == 0:
        return "manual_close"
    if deal_reason == 1:
        return "auto_close_ea"
    if deal_reason == 4:
        return "stop_out"
    if deal_reason == 5:
        return "rollover_close"
    if deal_reason == 6:
        return "variation_margin"
    return "closed_by_mt5"


async def fetch_deals(days_back: int):
    from metty.core.account_registry import get_bridge_config
    from metty.bridge.client import MT5Bridge
    cfg = get_bridge_config("A")
    bridge = MT5Bridge(cfg)
    await bridge.connect()
    try:
        # days_back must cover the oldest trade being backfilled — the 3
        # corrupted trades of 2026-07-07/08 (ISSUE-080) are ~78 days old,
        # far beyond the old hard-coded 35.
        deals = await bridge.get_deal_history("XAUUSDm", days_back=days_back)
        return deals
    finally:
        await bridge.disconnect()


def pair_entries_closes(deals):
    """Pair opening deals (profit=0) to closing deals (profit!=0).

    MT5 returns deals in time order. Each position has an opening deal
    (profit=0) and a closing deal (profit!=0). We pair them by walking
    through deals chronologically: each opening deal is matched to the
    next closing deal of opposite type.

    Returns list of (entry_deal, close_deal) tuples.
    """
    sorted_deals = sorted(deals, key=lambda d: d.get("time", 0))
    pairs = []
    open_stack = []  # FIFO of unmatched opening deals
    for d in sorted_deals:
        t = d.get("type")
        if t not in (0, 1):
            continue
        profit = d.get("profit", 0) or 0
        if profit == 0:
            # Opening deal
            open_stack.append(d)
        else:
            # Closing deal — pair with earliest unmatched opening of opposite type
            closing_type = t  # 0=BUY closing deal closes a SELL; 1=SELL closes a BUY
            # Position direction = opposite of closing deal type
            position_direction = "BUY" if closing_type == 1 else "SELL"
            # Match to earliest opening deal with matching direction
            for i, od in enumerate(open_stack):
                open_type = od.get("type")
                open_dir = "BUY" if open_type == 0 else "SELL"
                if open_dir == position_direction:
                    pairs.append((od, d))
                    open_stack.pop(i)
                    break
    return pairs


def match_pair_to_db_trade(pair, db_trades):
    """Match an (entry_deal, close_deal) pair to a DB trade.

    Matching criteria (all must hold):
      - DB trade is_open=0 (already reconciled) AND pnl=0 AND exit_reason contains 'inferred'
      - DB direction matches entry deal direction
      - DB entry_price within 0.3% of entry deal price
      - DB timestamp within 5 min of entry deal time
    """
    entry_deal, close_deal = pair
    entry_price = float(entry_deal.get("price", 0))
    entry_time = float(entry_deal.get("time", 0))
    if entry_time > 1e12:
        entry_time /= 1000
    direction = "BUY" if entry_deal.get("type") == 0 else "SELL"

    candidates = []
    for t in db_trades:
        if t["direction"] != direction:
            continue
        ep = t["entry_price"]
        if abs(ep - entry_price) / ep > 0.003:
            continue
        # Parse DB timestamp
        try:
            db_ts = datetime.fromisoformat(t["timestamp"]).timestamp()
        except Exception:
            continue
        if abs(db_ts - entry_time) > 300:  # 5 min
            continue
        candidates.append((abs(db_ts - entry_time), t))

    if not candidates:
        return None
    # Closest by time
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="Commit changes (default: dry-run)")
    parser.add_argument("--days-back", type=int, default=35,
                        help="Deal history window in days (default 35; must "
                             "cover the oldest trade being backfilled)")
    args = parser.parse_args()

    print(f"=== Backfill Account A PnL from MT5 deals ({'APPLY' if args.apply else 'DRY-RUN'}) ===")

    # 1. Fetch deals
    print(f"Fetching deals from MT5 bridge (days_back={args.days_back})...")
    deals = await fetch_deals(args.days_back)
    print(f"  Got {len(deals)} deals")

    # 2. Pair entries to closes
    pairs = pair_entries_closes(deals)
    print(f"  Paired {len(pairs)} (entry, close) deal pairs")

    # 3. Load DB trades with missing PnL
    conn = sqlite3.connect("/app/data/oracle.db")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT id, timestamp, direction, entry_price, stop_loss, take_profit,
               ticket, exit_reason, pnl, is_open, lot_size
        FROM live_trades
        WHERE account_id = ?
          AND pnl = 0
          AND (exit_reason LIKE '%inferred%' OR exit_reason = 'closed_by_mt5')
        ORDER BY timestamp
    """, (ACCOUNT_ID,))
    db_trades = [dict(r) for r in cur.fetchall()]
    print(f"  DB trades with missing PnL: {len(db_trades)}")

    # 4. Match each pair to a DB trade
    matched_pairs = []
    used_trade_ids = set()
    for pair in pairs:
        t = match_pair_to_db_trade(pair, db_trades)
        if t and t["id"] not in used_trade_ids:
            matched_pairs.append((pair, t))
            used_trade_ids.add(t["id"])

    print(f"  Matched: {len(matched_pairs)}/{len(db_trades)} DB trades")
    print(f"  Unmatched DB trades: {len(db_trades) - len(matched_pairs)}")

    # 5. Show sample matches + reason distribution
    print("\n=== SAMPLE MATCHES (first 5) ===")
    for (entry, close), t in matched_pairs[:5]:
        ct = float(close.get("time", 0))
        if ct > 1e12: ct /= 1000
        close_time = datetime.fromtimestamp(ct, tz=timezone.utc).isoformat()
        er = map_exit_reason(close.get("comment", ""), close.get("reason", -1))
        print(f"  DB trade #{t['id']} {t['direction']} entry={t['entry_price']} "
              f"ts={t['timestamp']}")
        print(f"    → close price={close.get('price')} profit={close.get('profit')} "
              f"reason={er} comment={close.get('comment','')[:30]} at={close_time}")

    # 6. Reason distribution
    print("\n=== EXIT REASON DISTRIBUTION (matched) ===")
    reason_count = defaultdict(int)
    reason_pnl = defaultdict(float)
    for (entry, close), t in matched_pairs:
        er = map_exit_reason(close.get("comment", ""), close.get("reason", -1))
        p = float(close.get("profit", 0))
        reason_count[er] += 1
        reason_pnl[er] += p
    for r in sorted(reason_count.keys()):
        print(f"  {r}: n={reason_count[r]}  pnl={reason_pnl[r]:+.2f}")
    total_pnl = sum(reason_pnl.values())
    print(f"  TOTAL backfilled PnL: {total_pnl:+.2f}")

    # 7. Apply or dry-run
    if not args.apply:
        print("\n=== DRY-RUN — no changes made. Re-run with --apply to commit. ===")
        return

    print(f"\n=== APPLYING {len(matched_pairs)} updates ===")
    updated = 0
    for (entry, close), t in matched_pairs:
        close_price = float(close.get("price", 0))
        profit = float(close.get("profit", 0))
        er = map_exit_reason(close.get("comment", ""), close.get("reason", -1))
        ct = float(close.get("time", 0))
        if ct > 1e12: ct /= 1000
        exit_time = datetime.fromtimestamp(ct, tz=timezone.utc).isoformat()
        # Compute pnl_pct
        entry_price = t["entry_price"]
        lot_size = t["lot_size"] or 0.01
        if entry_price > 0 and lot_size > 0:
            pnl_pct = round(profit / (entry_price * lot_size * CONTRACT_SIZE) * 100, 4)
        else:
            pnl_pct = 0.0
        try:
            cur.execute("""
                UPDATE live_trades
                SET exit_price = ?, pnl = ?, pnl_pct = ?,
                    exit_reason = ?, is_open = 0, exit_time = ?
                WHERE id = ?
            """, (round(close_price, 2), round(profit, 2), pnl_pct,
                  er, exit_time, t["id"]))
            updated += 1
        except Exception as e:
            print(f"  ❌ trade #{t['id']}: {e}")
    conn.commit()
    print(f"  Updated {updated} trades")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())