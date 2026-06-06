---
name: partial-tp-backtest-option-c
description: Backtest results for Option C (TP1 → Scale-In) partial TP strategy
metadata:
  type: project
---

# Partial TP Backtest — Option C Results

**Date**: 2026-06-05
**Strategy**: Close at TP1 (50% of TP distance), reopen at TP1 with SL from final TP RR ratio

## Key Findings

- Option C **improves PnL by ~$14,698** across 2,154 trades (current: $4,349 → Option C: ~$19,047)
- Win rate stays ~78.8% (no change in total wins, but loss trades become smaller)
- **34.6% of trades reach TP1 but NOT final TP** → these benefit most from partial TP
- **~46% of SL trades likely reached TP1 before reversing** (Monte Carlo estimate)
- Improvement holds across all TP1 ratios (30-70%) and RR ratios (1.5-3.0)

## Scenarios

| Scenario | % | Current | Option C | Impact |
|----------|---|---------|----------|--------|
| TP1 → TP final | 37.7% | Normal TP win | +$15.76 | Small improvement |
| TP1 → not TP final | 34.6% | Mixed PnL | +$2,263 | **Big improvement** |
| Never TP1 | 27.7% | Same as current | Same | No change |
| SL (MC) | ~46% reached TP1 | Full SL loss | +$12,419 | **Big improvement** |

## Caveats

- SL trade MFE is estimated via Monte Carlo (random walk probability), not from actual candle data
- M5 candle data only covers 2023-06 to 2026-04, but trades start 2026-05
- Need real MFE data collection to validate

## Why It Works

Option C works because:
1. Many trades reach TP1 (50% of TP distance) but reverse before final TP
2. Current strategy: these trades either hit SL (full loss) or exit at max_holding (partial PnL)
3. Option C: captures TP1 profit regardless, then adds a small scale-in position
4. Scale-in SL is proportional (RR-based), so even if it fails, the loss is small relative to TP1 profit

## Next Steps

1. Deploy MFE/MAE/TP1 data collection to VPS
2. Collect 2-4 weeks of real data
3. Re-backtest with actual MFE from candle data
4. If confirmed, implement Option C execution logic in traders

See also: [[ml-risk-scaling-vs-hard-blocking]], [[trading-strategies]]