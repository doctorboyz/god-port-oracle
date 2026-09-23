#!/usr/bin/env python3
"""ISSUE-080 surgical fix: backfill correct PnL for 3 corrupted Real-A trades.

Trades #5541/#5543/#5546 (2026-07-07/08) had exit prices SHIFTED between
positions by the ISSUE-074/079 reconcile bug — e.g. #5543 (BUY) is recorded
as exit_reason='stop_loss' with pnl +7.66, impossible. This script fetches
real MT5 deal history, matches each trade's entry deal by direction +
entry price + time and its closing deal by SL/TP comment level, prints
the proposed update, and applies ONLY with --apply after the values are
confirmed against the recorded evidence.

Run inside oracle-engine container. DB backup taken 2026-09-23 at
/opt/db-backups/oracle_pre_080_20260923_1647.db (89MB) before any apply.

Matching lesson (kept for future backfills, incl. the 57 remaining
pnl=0 'inferred' rows if scripts/backfill_a_pnl_from_deals.py is ever
run): FIFO pairing mis-assigns closes when two same-direction positions
close seconds apart — #5546's close '[sl 4054.18400]' was first paired
to another position's entry, yielding -11.39 instead of -7.95. MT5
closes exactly at SL/TP level, so a close deal's '[sl X]'/'[tp X]'
comment matched against the DB trade's stop_loss/take_profit is
decisive evidence and must take precedence over FIFO.
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")

CONTRACT_SIZE = 100.0  # XAUUSD: 1 lot = 100 oz
TARGET_IDS = (5541, 5543, 5546)

# Expected values recorded in ISSUE-080 from the July deal-history
# inspection. All three now confirmed against actual MT5 deals:
# #5546's close is '[sl 4054.18400]' profit -7.95 — its DB stop_loss is
# 4054.1844, matching exactly; the -11.39 close ('[sl 4059.71500]')
# belongs to a DIFFERENT position (FIFO pairing mis-assigns it).
EXPECTED = {
    5541: {"exit_price": 4132.34, "pnl": -10.16},
    5543: {"exit_price": 4112.76, "pnl": -11.92},
    5546: {"exit_price": 4054.18, "pnl": -7.95},
}


def map_exit_reason(comment: str, deal_reason: int) -> str:
    c = (comment or "").lower()
    if "[sl" in c or "sl " in c:
        return "stop_loss"
    if "[tp" in c or "tp " in c:
        return "take_profit"
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
        return await bridge.get_deal_history("XAUUSDm", days_back=days_back)
    finally:
        await bridge.disconnect()


def _comment_level(comment: str):
    """Extract SL/TP level from '[sl 4054.18400]' / '[tp 3999.5]' comments."""
    c = comment or ""
    for tag in ("sl", "tp"):
        marker = f"[{tag} "
        i = c.find(marker)
        if i >= 0:
            rest = c[i + len(marker):].split("]")[0].strip()
            try:
                return tag, float(rest)
            except ValueError:
                continue
    return None, None


def find_entry_deal(trade, deals):
    """Find the opening deal for a DB trade: direction + price + time."""
    best = None
    for d in deals:
        t = d.get("type")
        if t not in (0, 1):
            continue
        if ("BUY" if t == 0 else "SELL") != trade["direction"]:
            continue
        ep = float(d.get("price", 0))
        if abs(trade["entry_price"] - ep) / trade["entry_price"] > 0.003:
            continue
        try:
            db_ts = datetime.fromisoformat(trade["timestamp"]).timestamp()
        except Exception:
            continue
        et = float(d.get("time", 0))
        if et > 1e12:
            et /= 1000
        if abs(db_ts - et) > 300:
            continue
        if best is None or abs(db_ts - et) < best[0]:
            best = (abs(db_ts - et), d)
    return best[1] if best else None


def find_close_deal(trade, deals):
    """Find the closing deal for a DB trade.

    Gates on opposite direction + occurring after the entry window, then
    prefers a deal whose '[sl X]'/'[tp X]' comment level equals the DB
    trade's stop_loss/take_profit — MT5 closes exactly at SL/TP level,
    so this is decisive when two same-direction positions closed seconds
    apart (the #5546 case, where FIFO mis-assigns).
    """
    close_dir_type = 1 if trade["direction"] == "BUY" else 0
    try:
        db_ts = datetime.fromisoformat(trade["timestamp"]).timestamp()
    except Exception:
        return None
    best = None
    for d in deals:
        if d.get("type") != close_dir_type:
            continue
        if not (d.get("profit", 0) or 0):
            continue
        ct = float(d.get("time", 0))
        if ct > 1e12:
            ct /= 1000
        if ct < db_ts or ct > db_ts + 86400:
            continue
        tag, level = _comment_level(d.get("comment", ""))
        target = trade.get("stop_loss") if tag == "sl" else (
            trade.get("take_profit") if tag == "tp" else None)
        if tag and target and abs(target - level) < 0.01:
            return d  # decisive — SL/TP level match
        score = ct - db_ts
        if best is None or score < best[0]:
            best = (score, d)
    return best[1] if best else None


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--days-back", type=int, default=95)
    args = parser.parse_args()

    print(f"=== ISSUE-080 surgical backfill ({'APPLY' if args.apply else 'DRY-RUN'}) ===")
    deals = await fetch_deals(args.days_back)
    print(f"deals fetched: {len(deals)}")

    conn = sqlite3.connect("/app/data/oracle.db")
    conn.row_factory = sqlite3.Row
    trades = [dict(r) for r in conn.execute(
        "SELECT id, timestamp, direction, entry_price, stop_loss, "
        "take_profit, exit_price, pnl, exit_reason, exit_time, lot_size "
        "FROM live_trades "
        f"WHERE account_id=1 AND id IN {TARGET_IDS} ORDER BY id")]

    if len(trades) != 3:
        print(f"FATAL: expected 3 target trades, got {len(trades)} — abort")
        sys.exit(1)

    updates = []
    for t in trades:
        entry = find_entry_deal(t, deals)
        if entry is None:
            print(f"  ❌ trade #{t['id']}: entry deal NOT in history — aborting this trade")
            continue
        close = find_close_deal(t, deals)
        if close is None:
            print(f"  ❌ trade #{t['id']}: NO matching deal pair — aborting this trade")
            continue
        exit_price = round(float(close.get("price", 0)), 2)
        profit = round(float(close.get("profit", 0)), 2)
        exp = EXPECTED[t["id"]]
        ok = (abs(exit_price - exp["exit_price"]) < 0.01
              and abs(profit - exp["pnl"]) < 0.01)
        ct = float(close.get("time", 0))
        if ct > 1e12:
            ct /= 1000
        exit_time = datetime.fromtimestamp(ct, tz=timezone.utc).isoformat()
        reason = map_exit_reason(close.get("comment", ""), close.get("reason", -1))
        lot = t["lot_size"] or 0.01
        pnl_pct = round(profit / (t["entry_price"] * lot * CONTRACT_SIZE) * 100, 4)
        mark = "✅" if ok else "⚠️  MISMATCH"
        print(f"  {mark} trade #{t['id']} {t['direction']} @ {t['entry_price']}: "
              f"DB=({t['exit_price']}, {t['pnl']}, {t['exit_reason']}) "
              f"→ deal=({exit_price}, {profit}, {reason}) expected=({exp['exit_price']}, {exp['pnl']})")
        if not ok:
            print(f"      deal does not match ISSUE-080 recorded value — NOT updating #{t['id']}")
            continue
        updates.append((t["id"], exit_price, profit, pnl_pct, reason, exit_time))

    if not args.apply:
        print(f"\nDRY-RUN — {len(updates)}/3 verified updates ready. Re-run with --apply.")
        return
    cur = conn.cursor()
    for (tid, ep, p, pp, reason, et) in updates:
        cur.execute(
            "UPDATE live_trades SET exit_price=?, pnl=?, pnl_pct=?, exit_reason=?, "
            "is_open=0, exit_time=? WHERE id=?",
            (ep, p, pp, reason, et, tid))
    conn.commit()
    print(f"\nAPPLIED {len(updates)} updates.")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())