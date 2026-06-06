---
name: counter-trend-bollinger-mean-reversion
description: Counter-trend logic: hard block when H4 overrides D1, allow mean-reversion at Bollinger extremes with reduced size
metadata:
  type: learning
  date: 2026-06-05
---

# Counter-Trend Logic: Trend-Following Primary + Mean-Reversion Exception

**Decision**: When H4 trend conflicts with D1 trend (h4_override=True):
1. **Hard block** counter-trend signals (`trend_mult=0.0`)
2. **Allow mean-reversion** at Bollinger extremes:
   - BUY counter-trend if oversold (`band_position ≤ 0.15`) → `trend_mult=0.3`
   - SELL counter-trend if overbought (`band_position ≥ 0.85`) → `trend_mult=0.3`

**Rationale**:
- Trend-following is primary — don't fight the shorter-term trend
- Mean-reversion at extremes has higher probability because price tends to revert from bands
- TP/SL provides protection on smaller positions (trend_mult=0.3 → reduced lot size)
- `trend_mult=0.3` combined with `min_confidence=0.3` means only strong signals pass

**Evolution**: This went through 3 iterations:
1. `trend_mult=0.5` (original: reduce confidence by half)
2. `trend_mult=0.0` (hard block all counter-trend)
3. `trend_mult=0.0` + Bollinger exception (current: block unless extreme)

**Data point**: Paper trade Jun 5 showed 4/8 counter-trend trades lost, confirming that blocking most counter-trend is safer.

**Related**: [[ml-data-pipeline-return-values]], [[trend-filter-asymmetry]]