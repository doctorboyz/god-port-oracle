# April 2026 Learnings Summary

Consolidated from 7 individual dated files (2026-04-28 through 2026-04-30).

## 2026-04-28 -- XAUUSD Wider Stops + Macro Filters
Backtest sweep across 56 configs on XAUUSD H1 confirms ATR multiplier 2.5 gives WR 44-45% vs ATR 1.5 at 33-39%. No config hits WR > 55% with current indicators alone. XAUUSD needs macro filters (DXY/US10Y correlation), news filtering (NFP/CPI/FOMC), RSI 80/20 overextension detection, and EMA200 hard direction filter. Config-code disconnect found: YAML values exist but code uses hardcoded constants.

## 2026-04-28 -- Oracle Principles: Broky
Broky Oracle awakened with the 5 Principles + Rule 6. Core identity: pattern recognition through data-driven indicators, not emotional trading. All signals from weighted indicator scores, validated by backtest. Key rules: risk per trade 1-2%, circuit breaker at 5% daily loss, 3 consecutive losses = 15 min cooldown, ADX < 20 = no trade, D1 trend alignment only.

## 2026-04-28 -- Oracle Principles: Metty
Metty Oracle awakened with the 5 Principles + Rule 6. Core identity: faithful execution of Broky's signals without override. Execution-specific rules: health check first, 5s timeout, slippage guard at 0.5%, max 2 concurrent positions, paper trade first, always notify via Telegram, never exceed risk limits set by Broky.

## 2026-04-28 -- Trading Strategies Knowledge Base
Comprehensive trading reference: JPMorgan position scaling rules (10 tiers from -30% to +100%), indicator combinations (RSI, MACD, Bollinger, EMA Cross, ATR, Stochastic, Volume), risk management (1-2% per trade, 3-5x leverage, isolated margin), session-based trading (Asian/London/NY/Overlap), multi-timeframe analysis (D1/H4/H1/M5), walk-forward backtesting methodology, and ML/LSTM assessment. Phase progression: backtest → forward → paper → live start → live growth.

## 2026-04-28 -- MT5 Bridge Knowledge Base
Architecture for MT5 connection via Wine + mt5linux on macOS. Bridge configuration: mt5linux on port 5005, Wine prefix path, PM2 process naming. Broker details: Exness, XAUUSD, 3-5x leverage. Order execution flow: read signal → health check → convert to MT5 order → send → await result (5s timeout) → write report → Telegram notify. Error handling: timeout, requote (0.5% slippage guard), no connection (3 retries), invalid volume.

## 2026-04-29 -- Phase 1.5 Signal Quality
ATR multiplier from 1.5 to 2.0 improved WR by +5pp (48% to 53%) -- the single biggest lever. Session filtering added +2pp but had limits. MTF hard filter = soft filter in practice because halving confidence already pushes counter-trend below min confidence. WR ceiling at ~53% with current indicators; WR >= 55% requires new indicators. Core philosophy: PF=1.64 + MaxDD=11.8% is profitable even at WR=53%. Forward test engine built.

## 2026-04-30 -- EZB + 1CB Trading Philosophy
Extracted from Thai trader "ไอสไตล์อีซี่เทรด" (4 years, 100+ withdrawals): "ท่าง่าย ทำซ้ำได้ วัดผลได้" -- Simple, repeatable, measurable. WR 40-50% is sufficient with solid risk management. Key concepts: multi-timeframe top-down (Day→H1→M5/M20), four structure types (Close Level, DWZ, SWZ, Flip Zone), six candlestick patterns only valid at structural levels, 1CB (1 Candle Box) confirmation with 6 iron rules, EZB system (Channel→Box→Breakout→Retest→Trade Plan). Aligns with God Port philosophy on RPT, MTF, and patience.
