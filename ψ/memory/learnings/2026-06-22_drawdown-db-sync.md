---
name: drawdown-db-sync
description: Drawdown protection now syncs PnL from DB — fixes rapid cycling bypass
metadata:
  type: feedback
---

# Drawdown Protection DB Sync Fix

## What happened

Account C lost $663 (132% of $500 equity) with 0% WR across 55 trades in one day. The 10% daily loss limit ($50) should have blocked trading after the first few losses, but didn't.

## Root cause

`DrawdownProtector` tracked PnL only in memory via `record_pnl()`. When `reconcile_closed_positions()` closed trades (SL/TP hit between oracle cycles), it updated the DB with exit_price and pnl, but never called `record_pnl()`. The drawdown protector was **blind** to reconciliation losses.

This created rapid cycling:
1. Oracle opens trade
2. SL hits in MT5 (between oracle cycles)
3. Next cycle: `reconcile_closed_positions()` closes trade in DB
4. Drawdown protector doesn't know → no block
5. `_check_existing_position()` returns False → opens new trade immediately
6. Repeat 55 times → $663 loss, far beyond 10% daily limit

## Fix

Made drawdown protection query the DB for actual PnL:

1. **`get_pnl_summary(account_id, db_path)`** in `db.py` — queries `live_trades` for today's and this week's closed trade PnL
2. **`sync_pnl_from_db(account_id, db_path)`** in `DrawdownProtector` — replaces in-memory counters with DB truth
3. Called before every `check()` in both `live_trader.py` and `m5_scalp_trader.py`
4. Called after every `reconcile_closed_positions()` for immediate feedback

This also makes drawdown protection **survive oracle restarts** — the DB is the source of truth, not in-memory state that resets on restart.

## User preference

User prefers V4 drawdown protection for real account (Account A) — daily 20%, weekly 30%, account 30%, cooldown 4h. Don't change Account A until demo data proves otherwise.

## How to apply

- Daily loss limits are now truly enforced because they query actual DB PnL
- No cooldown needed — the daily % limit naturally stops trading after sufficient losses
- MAX_POSITIONS isn't the problem — rapid cycling is, and DB sync fixes it