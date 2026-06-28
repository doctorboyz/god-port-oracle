---
name: good-era-config-restore
description: Restore V4 good era config — DRAWDOWN_DAILY_LIMIT_A=0.05, BUY_MIN_CONFIDENCE_A=0.50, equity topped up to $200
metadata:
  type: project
---

# Good Era Config Restore + $200 Top-up

**Date**: 2026-06-28
**Why**: V4 was profitable in good era (before Jun 15, 938 trades +$1,997). After LEARNING_MODE=1 + sync_pnl bug broke risk management, performance degraded. Bugs fixed but daily limit was 20% (too lenient) and BUY_MIN_CONFIDENCE was 0.45 (let weak BUYs through).

## Changes Applied (VPS .env)

| Parameter | Before | After | Reason |
|-----------|--------|-------|--------|
| `DRAWDOWN_DAILY_LIMIT_A` | 0.20 (20%) | **0.05 (5%)** | Good era value — stops bad days at $10 loss |
| `BUY_MIN_CONFIDENCE_A` | 0.45 | **0.50** | Good era value — filters weak BUY (BUY PF=0.52 was destroying profit) |
| `INITIAL_EQUITY_A` | 78 | **200** | User topped up to $200, peak_equity must match |
| `INITIAL_BALANCE_A` | 100 | **200** | Match actual MT5 balance |

## What Stayed (Already Correct)

- `LEARNING_MODE=0` (good era)
- `ATR_MULTIPLIER_A=1.5` (good era)
- `RR_RATIO_A=2.5` (good era)
- `ML_MODEL_DIR_A=trade_outcome_v4` (V4 crown jewel: trending_SELL PF=3.0)
- `ML_FILTER_ENABLED=1`

## Top-up Sequence (IMPORTANT)

**Must update INITIAL_EQUITY_A AFTER top-up confirmed, not before.**

If set INITIAL_EQUITY_A=200 while equity still $78:
- peak=200, equity=78 → drawdown = 61% → BLOCKED immediately

Correct sequence:
1. Deploy config changes (DRAWDOWN, BUY_MIN_CONFIDENCE)
2. User tops up Exness account
3. Verify MT5 shows new equity via `docker compose logs oracle-engine | grep balance=`
4. Update INITIAL_EQUITY_A + INITIAL_BALANCE_A to match actual
5. Restart oracle-engine
6. Verify no BLOCKED messages

## Dynamic Max Positions at $200

`max(1, min(5, floor(200/200)))` = `max(1, min(5, 1))` = **1 position**

To get 2 positions need $400 equity. To get 5 (cap) need $1000.

## What to Verify Monday When Market Opens

1. DrawdownProtector initializes with peak=200 (look for "Initialized: equity=200.00")
2. Real trades appear (exit_reason = take_profit/stop_loss/max_holding, NOT closed_by_mt5_inferred)
3. BUY signals reduced ~30% (conf ≥0.50 filter)
4. Daily PnL stays within ±$10 (5% limit)
5. If ghost trades continue → MT5 margin/order issue, not config

## Files

- VPS `.env` at `/opt/god-port-oracle/.env`
- `scripts/check_system.sh` — performance/availability checker with market-open awareness

## Related

- [[win-config-analysis]] — full good era vs broken era analysis
- [[dynamic-max-positions-from-equity]] — position limit logic
- [[drawdown-db-sync]] — drawdown protection DB sync