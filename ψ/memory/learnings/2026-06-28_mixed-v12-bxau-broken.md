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

## Follow-up (NOT done — needs ISSUE-034)

To restore the original mixed_v12 intent (V11 SELL PF=3.0 + V12 BUY), retrain
**without bxau**:
1. Retrain V11 SELL models using `broky.ml.features.FeatureEngineer` (in this
   repo, not bxau). Same hyperparams, same data, just different engineer class.
2. Retrain V12 BUY models (already use broky.ml — but verify).
3. Rebuild mixed_v12 directory with new V11 SELL + V12 BUY models + a fresh
   `feature_engineer.joblib` from broky.ml.features.
4. Backtest mixed_v12 vs V4 — only deploy if PF > V4's 1.71.
5. Update `scripts/build_mixed_v12_metadata.py` to use new metadata.

User directive: "ไม่ควรมี bxau เพราะแยกกันเด็ดขาดออกไปล้ว" — bxau must be
fully removed from god-port-oracle's ML pipeline. It's a separate personal
package and must never be a production dependency.

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