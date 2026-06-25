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
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from metty.core.account_registry import get_bridge_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "data/oracle.db")

# MT5 deal columns — known field names for _netref_to_dict extraction
DEAL_COLUMNS = [
    "ticket", "order", "time", "time_msc", "type", "magic", "identifier",
    "reason", "volume", "price", "commission", "swap", "profit", "symbol",
    "comment", "external_id",
]


def _netref_to_dict(netref_dict, columns: list[str]) -> dict:
    """Convert an RPyC netref dict to a local Python dict using known column names."""
    if netref_dict is None:
        return {}
    result = {}
    for col in columns:
        try:
            result[col] = netref_dict[col]
        except (KeyError, Exception):
            pass
    return result


async def get_deals_from_mt5(account: str, days_back: int = 90) -> list[dict]:
    """Fetch deal history from MT5 for a given account.

    Uses a single RPyC connection for initialize + history query.
    Returns list of local dicts (not netref proxies).
    """
    config = get_bridge_config(account)

    try:
        import rpyc

        conn = await asyncio.to_thread(
            rpyc.connect, config.bridge_host, config.bridge_port,
            config={"sync_request_timeout": 30},
        )

        try:
            # Initialize MT5 in this connection
            await asyncio.to_thread(conn.root.initialize)

            # Use Unix timestamps — bridge converts to datetime internally
            from_ts = int(time.time()) - days_back * 86400
            to_ts = int(time.time())

            deals_raw = await asyncio.to_thread(
                conn.root.exposed_history_deals_get, from_ts, to_ts
            )

        finally:
            # Always close the connection
            conn.close()

        if not deals_raw:
            logger.info("No deals found for account %s", account)
            return []

        # Convert netref dicts to local dicts
        deals = []
        for d in deals_raw:
            deal = _netref_to_dict(d, DEAL_COLUMNS)
            if deal and deal.get("symbol") in ("XAUUSD", "XAUUSDm", "XAUUSD.i", "XAUUSDb"):
                deals.append(deal)

        logger.info("Found %d XAUUSD deals for account %s (total: %d)",
                     len(deals), account, len(deals_raw))
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
    """Match an MT5 closing deal to a ghost trade.

    MT5 deal types:
      0 = BUY (deal entry)
      1 = SELL (deal entry or position close)
      2 = BALANCE (deposit/withdrawal)
      3 = CREDIT

    For closing deals:
      - A BUY position is closed by a SELL deal at the exit price
      - A SELL position is closed by a BUY deal at the exit price

    Matching strategy (in order of reliability):
      0. Ticket/order match (MT5 links deals to positions via order field)
      1. Price proximity + direction
      2. Exact SL/TP price match
      3. Time proximity (for force-closes where price is far from entry)
    """
    entry_price = float(trade.get("entry_price", 0))
    direction = trade.get("direction", "").upper()
    ticket = trade.get("ticket")

    # MT5 closing direction: opposite of entry
    # BUY position closed by SELL deal (type=1)
    # SELL position closed by BUY deal (type=0)
    closing_type = 1 if direction == "BUY" else 0

    # Strategy 0: Ticket/order match — most reliable
    # MT5 links closing deals to positions via deal.order == position.ticket
    if ticket is not None:
        ticket_int = int(ticket)
        for deal in deals:
            deal_order = deal.get("order")
            if deal_order is not None and int(deal_order) == ticket_int:
                deal_type = deal.get("type", -1)
                if deal_type not in (0, 1):
                    continue
                if deal_type == closing_type:
                    deal_price = float(deal.get("price", 0))
                    if deal_price > 0:
                        return deal

    # Strategy 1: match by price + direction
    best_match = None
    best_price_diff = float("inf")

    for deal in deals:
        # Skip non-trade deals (balance, credit)
        deal_type = deal.get("type", -1)
        if deal_type not in (0, 1):
            continue

        # Skip deals that are NOT the closing direction
        if deal_type != closing_type:
            continue

        deal_price = float(deal.get("price", 0))
        if deal_price == 0:
            continue

        # The closing deal price should be near the SL or TP of the trade
        # or at least in a reasonable range from entry
        price_diff = abs(deal_price - entry_price)

        # For XAUUSD, prices can be 1300-5000, so allow 0.5% tolerance
        max_diff = entry_price * 0.005  # 0.5% of entry price

        if price_diff < max_diff and price_diff < best_price_diff:
            best_price_diff = price_diff
            best_match = deal

    if best_match:
        return best_match

    # Strategy 2: if no closing deal found, try matching SL/TP price directly
    sl = float(trade.get("stop_loss", 0)) if trade.get("stop_loss") else 0
    tp = float(trade.get("take_profit", 0)) if trade.get("take_profit") else 0

    for deal in deals:
        deal_type = deal.get("type", -1)
        if deal_type != closing_type:
            continue

        deal_price = float(deal.get("price", 0))
        if deal_price == 0:
            continue

        # Check if deal price matches SL or TP exactly
        if (sl > 0 and abs(deal_price - sl) < 0.1) or (tp > 0 and abs(deal_price - tp) < 0.1):
            return deal

    # Strategy 3: time proximity — for force-closes where price is far from entry
    trade_time = trade.get("timestamp") or trade.get("exit_time")
    if trade_time and deals:
        try:
            from datetime import datetime as _dt, timezone as _tz
            if isinstance(trade_time, str):
                ts = trade_time.replace("Z", "+00:00")
                trade_dt = _dt.fromisoformat(ts)
                if trade_dt.tzinfo is None:
                    trade_dt = trade_dt.replace(tzinfo=_tz.utc)

                best_time_match = None
                best_time_diff = float("inf")
                max_time_diff = 60  # seconds

                for deal in deals:
                    deal_type = deal.get("type", -1)
                    if deal_type not in (0, 1):
                        continue
                    if deal_type != closing_type:
                        continue

                    deal_price = float(deal.get("price", 0))
                    if deal_price == 0:
                        continue

                    deal_time = deal.get("time")
                    if deal_time is None:
                        continue
                    deal_ts = float(deal_time)
                    if deal_ts > 1e12:  # milliseconds
                        deal_ts /= 1000

                    time_diff = abs(trade_dt.timestamp() - deal_ts)
                    if time_diff < max_time_diff and time_diff < best_time_diff:
                        best_time_diff = time_diff
                        best_time_match = deal

                if best_time_match:
                    return best_time_match
        except (ValueError, OSError):
            pass

    return None


def infer_exit_price(trade: dict) -> float | None:
    """Infer exit price when deal history is unavailable.

    For ghost trades, MT5 closed the position but we couldn't find the deal.
    Most likely scenarios:
      - SL hit → use stop_loss
      - TP hit → use take_profit
      - Force-close / unknown → use entry_price (breakeven, more neutral than SL)

    IMPORTANT: We no longer default to SL for unknown cases. Using SL as default
    made force-closed trades appear as worse losses than they actually were, because
    MT5 closes at market price (not SL price) during margin calls / stop-outs.
    Using entry_price as fallback records PnL as ~0 instead of a fabricated loss.
    """
    sl = trade.get("stop_loss")
    tp = trade.get("take_profit")
    direction = trade.get("direction", "").upper()
    reason = trade.get("exit_reason", "")

    # Check exit_reason for hints
    if "sl" in reason.lower() or "stop" in reason.lower():
        return float(sl) if sl else None

    if "tp" in reason.lower() or "take" in reason.lower():
        return float(tp) if tp else None

    # For force-closes / unknown exits: use entry_price (breakeven), NOT SL.
    # SL price ≠ actual close price during margin calls / stop-outs.
    entry = trade.get("entry_price")
    if entry:
        logger.warning(
            "Trade #%d: no deal match, using entry_price=%.2f as exit (not SL=%.2f) "
            "— force-close prices differ from SL",
            trade.get("id"), float(entry), float(sl) if sl else 0,
        )
        return float(entry)

    # Absolute last resort
    if sl:
        return float(sl)

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

    # Match and update
    conn = sqlite3.connect(db_path)
    updated = 0
    inferred = 0
    skipped = 0

    for trade in ghost_trades:
        deal = match_deal_to_trade(trade, deals) if deals else None

        if deal and deal.get("price") is not None:
            # Found matching deal — use deal's exit price and profit
            exit_price = float(deal["price"])
            pnl = float(deal.get("profit", 0))
            exit_reason = trade.get("exit_reason", "closed_by_mt5")

            # Check deal comment for SL/TP info
            comment = deal.get("comment", "")
            if "[sl" in str(comment):
                exit_reason = f"{exit_reason}_sl_hit"
            elif "[tp" in str(comment):
                exit_reason = f"{exit_reason}_tp_hit"

            logger.info(
                "  Trade #%d (%s %s @ %s): matched deal → exit=%.2f, pnl=%.2f, comment=%s",
                trade["id"], trade["direction"], trade.get("symbol", "?"),
                trade["entry_price"], exit_price, pnl, comment,
            )
        else:
            # No deal found — infer exit price from SL/TP
            exit_price = infer_exit_price(trade)
            if exit_price is None:
                logger.warning(
                    "  Trade #%d: cannot determine exit_price, skipping",
                    trade["id"],
                )
                skipped += 1
                continue

            # Calculate PnL from entry and inferred exit
            direction = trade.get("direction", "").upper()
            entry = float(trade["entry_price"])
            lot_size = float(trade.get("lot_size", 0.01))
            # XAUUSDm: 1 lot = 100 oz, $1 move = $100 per lot
            contract_size = 100  # oz per lot for XAUUSDm
            if direction == "BUY":
                pnl = (exit_price - entry) * lot_size * contract_size
            else:
                pnl = (entry - exit_price) * lot_size * contract_size

            exit_reason = f"{trade.get('exit_reason', 'unknown')}_inferred"
            inferred += 1
            logger.info(
                "  Trade #%d (%s @ %s): inferred exit=%.2f, pnl=%.2f (reason: %s)",
                trade["id"], trade["direction"], trade["entry_price"],
                exit_price, pnl, exit_reason,
            )

        if dry_run:
            logger.info("    [DRY RUN] Would update trade #%d", trade["id"])
        else:
            conn.execute(
                """UPDATE live_trades
                   SET exit_price = ?, pnl = ?, pnl_pct = ?,
                       exit_reason = ?, is_open = 0
                   WHERE id = ? AND exit_price IS NULL""",
                (
                    exit_price,
                    round(pnl, 2),
                    round(pnl / (float(trade["entry_price"]) * lot_size * contract_size) * 100, 2)
                    if float(trade["entry_price"]) > 0 else 0.0,
                    exit_reason,
                    trade["id"],
                ),
            )
            updated += 1

    if not dry_run and updated > 0:
        conn.commit()
        logger.info(
            "Updated %d/%d trades for account %s (inferred: %d, skipped: %d)",
            updated, len(ghost_trades), account, inferred, skipped,
        )
    elif dry_run and ghost_trades:
        logger.info(
            "[DRY RUN] Would update %d/%d trades for account %s (inferred: %d, skipped: %d)",
            len(ghost_trades) - skipped, len(ghost_trades), account, inferred, skipped,
        )

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