---
name: mixed-model-scalp-swing-analysis
description: Mixed model analysis — V11 dominates SELL, V6 best for BUY, no scalp data yet
metadata:
  type: project
---

# Mixed Model & Scalp vs Swing Analysis

## Sub-Model Performance Summary

### V4 (10 models, feature_set=direction_specific)
| Model | PF | Accuracy | WR | Notes |
|-------|-----|----------|-----|-------|
| trending_SELL | **3.00** | 0.687 | 0.335 | 🏆 Crown jewel |
| overall | 1.00 | 0.631 | 0.423 | Meh |
| direction_SELL | 1.00 | 0.607 | 0.378 | Decent |
| regime_trending | 0.57 | 0.582 | 0.406 | Weak |
| regime_ranging | 0.70 | 0.427 | 0.455 | Weak |
| direction_BUY | 0.52 | 0.363 | 0.469 | ❌ Trash |
| trending_BUY | 0.50 | 0.375 | 0.480 | ❌ Trash |
| ranging_BUY | 0.59 | 0.593 | 0.447 | Weak |
| ranging_SELL | 0.28 | 0.220 | 0.464 | ❌ Trash |

### V6 (12 models, feature_set=extended, live_weight=3.0)
| Model | PF | Accuracy | WR | Notes |
|-------|-----|----------|-----|-------|
| overall | 1.32 | 0.499 | 0.521 | Better than V4 |
| trending_BUY | **1.33** | 0.458 | 0.530 | 🏆 Best BUY |
| regime_trending | 1.30 | 0.459 | 0.525 | Good |
| regime_ranging | 1.13 | 0.497 | 0.516 | OK |
| direction_BUY | 1.15 | 0.527 | 0.525 | Decent |
| ranging_BUY | 1.07 | 0.509 | 0.521 | OK |
| direction_SELL | 1.03 | 0.525 | 0.465 | Weak |
| trending_SELL | 0.99 | 0.494 | 0.448 | ❌ |
| ranging_SELL | 0.93 | 0.502 | 0.477 | ❌ |
| regime_volatile | 0.73 | 0.471 | 0.501 | ❌ |
| volatile_BUY | 0.72 | 0.470 | 0.502 | ❌ |
| volatile_SELL | 0.67 | 0.400 | 0.473 | ❌ |

### V11 (8 models, feature_set=extended+optuna, SELL-only direction models)
| Model | PF@0.55 | Accuracy | WR | Notes |
|-------|---------|----------|-----|-------|
| volatile_SELL | **3.18** | 0.803 | 0.432 | 🔥🔥 Best model |
| regime_volatile | 2.93 | 0.799 | 0.432 | 🔥🔥 |
| overall | 2.71 | 0.681 | 0.459 | 🔥🔥 |
| direction_SELL | 2.66 | 0.683 | 0.459 | 🔥 |
| trending_SELL | 2.39 | 0.706 | 0.453 | 🔥 |
| ranging_SELL | 2.38 | 0.684 | 0.477 | 🔥 |
| regime_trending | 2.34 | 0.707 | 0.453 | 🔥 |
| regime_ranging | 2.47 | 0.695 | 0.477 | 🔥 |

## Mixed Model Strategy

### Optimal Selection (by regime + direction)

| Scenario | Best Model | Source | PF | Why |
|----------|-----------|--------|-----|-----|
| SELL + trending | trending_SELL | V11 | 2.39@0.55 | V11 dominates all SELL |
| SELL + ranging | ranging_SELL | V11 | 2.38@0.55 | V4 ranging_SELL PF=0.28 |
| SELL + volatile | volatile_SELL | V11 | 3.18@0.55 | Best model period |
| SELL (fallback) | direction_SELL | V11 | 2.66@0.55 | Solid fallback |
| BUY + trending | regime_trending | V11 | 2.34@0.55 | V11 overall > V6 BUY |
| BUY + ranging | regime_ranging | V11 | 2.47@0.55 | V11 overall > V6 BUY |
| BUY + volatile | regime_volatile | V11 | 2.93@0.55 | V11 overall > V6 BUY |
| BUY (fallback) | direction_BUY | V6 | 1.15 | Only viable BUY-specific |
| overall (fallback) | overall | V11 | 2.71@0.55 | Best overall model |

**Key insight**: V11's regime models (trained on mixed BUY+SELL data) are BETTER for BUY predictions than V6's direction-specific BUY models. V11 acc=0.68-0.80 vs V6 acc=0.46-0.53.

### Implementation: V11 + V6 BUY fallback

The current predictor fallback chain is:
1. `{regime}_{direction}` → 2. `direction_{direction}` → 3. `regime_{regime}` → 4. `overall`

For V11:
- SELL signals hit step 1 (trending_SELL, etc.) → best models
- BUY signals skip steps 1-2 (no BUY direction models) → fall back to step 3 (regime_trending, etc.) → still excellent

**V11 alone handles both BUY and SELL well.** The only gap is no BUY-specific direction models. We should train these.

## Scalp vs Swing

**Cannot separate yet** — insufficient data:
- trade_outcomes: 98,389 rows, ALL `premium_backfill` mode
- live_trades: 20 closed trades, ALL `swing` mode
- Only 19 `swing` mode rows in trade_outcomes, 0 `scalp` rows

**Minimum requirements for scalp model**: ~2,000 closed scalp trades with features_json

**Why:** [[training-data-minimum]] — need min 2000 samples per sub-model for reliable XGBoost training. Current scalp data = 0.

**Until scalp data accumulates:** Both M5ScalpTrader and LiveTrader use the same mixed model.

## Why V4 trending_SELL PF=3.0 vs V11 trending_SELL PF=2.39

Different PF calculation methods:
- V4 PF = traditional (total_wins / total_losses) — includes all trades regardless of confidence
- V11 PF = threshold-based (at confidence ≥ 0.55, only count trades where model predicted correctly)

V11's PF@0.55=2.39 means "when model predicts LOSS with ≥55% confidence, the profit factor is 2.39" — this is more conservative and practical than V4's raw PF.

At threshold 0.50 (include all predictions), V11 trending_SELL PF=2.20 — still strong but lower than V4's 3.0. The difference is that V11 is trained on more data with better features and Optuna tuning.

## Action Items

1. **Deploy V11 to demo accounts (B, C, D)** immediately — best model for both BUY and SELL
2. **Keep V4 on Account A (real)** — per user decision, don't change until demo proves better
3. **Train V11 BUY sub-models** — use same V11 feature set + Optuna on BUY data only
4. **Accumulate scalp data** — label live trades by trading_mode for future scalp model training
5. **After 4 weeks demo**: compare V11 vs V4 live results on demo accounts

## Related

- [[v4-stable-on-real-a]] — V4 pinned on real account
- [[ml-data-quality-feature-importance]] — BUY/SELL need separate models
- [[trend-filter-asymmetry]] — D1 bullish → BUY bias