---
name: ghost-trade-prevention
description: How ghost trades happen and how reconciliation prevents them
metadata:
  type: project
---

# Ghost Trade Prevention

## What happened
- 43 trades had `is_open=0` but `exit_price=NULL` — MT5 closed positions (SL/TP hit) between oracle cycles, but oracle never recorded the exit price
- Root cause: `close_ghost_trades()` only set `is_open=0` and `exit_reason`, never `exit_price`, `pnl`, or `pnl_pct`
- Secondary: `_check_existing_position()` only called `close_ghost_trades()` for `ticket IS NULL`, missing trades with valid tickets closed by MT5

## Fix (deployed 2026-06-22)

### 1. `close_ghost_trades()` — now sets exit_price via SQL COALESCE
- Uses `COALESCE(stop_loss, entry_price)` as exit_price (SL is most likely for ghost trades)
- Computes `pnl` and `pnl_pct` from the inferred exit_price
- Still only targets `ticket IS NULL` trades

### 2. `reconcile_closed_positions()` — new function in db.py
- Compares DB open trades against MT5 positions
- For trades MT5 no longer has (broker closed between cycles):
  - Tries `_match_closing_deal()` to find actual exit price from deal history
  - Falls back to `_infer_exit_price()` (SL/TP) when deal not found
- Always sets exit_price, pnl, pnl_pct, exit_reason

### 3. `_check_existing_position()` — updated in both traders
- Now calls `reconcile_closed_positions()` instead of just `close_ghost_trades()`
- Fetches deal history via `_get_deal_history()` → `MT5Bridge.fetch_deal_history_sync()`

### 4. `get_deal_history()` + `fetch_deal_history_sync()` — new in MT5Bridge
- Queries MT5 `history_deals_get()` via RPyC bridge
- Uses Unix timestamps (RPyC cannot serialize datetime objects)
- Filters for XAUUSD/XAUUSDm deals with non-zero volume

## Bugs found in deployment

1. `logger` was defined inside `check_data_integrity()` function scope, not module level → `NameError` in `reconcile_closed_positions()`. Fixed by adding module-level `logger = logging.getLogger(__name__)`.
2. `config.name.value` → `config.name` — after AccountConfig refactor, `name` is a `str`, not an enum.

## RPyC Gotchas
- Cannot serialize `datetime` objects — must use Unix timestamps
- Cannot forward `**kwargs` — must use positional args
- Netref dicts don't support `.get()` or `.keys()` — must use bracket access with known column names

## Verification
- VPS DB: 0 ghost trades, 5252 total trades all with exit_price
- All new functions import successfully in container
- Production trades executing on A, C, D accounts

## Related
- [[mt5-source-of-truth-ghost-positions]]
- [[rpyc-netref-dict-limitations]]