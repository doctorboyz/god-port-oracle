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

## Backtest verification (2026-06-28, ISSUE-025 IRON LAW)

Backtested v6 against V4 production on real B/C/D data (2025-10-01 onward).
Full results in ψ/lab/v6-backtest-2026-06-28/.

| Account | v6 best PF | v6 best PnL | V4 best PF | V4 best PnL | Winner |
|---------|-----------|-------------|-----------|-------------|--------|
| A | 2.05 (@0.70) | $1,531 | 1.77 (@0.65) | $1,193 | **v6** |
| B | 1.05 (@0.70) | $92 | 1.06 (@0.65) | $150 | V4 (slight) |
| C | 1.69 (@0.60) | $722 | 1.44 (@0.65) | $694 | **v6** |

**IRON LAW bar (PF>1.5 on B/C/D data)**: met on C (PF 1.69) and A (PF 2.05).
NOT met on B (max 1.05) — but no model passes PF>1.5 on B because B's unfiltered
baseline is PF 0.87. V4=1.06, V5=1.20, mixed_v12=0.98 on B — all fail the bar.

**Decision**: v6 is a verified viable model. V4 stays production on B/C/D for
now (proven track record, v6 gains are modest). v6 is the verified backup if
V4 degrades. NOT deployed to A per IRON LAW.

## Five retrain experiments (2026-06-28, ISSUE-025 deep dive)

Five attempts to beat v6 original — all failed.

| Variant | What changed | A PF | B PF | C PF | vs v6 orig |
|---------|--------------|------|------|------|------------|
| v6 (orig) | baseline | 2.05 | 1.05 | 1.69 | — |
| v6_consensus | 7 features only | 1.59 | 0.87 | 1.30 | worse |
| v6_ext | 139 features | 1.65 | 1.06 | 1.33 | worse |
| v6_tuned | tuned xgb hyperparams | 1.81 | 0.96 | 1.40 | worse |
| v6_pnlw | PnL-magnitude sample weighting | 1.65 | 1.06 | 1.33 | worse |

**Key finding**: The trading-PF-aware objective (PnL-magnitude weighting) —
the approach I kept saying was the real blocker — was tried and made things
worse. Upweighting high-volatility trades amplifies noise, not signal.

**Real bottleneck** (now confirmed across 5 experiments):
1. B's unfiltered baseline PF=0.87 — trades themselves are bad, no model can
   fix bad trades, only skip them
2. No B/C/D-specific training data (account 0 synthetic + 19 account 3 trades)
3. Training/backtest config mismatch — model trained on one config, evaluated
   on three different account configs

**Lesson**: When five attempts to beat a model all fail across feature count,
hyperparams, AND training objective, the issue is the data, not the model.
B is fundamentally weak in this period. No retrain variant can lift combined
B+C PF above 1.23. The per-account bar (PF>1.5 on C) IS met. The strict
combined bar is structurally unreachable with current data.

## Account-specific training data experiment (2026-06-28, 7th experiment)

Re-backfilled with account-specific TP/SL multipliers to match each account's
actual trade geometry (B: 6.25x/2.5x ATR, C: 4x/2x ATR). Trained v6_bspec and
v6_cspec on these account-specific datasets.

| Account | v6 (orig on acct 0) | v6_xspec (account-specific) | Winner |
|---------|---------------------|------------------------------|--------|
| B | 1.05 | 0.82 | **v6 orig** |
| C | 1.69 | 1.24 | **v6 orig** |

**Account-specific training made things WORSE.** The original 2x/1x ATR
labeling is a moderate threshold that produces balanced labels (50.7% WR)
capturing a general "will price move favorably" signal. Account-specific
labeling asks a harder, noisier question — larger thresholds mean rarer TP
hits, so labels are noisier and the model can't learn the signal as well.

**Final lesson (7 experiments)**: The original v6 setup (65 features, default
hyperparams, standard weighting, account-0 synthetic data with 2x/1x ATR
labeling) is a sweet spot. Every "improvement" — more/fewer features, tuned
hyperparams, PnL weighting, account-specific data — reduces generalization.
The 2x/1x ATR labeling is a good general-purpose proxy that doesn't need to
match account geometry exactly. The real bottleneck for B is the trades
themselves (unfiltered PF=0.87), not the model.