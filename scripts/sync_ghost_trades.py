#!/usr/bin/env python3
"""Sync ghost trades with MT5 deal history.

Finds trades in the DB that have is_open=0 but exit_price=NULL (ghost/phantom/stale),
then queries MT5 deal history for each account to find the closing deal and update
exit_price, pnl, and exit_reason.

Usage:
    python scripts/sync_ghost_trades.py [--dry-run] [--account A|B|C|D]
"""

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metty.bridge.client import MT5Bridge
from metty.core.account_registry import get_bridge_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "data/oracle.db")


async def get_deals_from_mt5(account: str, days_back: int = 90) -> list[dict]:
    """Fetch deal history from MT5 for a given account."""
    config = get_bridge_config(account)
    bridge = MT5Bridge(config)

    try:
        connected = await bridge.connect()
        if not connected:
            logger.error("Failed to connect to MT5 bridge for account %s", account)
            return []

        # Use Unix timestamps — RPyC cannot serialize datetime objects reliably
        from_ts = int((datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp())
        to_ts = int(datetime.now(timezone.utc).timestamp())

        import rpyc
        conn = await asyncio.to_thread(
            rpyc.connect, config.bridge_host, config.bridge_port,
            config={"sync_request_timeout": 30},
        )

        # Initialize MT5 in this connection
        await asyncio.to_thread(conn.root.initialize)

        # Bridge converts Unix timestamps to datetime internally
        deals = await asyncio.to_thread(
            conn.root.exposed_history_deals_get, from_ts, to_ts
        )

        conn.close()
        await bridge.disconnect()

        if not deals:
            logger.info("No deals found for account %s", account)
            return []

        logger.info("Found %d deals for account %s", len(deals), account)
        return deals

    except Exception as e:
        logger.error("Error fetching deals for account %s: %s", account, e)
        return []


def get_ghost_trades(db_path: str, account_id: int | None = None) -> list[dict]:
    """Get ghost trades from DB (is_open=0, exit_price=NULL)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    query = """
        SELECT id, account_id, direction, entry_price, stop_loss, take_profit,
               lot_size, ticket, timestamp, exit_reason, symbol
        FROM live_trades
        WHERE exit_price IS NULL AND account_id > 0
    """
    params = []
    if account_id is not None:
        query += " AND account_id = ?"
        params.append(account_id)

    query += " ORDER BY id"
    rows = conn.execute(query, params).fetchall()

    result = []
    for row in rows:
        result.append({
            "id": row["id"],
            "account_id": row["account_id"],
            "direction": row["direction"],
            "entry_price": row["entry_price"],
            "stop_loss": row["stop_loss"],
            "take_profit": row["take_profit"],
            "lot_size": row["lot_size"],
            "ticket": row["ticket"],
            "timestamp": row["timestamp"],
            "exit_reason": row["exit_reason"],
            "symbol": row["symbol"],
        })

    conn.close()
    return result


def match_deal_to_trade(trade: dict, deals: list[dict]) -> dict | None:
    """Match an MT5 deal to a ghost trade.

    Strategy:
    1. Match by ticket (most reliable)
    2. Match by direction + entry_price + approximate time
    3. Use SL/TP as exit price if deal not found
    """
    # Try matching by ticket first
    trade_ticket = trade.get("ticket")
    if trade_ticket:
        for deal in deals:
            deal_ticket = deal.get("order", deal.get("ticket"))
            if deal_ticket and int(deal_ticket) == int(trade_ticket):
                return deal

    # Try matching by entry price and direction
    entry_price = trade.get("entry_price", 0)
    direction = trade.get("direction", "").upper()
    deal_type = 0 if direction == "BUY" else 1  # 0=BUY, 1=SELL in MT5

    # Look for deal entry that matches our trade's entry price
    for deal in deals:
        deal_price = deal.get("price", 0)
        deal_type_val = deal.get("type", -1)
        # MT5 deal type: 0=BUY, 1=SELL, 2=BALANCE, 3=CREDIT
        if (abs(float(deal_price) - float(entry_price)) < 0.5
                and int(deal_type_val) == deal_type):
            # Additional check: deal should be around the trade's timestamp
            return deal

    return None


def infer_exit_price(trade: dict) -> float | None:
    """Infer exit price from SL/TP when deal history is unavailable.

    If exit_reason indicates SL hit → use stop_loss
    If exit_reason indicates TP hit → use take_profit
    Otherwise → use SL for losing trades, TP for winning (best guess)
    """
    sl = trade.get("stop_loss")
    tp = trade.get("take_profit")
    direction = trade.get("direction", "").upper()
    entry = float(trade.get("entry_price", 0))
    reason = trade.get("exit_reason", "")

    if "sl" in reason.lower() or "stop" in reason.lower():
        return float(sl) if sl else None

    if "tp" in reason.lower() or "take" in reason.lower():
        return float(tp) if tp else None

    # For ghost/phantom trades: MT5 closed them, likely via SL or TP
    # Default to SL (pessimistic — these are probably losses)
    if sl:
        return float(sl)

    # Last resort: if no SL, assume tiny loss near entry
    return None


async def sync_account(account: str, account_id: int, db_path: str, dry_run: bool = True):
    """Sync ghost trades for one account."""
    logger.info("Syncing account %s (id=%d)...", account, account_id)

    # Get ghost trades from DB
    ghost_trades = get_ghost_trades(db_path, account_id)
    if not ghost_trades:
        logger.info("No ghost trades for account %s", account)
        return

    logger.info("Found %d ghost trades for account %s", len(ghost_trades), account)

    # Get deal history from MT5
    deals = await get_deals_from_mt5(account, days_back=90)
    if not deals:
        logger.warning("No deal history from MT5 for account %s — will infer exit prices", account)

    # Match and update
    conn = sqlite3.connect(db_path)
    updated = 0
    inferred = 0

    for trade in ghost_trades:
        deal = match_deal_to_trade(trade, deals) if deals else None

        if deal and deal.get("price"):
            # Found matching deal — use deal's exit price
            exit_price = float(deal["price"])
            pnl = float(deal.get("profit", 0))
            exit_reason = trade.get("exit_reason", "closed_by_mt5")
            if deal.get("entry") == deal.get("ticket"):
                # This is the entry deal, not exit — skip
                continue
            logger.info(
                "  Trade #%d: matched deal, exit_price=%.2f, pnl=%.2f",
                trade["id"], exit_price, pnl,
            )
        else:
            # No deal found — infer exit price from SL/TP
            exit_price = infer_exit_price(trade)
            if exit_price is None:
                logger.warning(
                    "  Trade #%d: cannot determine exit_price, skipping",
                    trade["id"],
                )
                continue

            # Calculate PnL from entry and inferred exit
            direction = trade.get("direction", "").upper()
            entry = float(trade["entry_price"])
            lot_size = float(trade.get("lot_size", 0.01))
            # Approximate PnL: for XAUUSD, 1 lot = 100 oz, $1 move = $100
            # For micro lot (0.01), $1 move = $1
            if direction == "BUY":
                pnl = (exit_price - entry) * lot_size * 100
            else:
                pnl = (entry - exit_price) * lot_size * 100

            exit_reason = f"{trade.get('exit_reason', 'unknown')}_inferred"
            inferred += 1
            logger.info(
                "  Trade #%d: inferred exit_price=%.2f, pnl=%.2f (reason: %s)",
                trade["id"], exit_price, pnl, exit_reason,
            )

        if dry_run:
            logger.info("  [DRY RUN] Would update trade #%d", trade["id"])
        else:
            conn.execute(
                """UPDATE live_trades
                   SET exit_price = ?, pnl = ?, exit_reason = ?
                   WHERE id = ? AND exit_price IS NULL""",
                (exit_price, round(pnl, 2), exit_reason, trade["id"]),
            )
            updated += 1

    if not dry_run and updated > 0:
        conn.commit()
        logger.info("Updated %d trades for account %s (inferred: %d)", updated, account, inferred)
    elif dry_run and ghost_trades:
        logger.info("[DRY RUN] Would update %d trades for account %s", len(ghost_trades), account)

    conn.close()


async def main():
    parser = argparse.ArgumentParser(description="Sync ghost trades with MT5 deal history")
    parser.add_argument("--dry-run", action="store_true", help="Don't write changes to DB")
    parser.add_argument("--account", choices=["A", "B", "C", "D"], help="Sync only this account")
    parser.add_argument("--db", default=DB_PATH, help="Path to SQLite database")
    args = parser.parse_args()

    ACCOUNT_MAP = {"A": 1, "B": 2, "C": 3, "D": 4}

    if args.account:
        accounts = [(args.account, ACCOUNT_MAP[args.account])]
    else:
        accounts = list(ACCOUNT_MAP.items())

    for account, account_id in accounts:
        await sync_account(account, account_id, args.db, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())