---
name: ml-data-pipeline-return-values
description: ML predictor return values must include all data callers need — instance variable hacks create hidden coupling and race conditions
metadata:
  type: learning
  date: 2026-06-05
---

# ML Data Pipeline: Return Values > Instance Variables

**Lesson**: When a function computes data that multiple callers need, return it as part of the return value. Don't use instance variables for cross-call communication.

**What happened**: `get_risk_multiplier()` returned only `(multiplier, reason)` but traders needed `loss_proba` and `model_used` too. A `_last_loss_proba` instance variable hack was used but never reliably populated, causing `ml_loss_proba` and `ml_model_used` to always be NULL in the database.

**Root cause**: Instance variables for computed data create hidden coupling — callers don't know they need to check `_last_loss_proba`, and the variable can be stale or None between calls.

**Fix**: Changed signatures to:
- `predict_loss_proba() → tuple[Optional[float], Optional[str]]` (proba, model_name)
- `get_risk_multiplier() → tuple[float, str, Optional[float], Optional[str]]` (multiplier, reason, loss_proba, model_used)

**Why it matters**: Without proper ML metadata in DB, model retraining would have incomplete data. The bug was invisible — no error, just NULL columns.

**How to apply**: When adding a new piece of computed data that callers need, always return it. Never use `_last_X` instance variable patterns for data that needs to flow to callers.

**Related**: [[ml-risk-scaling-vs-hard-blocking]], [[counter-trend-bollinger-mean-reversion]]