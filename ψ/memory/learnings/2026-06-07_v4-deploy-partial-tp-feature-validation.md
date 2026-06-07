---
name: v4-deploy-partial-tp-feature-validation
description: v4 deployed to production, partial TP on Account C; feature pipeline needs single source of truth; manual deploy error-prone
metadata:
  type: learning
---

# v4 Deploy + Partial TP + Feature Validation Gaps

## What Happened

Session 2026-06-07: retrained v5 model, discovered `regime_encoded` missing from features pipeline, found ALL_FEATURE_COLS gap, compared v4 vs v5, deployed v4 to VPS with partial TP on Account C.

## Key Findings

1. **Feature pipeline lacks validation**: `_compute_feature_columns()` was missing `regime_encoded`, and `ALL_FEATURE_COLS` didn't include `ENCODED_FEATURES`. This is a recurring pattern — every time we add a feature, we must update multiple lists. **Should have a single source of truth** that all other lists derive from.

2. **v4 outperforms v5 at all thresholds**: v4 has 10 models (incl. regime_volatile), predictions more varied (not constant ~0.78), blocks more losses (blocked WR 29-32% vs v5's 35-37%).

3. **h4_trend data gap**: Historical `features_json` doesn't contain `h4_trend` field, so `h4_trend_encoded` can't be computed for past trades. Need to accumulate live data before v6 retraining.

4. **tp1_ratio hardcoded in 3 trader files**: `0.5` instead of `self.risk.tp1_ratio`. Parameter values must come from config, not magic numbers.

5. **Manual deployment is error-prone**: scp didn't update `trade_outcome_trainer.py` correctly on first attempt. Need `scripts/deploy_vps.sh` with built-in verification.

## VPS Status (verified 2026-06-07 19:00)

- All 4 containers healthy (mt5a, mt5b, mt5c, oracle-engine)
- v4 model loaded: 10 models ✅
- ML filter enabled on all traders ✅
- Partial TP enabled on Account C (PARTIAL_TP_ENABLED_C=1) ✅
- 3 open BUY positions across all accounts with tp1_price set on Account C ✅
- DB schema has all new columns (tp1_price, parent_trade_id, atr_multiplier, etc.) ✅

## Action Items

- [ ] Add feature count validation test
- [ ] Create `scripts/deploy_vps.sh` with verification
- [ ] Collect h4_trend in features_json for future v6 retraining
- [ ] Monitor Account C partial TP vs A/B after market opens

See also: [[ml-v4-vs-v5-backtest]], [[partial-tp-bug-fix-and-analysis]], [[data-integrity-backfill-chain]]