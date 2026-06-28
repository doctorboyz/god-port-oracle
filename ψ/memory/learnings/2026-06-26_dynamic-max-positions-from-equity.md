---
name: dynamic-max-positions-from-equity
description: Max positions calculated from equity with $200 buffer per position, minimum 1, capped at 5. Prevents margin calls on small accounts.
metadata:
  type: project
---

# Dynamic Max Positions from Equity

**Date**: 2026-06-26
**Why**: On June 22, Real-A ($199 equity) opened 18 positions simultaneously because max_positions was hardcoded to 5. This caused a margin call. Dynamic limits scale with account size.

**Formula**: `max(1, min(cap, floor(equity / equity_per_position)))`
- `equity_per_position` = $200 (default, configurable via `EQUITY_PER_POSITION_A` env var)
- `cap` = 5 (configurable via `MAX_POSITIONS_CAP_A` env var)

**How to apply**:
- Small accounts ($199) → 1 position (safe)
- Growing accounts ($400) → 2 positions
- Large accounts ($1000+) → 5 positions (capped)
- Both `M5ScalpTrader` and `LiveTrader` use the same `_calculate_max_positions()` method
- [[ghost-trade-prevention]] — related; both address the June 22 margin call incident

**Config env vars**:
- `EQUITY_PER_POSITION_A` — min equity per position (default 200)
- `MAX_POSITIONS_CAP_A` — hard max regardless of equity (default 5)

**Files**: `m5_scalp_trader.py`, `live_trader.py`, `account_registry.py`, `docker-compose.vps.yml`