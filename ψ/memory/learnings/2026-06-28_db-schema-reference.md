---
name: db-schema-reference
description: Quick reference for god-port-oracle SQLite schema — live_trades, trade_outcomes, accounts, signals columns and the gotchas that cost 20 min debugging
metadata:
  type: reference
---

# DB Schema Quick Reference

**Source**: `data/oracle.db` (local) + VPS `/app/data/oracle.db` (production)
**Verified**: 2026-06-28 via `PRAGMA table_info`

## Why This File Exists

Wasted 20 min debugging `scripts/check_system.sh` SQL because column names were guessed from memory and wrong. Common mistakes:

| Guessed | Actual |
|---------|--------|
| `action` | `direction` |
| `entry_time` | `timestamp` |
| `net_pnl` | `pnl` |
| `account_id='A'` (string) | `account_id=1` (numeric) |

**Rule**: Always `PRAGMA table_info(<table>)` before writing SQL. Don't rely on memory.

## Tables Overview

| Table | Rows (local) | Purpose |
|-------|--------------|---------|
| `live_trades` | 20 | Live/paper trades from MT5 bridge |
| `trade_outcomes` | 196,797 | ML training data with features_json |
| `accounts` | 5 | Account registry (A=1, B=2, C=3, D=4) |
| `signals` | 23,340 | Generated signals |
| `feature_snapshots` | 23,340 | Feature values at signal time |
| `ml_experiments` | 9 | ML experiment metadata |
| `orders` | 0 | (unused — reserved) |
| `trades` | 0 | (unused — reserved) |
| `candles` | 0 | (unused — candles stored in parquet) |
| `indicator_definitions` | 0 | (unused — reserved) |

## account_id Mapping (IMPORTANT)

`account_id` is **numeric** in live_trades/trade_outcomes:

| account_id | Label | Type |
|------------|-------|------|
| 1 | A | Real |
| 2 | B | Demo |
| 3 | C | Demo |
| 4 | D | Demo |

Never use `account_id='A'` — it won't match.

## live_trades (51 columns)

Most-queried columns:

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | auto-increment |
| `account_id` | INTEGER NOT NULL | 1=A, 2=B, 3=C, 4=D |
| `timestamp` | TEXT NOT NULL | entry time (ISO) — NOT `entry_time` |
| `direction` | TEXT NOT NULL | 'BUY'/'SELL' — NOT `action` |
| `symbol` | TEXT NOT NULL | 'XAUUSD' |
| `entry_price` | REAL NOT NULL | |
| `stop_loss` | REAL NOT NULL | |
| `take_profit` | REAL NOT NULL | |
| `lot_size` | REAL NOT NULL | |
| `confidence` | REAL NOT NULL | |
| `regime` | TEXT | trending/ranging/volatile |
| `session` | TEXT | london/new_york/asian/london_ny_overlap |
| `d1_trend` | TEXT | bullish/bearish/neutral |
| `ticket` | INTEGER | MT5 ticket (NULL = ghost) |
| `exit_price` | REAL | |
| `exit_time` | TEXT | |
| `pnl` | REAL | NOT `net_pnl` |
| `pnl_pct` | REAL | |
| `exit_reason` | TEXT | take_profit/stop_loss/max_holding/trailing_stop/closed_by_mt5_inferred/ghost_no_mt5_ticket_inferred |
| `is_open` | INTEGER NOT NULL | 1=open, 0=closed |
| `tp1_price` | REAL | partial TP level |
| `parent_trade_id` | INTEGER | for partial TP children |
| `tp_level` | INTEGER | 0=parent, 1=TP1 hit |
| `remaining_lots` | REAL | |
| `ml_risk_multiplier` | REAL | ML filter output |
| `ml_loss_proba` | REAL | ML loss probability |
| `ml_model_used` | TEXT | which sub-model decided |
| `ml_model_version` | TEXT | model dir name |
| `atr_at_entry` | REAL | |
| `atr_multiplier` | REAL | per-account config at entry |
| `rr_ratio` | REAL | per-account config at entry |
| `min_confidence_threshold` | REAL | per-account config at entry |

## trade_outcomes (30 columns) — ML training data

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | |
| `trade_id` | INTEGER NOT NULL | FK to live_trades |
| `account_id` | INTEGER NOT NULL | |
| `symbol` | TEXT NOT NULL | |
| `direction` | TEXT NOT NULL | |
| `entry_price` / `exit_price` | REAL NOT NULL | |
| `profit` | REAL NOT NULL | NOT `pnl` here (different table, different name) |
| `profit_pct` | REAL NOT NULL | |
| `outcome_label` | TEXT NOT NULL | win/loss |
| `holding_minutes` | INTEGER | |
| `exit_reason` | TEXT | |
| `features_json` | TEXT | JSON blob of all features at entry — used by ML trainer |
| `mfe` / `mae` | REAL | max favorable / adverse excursion |
| `exit_regime` | TEXT | regime at exit |
| `exit_d1_trend` | TEXT | |

**Gotcha**: `live_trades.pnl` vs `trade_outcomes.profit` — different column names for similar data.

## accounts

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PK | matches account_id in live_trades |
| `name` | TEXT | 'Real-A', 'Demo-B', etc. |
| `account_type` | TEXT | 'real'/'demo' |
| `login` | INTEGER | MT5 login |
| `server` | TEXT | Exness server |
| `initial_balance` | REAL | |

## Real vs Ghost Trade Classification

Use `exit_reason` to filter:

```sql
-- Real trades only
WHERE exit_reason IN ('take_profit','stop_loss','max_holding','trailing_stop')

-- Ghost trades (do not count in performance)
WHERE exit_reason IN ('closed_by_mt5_inferred','ghost_no_mt5_ticket_inferred')

-- OR check ticket IS NULL
WHERE ticket IS NULL  -- ghost
```

## Useful Query Patterns

```sql
-- Real trades for account A this week
SELECT * FROM live_trades
WHERE account_id = 1
  AND exit_reason IN ('take_profit','stop_loss','max_holding','trailing_stop')
  AND timestamp >= date('now', '-7 days')
ORDER BY timestamp DESC;

-- Win rate + PF for account A real trades
SELECT
  COUNT(*) AS n,
  SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
  ROUND(100.0 * SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) AS win_rate_pct,
  ROUND(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) /
        ABS(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END)), 2) AS profit_factor
FROM live_trades
WHERE account_id = 1
  AND exit_reason IN ('take_profit','stop_loss','max_holding','trailing_stop');
```

## Related

- [[good-era-config-restore]] — uses these queries in check_system.sh
- [[mt5-source-of-truth-ghost-positions]] — ghost trade definition
- [[drawdown-db-sync]] — drawdown protection reads from live_trades