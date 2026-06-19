---
name: mt5-source-of-truth-ghost-positions
description: MT5 is source of truth for position state, not DB. Ghost trades (is_open=1, ticket=None) block all future trades.
metadata:
  type: lesson
  date: 2026-06-19
---

# MT5 as Source of Truth for Position State

**Lesson**: When checking if a position exists, always check MT5 first, not DB. DB records can become stale (ghost trades) and block all future trading.

**Why**: Ghost trades (is_open=1 but ticket=None) occur when a trade is recorded in DB but never reaches MT5 (bridge timeout, restart, etc.). If `_check_existing_position()` checks DB first, these ghosts block all new trades — the system thinks it has a position when Exness has none.

**How to apply**:
- Always query MT5 `positions_get()` as the primary check
- If MT5 says no position but DB has open trades → auto-close the ghosts
- Fallback to DB only when MT5 is unreachable
- Never hardcode symbols (use `account_registry.get_account_config()` for symbol, host, port)

**Related**: [[account-type-display-name]], [[multi-account-dynamic-scaling]]

**Bug details**:
- `_check_existing_position()` had hardcoded `symbol="XAUUSD"` → missed Real-A's `XAUUSDm`
- Port map was hardcoded `{"A": 5005}` → replaced with `get_account_config()`
- 3 ghost trades closed on VPS DB