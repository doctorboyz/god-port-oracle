---
name: ml-v4-vs-v5-backtest
description: v4 outperforms v5 at all thresholds; partial TP enabled on Account C; VPS switched to v4 model
metadata:
  type: learning
---

# ML v4 vs v5 Backtest Results + Config Changes

## What Changed

1. **Fixed `_compute_feature_columns()` in features.py** — was missing `regime_encoded`, now includes all 5 encoded categoricals
2. **Added ENCODED_FEATURES to ALL_FEATURE_COLS** — direction-specific models now use 33 features instead of 26
3. **Retrained v5** — 9 models, all now include `regime_encoded` and `mfi_signal_encoded` (but not `h4_trend_encoded` — historical data doesn't have it)
4. **Created `scripts/backtest_ml_filter.py`** — compare ML models + thresholds in backtest
5. **VPS config**: `ML_MODEL_DIR` → v4, `PARTIAL_TP_ENABLED_C` → 1

## Backtest Results (v4 vs v5, thresholds 0.65-0.70)

| Account | Model | Thresh | PnL | WR | PF | Δ PnL |
|---------|-------|--------|-----|-----|-----|-------|
| A | **v4** | 0.70 | $2,070 | 51.6% | 1.73 | +$736 |
| A | v5 | 0.70 | $1,540 | 45.8% | 1.57 | +$206 |
| B | **v4** | 0.65 | $904 | 43.6% | 1.25 | +$1,483 |
| B | v5 | 0.65 | $361 | 35.7% | 1.10 | +$941 |
| C | **v4** | 0.70 | $1,035 | 42.2% | 1.31 | +$754 |
| C | v5 | 0.65 | $386 | 37.1% | 1.12 | +$105 |

## Why v4 Wins

- v4 predictions are more varied (not constant ~0.78-0.82) → separates loss trades better
- v5's blocked trade WR is 35-37% vs v4's 29-32% → v4 blocks more losses
- v5 doesn't have `h4_trend` in historical features_json → can't learn that pattern
- v5's `regime` comes from live_trades column (backfilled) not features_json → less reliable

## Partial TP (Option C) on Account C

- Backtest estimation shows mixed results (scale-in SL very tight)
- Real data needed → A/B test on Account C only
- `PARTIAL_TP_ENABLED_C=1` in docker-compose.vps.yml

## Next Steps

- Collect live data with regime + h4_trend in features_json
- Retrain v6 when >500 trades with complete features
- Monitor Account C partial TP performance vs A/B (no partial TP)

See also: [[partial-tp-bug-fix-and-analysis]]