---
name: multi-config-backtest-comparison
description: Backtest comparison of Account A/B/C configs on premium M5 data (2024-01 to 2026-04)
metadata:
  type: project
  created: 2026-06-08
---

# Multi-Config Backtest Comparison (Premium Data)

## Data Source
- **Premium M5**: data/xau-data/XAUUSD_M5*.csv (~162k candles, 2024-01 to 2026-04)
- **Pre-processed parquet**: data/processed/xauusd_m5_indicators.parquet (with all indicators + reversal features)

## Results Summary

| Config | Trades | WR% | PnL | PF | Final Equity |
|--------|--------|-----|-----|----|-------------|
| A (ATR=3, RR=3, conf≥0.35) | 824 | 31.1% | +$612 | 1.06 | $1,612 |
| **B (ATR=2.5, RR=2.5, conf≥0.45)** | **767** | **33.0%** | **+$1,382** | **1.15** | **$2,382** |
| C (ATR=2, RR=2, conf≥0.6) | 639 | 31.9% | -$334 | 0.96 | $666 |

## Key Findings

1. **Account B ดีที่สุด** — RR=2.5 สมดุลกับ WR≈33% → PF=1.15, ทำกำไรดีสุด
2. **Account C ขาดทุน** — RR=2 ต้องการ WR≥33.3% แต่ได้แค่ 31.9%
3. **Reversal = 0 trades ทุก config** — reversal thresholds แข็งเกินไปสำหรับ XAUUSD
4. **Trend-aligned > Counter-trend** — 37-39% vs 25-28% WR ทุก config

## ML Implications
- Account B's config (ATR=2.5, RR=2.5, conf≥0.45) เป็น sweet spot สำหรับ XAUUSD
- v4 vs v5: v5 เพิ่ม `regime_encoded` และ `mfi_signal_encoded`, ลบ `price_vs_cloud`
- v5 trending_SELL CV=82.3% ดีกว่า v4 CV=78.7% → v5 ดีกว่าบน SELL trending
- แต่ v5 overall test accuracy ต่ำกว่า (41.6% vs 63.1%) → overfitting risk

## Processed Data Files
- `data/processed/xauusd_m5_indicators.parquet` — M5 + all indicators + reversal features (200k rows)
- `data/processed/xauusd_h4_trend.parquet` — H4 with EMA trend (26.8k rows)
- `data/processed/xauusd_d1_trend.parquet` — D1 with EMA trend (5.2k rows)

## Related
- [[premium-data-backtest-results]] — Initial reversal signal backtest
- [[trading-philosophy-trend-following]] — Trend-following rules and indicator priority