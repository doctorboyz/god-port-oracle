# V5 Backtest — 2026-06-28

**ISSUE**: ISSUE-001 — Train v5 model with scale_pos_weight + deploy
**Model**: `data/models/trade_outcome_v5/` (9 sub-models, scale_pos_weight=true)
**Bar**: PF > 1.5 on B/C/D at threshold 0.65 (IRON LAW)
**Verdict**: ❌ NOT DEPLOYED — does not beat V4 consistently

## Results at threshold 0.65 (start 2025-10-01)

| Account | V5 PF | V5 PnL | V4 PF | V4 PnL | Winner |
|---------|-------|--------|-------|--------|--------|
| A | 1.91 | $1,097 | 1.77 | $1,193 | V5 (PF) / V4 (PnL) |
| B | 1.20 | $342 | 1.06 | $150 | **V5** |
| C | 1.22 | $290 | 1.44 | $694 | **V4** |

V5 has scale_pos_weight enabled which helps class imbalance but:
- On B, V5 clearly wins (PF 1.20 vs 1.06, PnL +$192)
- On C, V4 clearly wins (PF 1.44 vs 1.22, PnL +$404)
- On A, mixed signal — higher PF but lower PnL

Neither V5 nor V4 passes PF > 1.5 on B/C/D at threshold 0.65.

## Decision

V5 stays as a **reference model**, not deployed. V4 remains production for A + B/C/D.
The IRON LAW bar (PF > 1.5) is not met by V5 on B/C/D.

## Artifact

`result.txt` — full backtest output from
`python scripts/backtest_ml_filter.py --models data/models/trade_outcome_v5 --thresholds 0.55 0.65 0.75 --start 2025-10-01 --account all`

## Related

- [[mixed-v12-bxau-broken]] — V4 proven PF=1.77 A / 1.06 B / 1.44 C at 0.65
- [[2026-06-20_v6-ml-model-training]] — v6 with one-hot regime features (ISSUE-025)