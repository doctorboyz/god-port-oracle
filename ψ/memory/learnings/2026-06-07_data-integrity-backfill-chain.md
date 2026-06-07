---
name: data-integrity-backfill-chain
description: Data chains must be verified end-to-end. Backfill is only as good as its source. Existing rows need UPDATE, not just INSERT.
metadata:
  type: learning
  created: 2026-06-07
---

# Data Integrity: Backfill Chains & Type Safety

## Problem
Trading params (`atr_multiplier`, `rr_ratio`, `min_confidence_threshold`) were added as DB columns but 3 separate issues prevented data from being populated:
1. `get_connection()` only accepted `Path`, not `str` — crashed when called with string path
2. `backfill_trade_outcomes()` skipped existing rows entirely, even if they had NULL trading params
3. `live_trades` itself had NULL params because trades were created before the code update

## Root Cause
**Missing end-to-end verification** — I verified columns existed but didn't verify data was actually populated. The backfill could only copy from source, so when `live_trades` had NULL values, `trade_outcomes` stayed NULL too.

## Solution Pattern
1. **Backfill must UPDATE, not just INSERT** — existing rows with NULL columns should be updated from source
2. **Verify source before copying** — check source table has data before backfill
3. **Account-based param mapping** — when source has NULL, reconstruct from known config (account_id → fixed params)
4. **Type guards at DB boundaries** — `get_connection()` should accept both `str` and `Path`

## How to Apply
- Every data migration: verify source table first, then target
- Add data integrity checks: `SELECT COUNT(*) FROM table WHERE required_col IS NULL`
- Backfill functions should handle both INSERT (missing rows) and UPDATE (incomplete rows)
- Use `Path | str` type hints for file path parameters

## Related
- [[backfill-trade-outcomes]]
- [[trading-params-per-trade]]