---
name: mixed-v12-bxau-broken
description: mixed_v12 + V11 feature_engineer.joblib reference bxau module not in production → silent ML filter disable on B/C/D for weeks; reverted B/C/D to V4
metadata:
  type: project
---

# mixed_v12 bxau Dependency Bug

**Date**: 2026-06-28
**Severity**: CRITICAL — silent production ML filter disable
**Affected**: B/C/D demo accounts (oracle-engine-train container)

## Symptom

`scripts/backtest_ml_filter.py` against mixed_v12 @ threshold 0.65:
- 289/289 trades kept (NO filtering)
- PnL $1,333.55 unchanged from baseline
- "no prediction" 289 — every trade bypassed ML

B/C/D had been trading **without ML filter for weeks** in production.

## Root Cause

`data/models/trade_outcome_mixed_v12/feature_engineer.joblib` was trained with
`bxau.ml.features.FeatureEngineer` (user's separate package at
`/Users/doctorboyz/Code/github.com/doctorboyz/bxau/`). The pickle's
`load_stack_global` step imports `bxau` on unpickle → `ModuleNotFoundError: No
module named 'bxau'` because bxau is NOT installed locally or in the VPS
Docker image.

`broky/ml/trade_outcome_predictor.py` line 56-60 swallows the load failure
silently:
```python
self._engineer = None  # stays None on failure
self.enabled = ...  # may still be True but predictions all return None
```

V11's `feature_engineer.joblib` has the same bxau dependency — both V11 and
mixed_v12 engineers are unloadable in any environment without bxau.

**Why V12 BUY's engineer works**: V12 BUY was trained inside the god-port-oracle
repo using `broky.ml.features.FeatureEngineer` (no bxau). Its engineer loads
fine. But mixed_v12's engineer was copied from V11 (bxau-trained), not V12 BUY.

## Verification

Local + VPS both reproduce:
```
$ python3 -c "import joblib; joblib.load('data/models/trade_outcome_mixed_v12/feature_engineer.joblib')"
ModuleNotFoundError: No module named 'bxau'
```

V4 predictor loads cleanly:
```
ML_MODEL_DIR: /app/data/models/trade_outcome_v4
enabled: True, models: 9, engineer: broky.ml.features
health: True | OK (test prediction: 0.2x, P(LOSS)=78%)
```

## Fix Applied (2026-06-28)

Reverted B/C/D to V4 (proven PF=1.71 @ threshold 0.65) by editing
`docker-compose.vps.yml` line 173:
```yaml
- ML_MODEL_DIR=/app/data/models/trade_outcome_v4  # was: trade_outcome_mixed_v12
```

Deployed via `scp` + `docker compose up -d --force-recreate oracle-engine-train`.
Verified on VPS: 9 models loaded, engineer `broky.ml.features`, health OK.

V4 BUY models are weak (PF=0.5) but V4 is strictly better than no filter at all.

## Follow-up (2026-06-28 — DONE, mixed_v12 NOT worth deploying)

Tested whether fixing the bxau dependency would make mixed_v12 viable. Built
`scripts/rebuild_engineer_no_bxau.py` which creates a fresh
`broky.ml.features.FeatureEngineer` fit on 196,796 trade_outcomes from
oracle.db, producing 137/139 features V11 expects (only d1_close, h4_close
missing — filled with 0; h4_close has 0 importance, d1_close 0.76%).

Backtest `scripts/backtest_ml_filter.py --start 2025-10-01 --account all`:

| Account | Model | Thresh | Trades | Kept | PnL | WR | PF |
|---------|-------|--------|--------|------|-----|-----|-----|
| A | V4 | 0.65 | 64 | 45 | $1,193 | 46.7% | **1.77** |
| A | V4 | 0.75 | 64 | 52 | $1,442 | 46.2% | **1.84** |
| A | mixed_v12 | 0.65 | 64 | 61 | $1,417 | 44.3% | 1.69 |
| B | V4 | 0.65 | 75 | 50 | $150 | 36.0% | **1.06** |
| B | mixed_v12 | 0.65 | 75 | 67 | -$46 | 35.8% | 0.98 |
| C | V4 | 0.65 | 81 | 49 | $694 | 44.9% | **1.44** |
| C | mixed_v12 | 0.65 | 81 | 72 | $635 | 40.3% | 1.28 |

**V4 beats mixed_v12 on every account at threshold 0.65.** V11's training
PF=3.0 (classification PF, wins/false_wins) does NOT translate to better real
trading PF — V11 SELL models are overfit to training data.

**Decision**: V4 stays as production model for A + B/C/D. mixed_v12 abandoned.
No retrain needed — even a working mixed_v12 doesn't outperform V4.

The mixed_v12 directory + `build_mixed_v12_metadata.py` +
`rebuild_engineer_no_bxau.py` are kept as reference artifacts but NOT deployed.
To revisit: train a V13 with proper separation of training PF vs trading PF,
and only deploy if backtest PF > V4's 1.77 at threshold 0.65.

## Lesson

**Silent failure is the most expensive failure mode.** The predictor's
`self._engineer = None` on load failure should have raised or at minimum
logged at ERROR level with a health flag. B/C/D traded unfiltered for weeks
because the only signal was "no prediction" in debug logs nobody reads.

**Always verify model artifacts load in the deployment environment, not just
where they were trained.** A model that loads locally can still fail in
production if its pickle references modules not in the prod image.

## Related

- [[good-era-config-restore]] — same session, V4 proven PF=1.71
- [[sklearn-version-pinning-v4-deploy]] — earlier V4 deploy with sklearn pin
- [[v4-stable-on-real-a]] — V4 pinned on Real-A
- [[one-hot-regime-encoding]] — V4 uses regime_encoded compat shim