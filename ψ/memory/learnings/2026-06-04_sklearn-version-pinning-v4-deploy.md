---
name: sklearn-version-pinning-v4-deploy
description: sklearn version mismatch breaks model loading; always pin in Dockerfile; deploy verification must check model health
metadata:
  type: learning
  source: rrr: god-port-oracle
  date: 2026-06-04
---

# sklearn Version Mismatch Breaks Model Loading

**Problem**: Models trained with sklearn 1.8.0 fail to load on VPS with sklearn 1.9.0. Error: `No module named '_loss'`. This is because sklearn 1.9 reorganized internal module structure, breaking pickle compatibility.

**Fix**: Pin sklearn version in Dockerfile to match training environment:
```dockerfile
"scikit-learn>=1.3.0,<1.9.0" "xgboost>=2.0.0,<4.0.0"
```

**Why**: XGBoost's `GradientBoostingClassifier` (which wraps sklearn internals) stores references to `_loss` module that was reorganized between sklearn versions.

**Lesson**: Always verify model loading as part of deployment, not just container health. Add a smoke test that confirms models load and ML filter passes health check.

**How to apply**: 
- Add model loading verification to deploy script
- Pin ALL ML library versions in Dockerfile (sklearn, xgboost, numpy)
- Run a test prediction in the container before declaring deployment success

## Related

- [[volatile-threshold-tuning]] — volatile regime data was also a challenge in this session
- [[backtest-to-ml-pipeline]] — the pipeline that generated training data