---
name: v4-still-wins-threshold-tuning-modest
description: V4 beats V5 and mixed_v12 even after fixes; risk-scaling threshold tuning gives only modest gains — model quality is the bottleneck, not thresholds
metadata:
  type: project
---

# V4 Stays Production — Model Quality Beats Threshold Tuning

**Date**: 2026-06-28
**Session**: 46d8bb6f (Ralph Loop)
**Related**: [[mixed-v12-bxau-broken]], [[2026-06-20_v6-ml-model-training]], [[good-era-config-restore]]

## What was tested

Three independent backtests on data from 2025-10-01 onward, all on the same
BacktestEngine + V4 feature pipeline:

1. **V5 vs V4** (ISSUE-001) — V5 has `scale_pos_weight=true` for class imbalance
2. **mixed_v12 vs V4** (ISSUE-034, earlier) — V11 SELL + V12 BUY, bxau-fixed
3. **Risk-scaling thresholds** (ISSUE-002) — 6 (full,skip) pairs on V4

## Results at threshold 0.65 (hard block, V4 baseline)

| Account | V4 PF | V4 PnL | V5 PF | V5 PnL | mixed_v12 PF | mixed_v12 PnL |
|---------|-------|--------|-------|--------|--------------|---------------|
| A | 1.77 | $1,193 | 1.91 | $1,097 | 1.69 | $1,417 |
| B | 1.06 | $150 | 1.20 | $342 | 0.98 | -$46 |
| C | 1.44 | $694 | 1.22 | $290 | 1.28 | $635 |

V4 wins on C clearly, V5 wins on B clearly, mixed_v12 wins on A on PnL but
loses on PF. **No model consistently beats V4 across all three accounts.**

## Risk-scaling threshold tuning (V4, linear multiplier)

Best pair per account vs production (0.50, 0.85):
- A: (0.60, 0.95) → +$135 (PnL $1,285 vs $1,150)
- B: (0.40, 0.75) → +$216 (PnL $459 vs $243)
- C: (0.55, 0.85) → +$6 (noise)

Production (0.50, 0.85) is within ~10% of best on A and C. B would benefit
most from tighter thresholds, but the gain is modest (+$216 on $1k equity
over 8 months = +21.6%).

## Why this matters

**The bottleneck is model quality, not threshold tuning.** Three attempts to
replace V4 (V5, V6, mixed_v12) all fail to consistently beat it. The reasons:

1. **Training PF ≠ trading PF** — V11 SELL had training PF=3.0 (classification
   PF on wins/false_wins) but real trading PF only 1.28. The training objective
   optimizes classification, not PnL.
2. **V4 is robust** — 9 sub-models, 32 features, no bxau dependency, proven on
   Real-A real money. Hard to beat without a fundamentally better feature set.
3. **Threshold tuning is overfit** — different accounts want different pairs
   because their trade populations differ. Tuning to this 8-month window won't
   generalize.

## Decision

- **V4 stays production** for A + B/C/D. No replacement until a v6+ or V13
  model clears backtest PF > 1.77 at 0.65 with one-hot only features AND beats
  V4 on at least 2 of 3 accounts.
- **(0.50, 0.85) stays as risk-scaling default**. Per-account tuning is a
  future option if a v6+ model proves worth deploying.
- **The "training PF ≠ trading PF" problem is the real blocker.** Next model
  experiment should optimize a trading-PF-aware objective, not classification
  accuracy. Consider: custom xgboost objective that weights PnL, or train on
  trade outcomes labeled by realized PnL not win/loss.

## Lesson

When three attempts to replace a model all fail, the issue isn't the
challengers — it's the evaluation methodology. Training metrics that don't
correlate with trading metrics produce overfit models (V11 SELL PF=3.0 → 1.28).
Fix the objective before training more challengers.