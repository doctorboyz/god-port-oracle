# Trend Filter Asymmetry: Protecting One Side While Exposing the Other

**Date**: 2026-05-27
**Source**: 8 losing BUY trades (-$126.33) in a market that was falling while D1 trend said "bullish"

## Pattern

Hard trend filters create asymmetric risk:
- D1 bullish → SELL blocked (good: prevents counter-trend shorts)
- D1 bullish → BUY allowed (bad: no protection against failing longs in a turning market)

The slower the trend indicator (D1 EMA 50/200 takes weeks to flip), the more severe the asymmetry — the market can move 1-3% before the filter acknowledges the trend change.

## What We Learned

1. **H4 override is necessary but not sufficient**: H4 EMA 10/50 flips ~4x faster than D1, but still lags real price action by ~20 hours during fast moves. A pure price-momentum filter (e.g. "price dropped >1% in 24h → reduce BUY confidence") would catch turns even faster.

2. **ML compounds the problem when trained on regime-specific data**: v2.best models were trained mostly on bullish D1 period → P(LOSS) is artificially high for SELL even after D1 flips bearish. Regime-aware model selection or retraining is needed.

3. **Silent ML crashes = silent capital destruction**: The ML filter crash (missing `get_risk_multiplier()`) went undetected for days while trades opened without protection. Health checks must verify functionality, not just loading.

## Application

- Add trend flip alerts (Telegram notification on D1/H4 direction change)
- Add price momentum overlay: if 24h change conflicts with D1 trend, flag it
- Consider regime-aware ML: different thresholds or models for D1 bullish vs bearish
- Add ML health check that runs a test prediction and verifies output
