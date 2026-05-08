---
name: scalping-best-practices-xauusd
description: Key scalping parameters and best practices for XAUUSD from multiple professional sources
type: project
---

# XAUUSD Scalping Best Practices (from research 2026-05-05)

## Critical Parameters (Validated by 4+ Professional Sources)

| Parameter | Best Practice | Our Value | Status |
|-----------|--------------|-----------|--------|
| Sessions | London+Overlap+NY | London+Overlap+NY | **Fixed** (was London+Overlap only) |
| ADX threshold (M1) | 10-15 | 10 | **Fixed** (was 15) |
| Min confidence | 0.40-0.55 | 0.45 | **Fixed** (was 0.55) |
| Direction threshold (M1) | 0.15-0.25 | 0.15 | **Fixed** (was 0.30) |
| Max spread | 25-35 pts | 35 pts | **Fixed** (was 30) |
| Risk per trade | 1% | 1% | Matches |
| Daily loss limit | 3% or 3 consecutive losses | 3% or 5 consecutive | Consider lowering to 3 |
| Cooldown after loss | 3 candles (3 min on M1) | 3 min | Matches |
| R:R minimum | 1:1.5 | 1.5 | Matches |

**Why:** NY session (16:00-22:00 UTC) is one of the most liquid periods for XAUUSD. Blocking it removed ~40% of tradeable hours. ADX(7) on M1 produces very noisy readings; threshold of 15 blocks too many valid setups.

## Key Insight: M5 May Be Better Than M1

M1 produces 50-100 signals/day but with extreme noise ($2-5 per candle during London). M5 produces 20-40 cleaner signals. Our current system uses M1 — consider adding M5 as an alternative or hybrid mode.

**Why:** Gold volatility on M1 causes frequent whipsaws. M5 is the professional consensus "sweet spot."

## 3-Confirmation Rule

Never enter on a single indicator. Require minimum 3 confirmations:
1. Trend direction (EMA cross or ribbon)
2. Momentum (MACD histogram or RSI)
3. Volume/confirmation (volume ratio or price action)

**How to apply:** Our weighted score already requires multiple indicators to agree for high confidence, but we could add explicit minimum indicator count.

## ATR-Based SL/TP > Fixed Pips

Gold volatility changes dramatically throughout the day. Fixed pip SL/TP doesn't adapt.
- SL: 1.0-1.5x ATR (our current: 1.0x ATR — matches)
- TP: Multi-level (1.0x, 1.5x, 2.5x, 4.0x ATR for position scaling)

## News Avoidance

Close positions 5-15 min before high-impact events (NFP, Fed rate, CPI). Wait 15-30 min after release.
**How to apply:** Consider adding economic calendar integration or hard-coded news time filter.