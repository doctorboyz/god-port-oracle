---
name: v6-ml-model-training
description: v6 ML model trained on 98K premium backfill data with ATR-based labeling
metadata:
  type: project
---

# v6 ML Model Training Results

## What was done
- Backfilled 98,389 trade outcomes from premium M5 data (200K candles, 2023-06 to 2026-04)
- ATR-based dynamic labeling (2x ATR TP threshold, 1x ATR SL threshold)
- Signal quality filter (ADX > 18, DI+momentum+trend direction scoring, counter-trend filter)
- Trained 12 XGBoost models (overall, regime×3, direction×2, regime×direction×6)

## Key results (overall model)
- Test accuracy: ~50% (near random — expected for M5 XAUUSD)
- **As confidence filter at P(WIN)≥0.55: 58.3% WR, PF 1.40**
- **As confidence filter at P(WIN)≥0.60: 63.8% WR, PF 1.76**
- **As confidence filter at P(WIN)≥0.65: 66.7% WR, PF 2.00**

## Why accuracy alone is misleading
With ~52% baseline WIN rate, ~50% test accuracy means the model can't distinguish WIN/LOSS on most bars. But **calibration matters more than accuracy** — when the model is confident (P(WIN)>0.55), it's right significantly more often than baseline.

## Best models for live trading
1. **overall** (98K samples) — use as fallback
2. **direction_BUY** (91K) — best for BUY signals, PF 1.15
3. **trending_BUY** (50K) — PF 1.33, good for trending regime

## What to avoid
- volatile_SELL: only 150 samples, unreliable
- SELL models generally: small sample size (7K total), lower confidence

## How to apply
- Use `get_risk_multiplier()` for position sizing (gradual scaling from 1.0 to 0.0)
- Use `should_skip()` with loss_threshold=0.65 as hard filter
- The model works best as a **confidence filter**, not a pure classifier

## Next steps
- Collect live trade data to supplement synthetic labels
- Consider regression model (predict profit_pct) for richer signal
- Add candle pattern features for more predictive power