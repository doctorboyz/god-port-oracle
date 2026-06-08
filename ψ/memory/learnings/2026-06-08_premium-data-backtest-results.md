---
name: premium-data-backtest-results
description: Backtest results on premium M5 data (2024-01 to 2026-04) — reversal signal verification
metadata:
  type: project
  created: 2026-06-08
---

# Premium Data Backtest Results

## Data Source
- **Premium M5 data**: data/xau-data/XAUUSD_M5*.csv (~200k candles, 2023-06 to 2026-04)
- **H4/D1**: data/xau-data/XAUUSD_H4*.csv, XAUUSD_D1*.csv (2009 to 2026-04)

## Backtest Config
- Period: 2024-01-01 → 2026-04-30 (~162k M5 candles after warmup)
- Strategy: swing (indicator-based, min_confidence=0.55)
- ATR multiplier: 2.0, Risk-reward: 2.5, Risk per trade: 1%
- Starting equity: $1,000

## Results Summary

| Category | Trades | WR% | PnL | Avg PnL |
|----------|--------|-----|-----|---------|
| Trend-aligned | 320 | 32.5% | +$550.88 | +$1.72 |
| Counter-trend | 393 | 30.0% | +$339.13 | +$0.86 |
| Reversal | 0 | — | — | — |
| **TOTAL** | **713** | **31.1%** | **+$890** | +$1.25 |

## Key Findings

1. **Reversal = 0 trades** — No counter-trend trade met both OB/OS + divergence thresholds simultaneously. The reversal detection is too strict for XAUUSD's characteristics.
2. **Trend-aligned > Counter-trend** — 32.5% vs 30.0% WR, but difference is modest. Both profitable at RR=2.5.
3. **Regime**: Trending WR=32.8%, Ranging WR=29.2%, Volatile **loses money** (-$94)
4. **D1 was mostly bullish** → all SELL signals were counter-trend (393 trades)
5. **VPS verification (4,827 trades)** showed reversal WR=31.5% was worst category — consistent with ML learning to AVOID reversal

## Implication for ML v6

- `trend_alignment` feature still valuable: clearly separates trend-aligned (32.5%) from counter-trend (30.0%)
- `has_reversal` feature needs threshold tuning — 0 trades detected means thresholds are too strict
- Consider lowering OB/OS thresholds (e.g., RSI>65/<35 instead of 70/30) for XAUUSD
- Volatile regime should reduce confidence or skip trading

## Related
- [[trading-philosophy-trend-following]] — indicator priority and trend-following rules
- [[volatile-regime-threshold-fix]] — BW threshold fix for M5