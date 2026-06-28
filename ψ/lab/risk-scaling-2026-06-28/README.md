# Risk-Scaling Threshold Backtest — 2026-06-28

**ISSUE**: ISSUE-002 — Backtest optimal risk-scaling thresholds
**Current prod**: (full_size=0.50, skip=0.85) chosen by intuition
**Model tested**: V4 (production)
**Period**: 2025-10-01 onward
**Method**: For each trade, compute P(LOSS) via V4 predictor, then linear-scale
PnL by multiplier:
- `P(LOSS) ≤ full`: multiplier = 1.0
- `P(LOSS) ≥ skip`: multiplier = 0.0 (skip)
- between: `(skip - p) / (skip - full)`

## Results — best pair per account (by scaled PnL)

| Account | Best (full,skip) | PnL | PF | Participation | vs prod (0.50,0.85) |
|---------|------------------|--------|------|---------------|---------------------|
| A | (0.60, 0.95) | $1,285 | 1.71 | 82% | +$135 |
| B | (0.40, 0.75) | $459 | 1.24 | 58% | +$216 |
| C | (0.55, 0.85) | $627 | 1.35 | 69% | +$6 (noise) |

**Production (0.50, 0.85) is NOT optimal for any account.** Each account wants
different thresholds:
- **A is profitable** → looser thresholds → participate more (82%)
- **B is the weakest** → tighter thresholds → skip more losers (58%)
- **C is mid** → current default is fine

## Recommendation

**Per-account thresholds** would help, especially for B (+$216 on a $1k equity
= +21.6% over the period). However:

1. The gains are modest compared to model quality (V4 vs V5 vs V6 matters more).
2. The thresholds are overfit to this 8-month window — different periods may
   prefer different pairs.
3. Account A's "loose" preference is partly because A's trades are already
   filtered by higher min_confidence (0.35) — wait, A has the LOOSEST confidence
   threshold, so it sees more trades, and V4 likes them.

**Decision**: keep (0.50, 0.85) as the default for now. The backtest proves it
isn't far from optimal (within ~10% of best on A and C). The real bottleneck is
model quality, not threshold tuning. Revisit when a v6+ model replaces V4.

If we do tune, B would benefit most from (0.40, 0.75) — that's the clearest win.

## Artifact

`result.txt` — full output from
`python scripts/backtest_risk_scaling.py --model data/models/trade_outcome_v4 --pairs 0.45,0.80 0.50,0.85 0.55,0.85 0.50,0.90 0.40,0.75 0.60,0.95 --start 2025-10-01 --account all`

## Related

- [[mixed-v12-bxau-broken]] — V4 production baseline
- [[2026-06-20_v6-ml-model-training]] — v6 with one-hot regime features