# Vibe Trading Integration Baseline

**Date**: 2026-05-10
**Context**: P2 integration complete, LEARNING_MODE activated on all 3 demo accounts
**Source**: rrr: god-port-oracle

## Key Fact

Vibe trading (LEARNING_MODE=1) was activated on 2026-05-09. This is the baseline shift point. All future trading results must be compared against pre-vibe-trading performance to measure improvement.

## Pre-Vibe Trading Baseline (before 2026-05-09)

| Account | Balance | Trades | WR | Total PnL |
|---------|---------|--------|------|-----------|
| A | $100 | 363 | 39.4% | +$333.79 |
| B | $500 | 365 | 39.2% | +$422.63 |
| C | $1,000 | 391 | 37.6% | +$364.18 |

- All accounts profitable despite low WR (target: 55%)
- R:R compensating — wins are larger than losses
- All positions identical SELL at same entry price (no diversification)

## Vibe Trading Config

- TRADING_PHASE=both (collect + trade)
- DRY_RUN=0 (real demo trading)
- M5_SCALP_ENABLED=1
- LEARNING_MODE=1 (bypass blockers, max trades for data)
- MAX_POSITIONS_PER_ACCOUNT=5

## What to Monitor

1. **Win Rate trend** — Should improve from ~38% baseline if learning loop works
2. **Position diversification** — Currently all positions identical. Learning should introduce variation
3. **M5 Scalp performance** — New strategy, no baseline yet
4. **Learning loop adjustments** — First adjustment expected after 10+ new trades accumulate
5. **Max drawdown** — Currently unknown per-account, need to track

## Why This Matters

This is the first real test of the full system: P0 (position sizing, slippage, liquidation) + P1 (comparison, broker ABC, validator) + P2 (registry, LLM analyzer) + learning mode all running together. The before/after comparison of vibe trading activation is the key metric for whether the system actually improves itself.

**How to apply**: When reviewing trading results, always note whether they are pre- or post-vibe-trading activation. The learning loop daily report at 00:05 UTC will show parameter adjustments once enough trades accumulate.