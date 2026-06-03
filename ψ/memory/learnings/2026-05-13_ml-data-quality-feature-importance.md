# ML Data Quality & Feature Importance

**Date**: 2026-05-13
**Context**: Feature importance analysis on 1,540 trades + 7,456 snapshots. Discovered low-confidence trades are noise, deployed quality-optimized config, built trade outcome ML trainer.
**Source**: rrr: god-port-oracle

## Key Insight

Confidence threshold determines data quality dramatically:
- conf < 0.3: WR 29%, avg PnL -$1.04 (noise)
- conf 0.3-0.5: WR 33%, avg PnL -$0.19 (noise)
- conf 0.5-0.7: WR 59%, avg PnL +$8.37 (quality)
- conf > 0.7: WR 42%, avg PnL +$1.31 (overconfident, lower quality)

**Why**: Low confidence means the signal indicators are conflicting — the model is unsure because market conditions are ambiguous. These trades are noise for ML training.

**How to apply**: Only use trades with confidence >= 0.50 for ML training. The new config enforces this at the signal generation level.

## Feature Importance Consensus (3 methods agree)

Top 7 features that predict trade outcomes:
1. ichimoku_senkou_b — cloud support/resistance boundary
2. ichimoku_senkou_a — cloud upper boundary
3. ema_50 — medium-term trend anchor
4. sma_50 — same role, slightly less important than ema_50
5. ema_200 — long-term trend context
6. dema_21 — short-term momentum
7. sma_10 — short-term price reference

## Direction-Specific Features

- **BUY**: ema_200, obv, cmf (trend + money flow)
- **SELL**: vwap_offset_pct, ichimoku_kijun (mean reversion + cloud)
- **Trending**: sma_50, ichimoku cloud (price vs. levels)
- **Ranging**: mfi, tick_volume_ratio (volume/flow indicators)

## SELL is Fundamentally Harder

- BUY WR: 48.7% (+$4,630 total)
- SELL WR: 34.7% (-$560 total)
- SELL models: 28-34% test accuracy (untrainable with current data)
- **Why**: SELL signals have less distinctive features; need higher confidence threshold

## Config Changes Deployed

| Setting | Before | After |
|---------|--------|-------|
| MIN_CONFIDENCE_A | 0.35 | 0.50 |
| MIN_CONFIDENCE_B | 0.45 | 0.55 |
| MIN_CONFIDENCE_C | 0.60 | 0.65 |
| MAX_POSITIONS | 10/7/5 | 3/3/3 |
| LEARNING_MODE | 1 | 0 |

## ML Pipeline Built

- `broky/ml/trade_outcome_trainer.py`: Train on live trade outcomes (win/loss)
- Supports regime-specific (trending/ranging) and direction-specific (BUY/SELL) models
- Uses consensus top features + extended feature set
- Time-based train/test split (no random shuffle to prevent look-ahead)
- 8 models trained: overall, trending, ranging, BUY, SELL, trending_BUY, trending_SELL, ranging_BUY, ranging_SELL

## Overfitting Warning

CV accuracy (87-94%) massively overestimates test accuracy (28-56%) due to correlated time-series data. Adjacent trades share nearly identical market conditions. Need 10-14 more days of diverse data before models are usable.