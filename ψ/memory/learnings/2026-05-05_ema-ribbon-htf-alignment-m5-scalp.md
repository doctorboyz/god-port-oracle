---
name: ema-ribbon-htf-alignment-m5-scalp
description: 6-EMA Ribbon Cloud strategy, HTF alignment (H4/D1), and M5 scalp system design for XAUUSD
type: project
---

# EMA Ribbon + HTF Alignment + M5 Scalp — Key Learnings

## 6-EMA Ribbon Cloud (Most Professional XAUUSD Scalp Strategy)

- EMA periods: **8, 13, 21, 34, 55, 89** (Fibonacci sequence)
- 3 cloud layers: Fast (8-21), Mid (21-55), Slow (55-89)
- **Buy signal:** All 6 EMAs bullish + price pulls back to fast cloud + confirmation candle closes above EMA 8 + ribbon expanding
- **Sell signal:** Mirror
- **Signal scoring:** Rate each signal on ribbon expansion, ATR level, session position, pullback depth — only take top 2-3 per session
- **Avoid:** Tangled ribbon (chop), counter-HTF direction, within 15 min of news, spread > 25 pips

## 4-Level ATR-Based TP (Position Scaling)

| Level | Distance | Close % | Action |
|-------|----------|---------|--------|
| TP1 | 1.0x ATR | 40% | Quick scalp |
| TP2 | 1.5x ATR | 25% | SL to breakeven |
| TP3 | 2.5x ATR | 25% | Trail stop |
| TP4 | 4.0x ATR | 10% | Runner |

**SL:** Below slow cloud (EMA 55-89) ≈ 1.2-1.5x ATR. Never widen after entry.

## HTF Alignment (H4 + D1) — 10-15% WR Improvement

- **D1 200 EMA:** Macro trend direction (already implemented as `d1_trend`)
- **H4 10/200 EMA crossover:** Intermediate trend confirmation (NEW — need to add)
- **Rule:** Only trade when D1 AND H4 agree on direction
- Without HTF filter: ~45% WR. With filter: ~55-60% WR.

**Why:** We already have `d1_trend` in `generate_signal()`. Need to add H4 alignment as an additional filter. If d1_trend and h4_trend disagree → HOLD.

## M5 vs M1 (Professional Consensus)

- **M5 is the sweet spot** — M1 is too noisy ($2-5 per candle during London)
- M5: 20-40 trades/day, 10-20 pip targets, 55-60% WR
- M1: 50-100 trades/day, 5-10 pip targets, 40-50% WR (spread eats profits)
- Recommendation: Add M5 scalp as a new mode (`TradingMode.M5_SCALP`)

## Proposed M5 Scalp Parameters

| Parameter | M1 (current) | M5 (proposed) |
|-----------|-------------|---------------|
| ADX period | 7 | 14 |
| ADX threshold | 10 | 15 |
| Min confidence | 0.45 | 0.50 |
| Direction threshold | 0.15 | 0.20 |
| Circuit breaker | 5 losses | 3 losses |
| Cooldown | 3 min | 25 min (5 bars) |
| TP | Fixed 1.5x ATR | 4-level ATR scaling |

**How to apply:** Implement M5_SCALP as a new trading mode with 6-EMA ribbon signal generation and H4+D1 trend alignment filter.