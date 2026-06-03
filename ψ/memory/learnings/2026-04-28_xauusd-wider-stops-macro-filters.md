---
name: XAUUSD Requires Wider Stops + Macro Filters
date: 2026-04-28
type: learning
source: rrr: broky-oracle
tags: [xauusd, atr, stop-loss, macro, config, backtest]
---

# XAU/USD Requires Wider Stops and Macro Filters

## Finding

Backtest sweep across 56 parameter combinations on XAUUSD H1 confirms: ATR multiplier of 2.5 consistently produces higher win rates (44-45%) vs ATR 1.5 (33-39%). No configuration achieves WR > 55% with current indicator set alone.

## Root Cause

Gold's daily range (1500-3000 pips) means tight stops get clipped by normal volatility spikes. The current system lacks XAU/USD-specific filters that forex systems don't need: macro correlation (DXY inversely correlates -0.85, US10Y inversely -0.70), news event filtering (NFP/CPI/FOMC), RSI 80/20 overextension detection, and EMA200 hard direction filtering.

## Evidence

- C60 RR2.0 ATR2.5 R0.5% MH48: WR 44.8%, PF 1.43 (best WR at C60)
- C60 RR2.5 ATR1.5 R0.5% MH48: WR 34.6%, PF 1.31 (ATR 1.5 = too tight)
- C65 RR2.0 ATR2.5 R0.5% MH48: WR 45.2%, MaxDD 23.9% (lowest MaxDD overall)
- Config-code disconnect: 3 YAML files exist but all values are hardcoded in Python

## How to Apply

- Set default ATR multiplier to 2.5 for XAUUSD (was 1.5)
- Add macro confidence adjustments: DXY rising strongly + BUY = ×0.7
- Add news filter: block trades 30 min before/after NFP, CPI, FOMC
- Add RSI 80/20 hard filter (not 70/30 standard)
- Add EMA200 hard direction filter (buy only above, sell only below)
- Fix config system: make code read from YAML instead of hardcoded constants