# God Port Oracle — Memory Index

## Learnings
- [VPS Bridge Restart Architecture](learnings/2026-05-03_vps-bridge-restart.md) — 3-phase startup, user abc, VNC, RPyC bridge on 3 accounts
- [Wine Prefix Ownership Bug](learnings/2026-05-04_wine-prefix-ownership.md) — chown must happen after gmag11 exits, not after file existence
- [gmag11 Race Condition](learnings/2026-05-04_gmag11-race-condition.md) — wait for PID, not file existence
- [RPyC Netref Dict Limitations](learnings/2026-05-03_rpyc-netref-dict-limitations.md) — use bracket access, not .get(); pass explicit args for order_send
- [Exness Order Send](learnings/2026-05-03_order-send-exness.md) — filling modes, AutoTrading, market hours, close position
- [MT5 Bridge JSON Transfer](learnings/2026-05-04_mt5-bridge-json-transfer.md) — 166x speedup: JSON method for 500 candles in 0.18s vs netref 30+s
- [VPS MT5 Bridge Status](learnings/2026-05-04_vps-mt5-bridge-status.md) — Accounts B+C working, A needs VNC login; symbol XAUUSD (Pro/Raw) vs XAUUSDm (Standard)
- [Live Collector Architecture](learnings/2026-05-04_live-collector-architecture.md) — sentiment caching, calendar awareness, MT5 bridge fallback
- [Finnhub API Gotchas](learnings/2026-05-04_finnhub-api-gotchas.md) — string timestamps, country vs currency codes, free tier limits
- [Learning Mode No Fear Trading](learnings/2026-05-06_learning-mode-no-fear-trading.md) — bypass all blockers, record factors, analyze later
- [Exness Filling Mode](learnings/2026-05-06_exness-filling-mode.md) — Exness requires ORDER_FILLING_FOK (0) not IOC (2), retcode 10030

- [M5 Scalp Order Fix](learnings/2026-05-06_m5-scalp-order-fix.md) — shared bridge per cycle, retry logic, cross-account ticket prefix, phantom trades risk
- [Trading Results Analysis](learnings/2026-05-07_trading-results-analysis.md) — M5 Scalp profitable (+$385), Swing losing (-$124), 57% SL exit rate, Account A/B critical, 499 phantom trades
- [Per-Account A/B/C Testing](learnings/2026-05-11_per-account-ab-testing.md) — XAUUSD needs 3x+ ATR SL; per-account env vars for ATR, RR, confidence; simultaneous config testing
- [ML Data Quality & Feature Importance](learnings/2026-05-13_ml-data-quality-feature-importance.md) — conf < 0.5 = noise (33% WR); ichimoku cloud is king; BUY/SELL need separate models; SELL untrainable; CV overfits time-series
- [god-port2 (VibeTrading)](../learn/god-port2/2026-05-07_LEARNINGS.md) — Agent-first crypto trading framework; @vibe decorator, AST validation, LLM strategy analysis, position sizing library, exchange ABC
- [Claude Code Stock DB](learnings/2026-05-08_claude-code-stock-database.md) — 17,000+ stock DB via prompt, potential multi-asset expansion
- [Spread: Static vs Tick Data](learnings/2026-05-16_spread-static-vs-tick-data.md) — mt5.symbol_info().spread is static (0 by default); live spread = (ask-bid)/point from symbol_info_tick
- [asyncio.run() + Thread Context = Fragile for Bridge](learnings/2026-05-18_asyncio-run-thread-context-bridge.md) — connect-disconnect-reconnect across separate asyncio.run() calls fails; use PersistentMT5Bridge with single connection per cycle

## Retrospectives
- [2026-05-03 Bridge Fix + Order Send](retrospectives/2026-05/03/17.22_3-bridge-fix-order-send.md)
- [2026-05-06 Learning Mode Deploy](retrospectives/2026-05/06/07.28_learning-mode-deploy.md)
- [2026-05-11 Per-Account Strategy A/B/C](retrospectives/2026-05/11/23.13_per-account-strategy-ab-testing.md)
- [2026-05-13 ML Data Quality & Feature Importance](retrospectives/2026-05/13/09.55_ml-data-quality-config.md)
- [2026-05-16 Spread Fix & Ranging Signals](retrospectives/2026-05/16/23.54_spread-fix-ranging-signals.md)
- [2026-05-18 M5 Scalp Spread Bug Fix](retrospectives/2026-05/18/23.33_m5-scalp-spread-bug-fix.md)
- [2026-05-22 ML Risk-Scaling](retrospectives/2026-05/22/10.42_ml-risk-scaling.md) — hard blocking → lot scaling, XGBoost calibration dead end, deploy
- [2026-05-25 ML Swing + Quality Gate](retrospectives/2026-05/25/22.45_ml-swing-quality-gate.md) — เพิ่ม ML ใน swing trader, เขียน docs 5 ไฟล์, market ranging → ML รอทดสอบ

## Learnings
- [ML Risk-Scaling vs Hard Blocking](learnings/2026-05-22_ml-risk-scaling-vs-hard-blocking.md) — เมื่อ model signal weak, risk-scale แทน block; calibration ต้องการ samples > 2000
- [ML Last-Mile Integration](learnings/2026-05-25_ml-last-mile-integration.md) — ก่อน optimize model: เช็คว่า pipeline end-to-end ทำงาน — signal → ML → trade ครบทุก trader
- [Trend Filter Asymmetry](learnings/2026-05-27_trend-filter-asymmetry.md) — D1 bullish → BUY เสียหายหมดในตลาดขาลง; H4 override เร็วกว่า 4x แต่ยังช้า 20ชม; ML bias จาก training regime; silent crash = silent losses
- [Trend Features Data Pipeline Blind Spot](learnings/2026-05-28_trend-features-data-pipeline.md) — backfill strip d1_trend ทิ้งจาก features_json ทำให้ 2,078 trades ไม่มี trend features; metadata vs feature เป็นเส้นบางๆ
- [Decorator Import-Time Side Effects](learnings/2026-05-28_decorator-import-side-effects.md) — @strategy มี side effect ตอน import; utility function ใต้ decorator โดยไม่ตั้งใจ = fatal import error
- [Scope Verification + SQL Placeholders](learnings/2026-06-04_scope-verification-sql-placeholders.md) — เปลี่ยน variable reference ต้องเช็ค scope; SQL INSERT ต้องนับ ? placeholders ให้ตรงกับ columns
- [sklearn Version Pinning + v4 Deploy](learnings/2026-06-04_sklearn-version-pinning-v4-deploy.md) — sklearn 1.8→1.9 breaks model loading (`_loss` module); pin in Dockerfile; verify model loading in deploy
- [v4 Model Deployment Fresh Start](learnings/2026-06-04_v4-model-deployment-fresh-start.md) — v4 with 10 sub-models incl volatile deployed; accounts reset A=$100 B=$500 C=$1000; ML filter enabled all traders
- [ML Data Pipeline Return Values](learnings/2026-06-05_ml-data-pipeline-return-values.md) — return values > instance variables; _last_loss_proba hack caused NULL in DB; changed to 4-tuple return
- [Counter-Trend Bollinger Mean Reversion](learnings/2026-06-05_counter-trend-bollinger-mean-reversion.md) — hard block counter-trend when H4 overrides D1; allow mean-reversion at Bollinger extremes (boll_pos ≤0.15 or ≥0.85) with trend_mult=0.3
- [Partial TP Backtest Option C](learnings/2026-06-05_partial-tp-backtest-option-c.md) — Option C (close TP1, scale-in) improves PnL by ~$14,698; 34.6% of trades reach TP1 but not final TP; ~46% of SL trades likely reached TP1 first; Monte Carlo estimate (no M5 candle data for trade dates)
- [Data Integrity Backfill Chain](learnings/2026-06-07_data-integrity-backfill-chain.md) — Data chains must be verified end-to-end; backfill must UPDATE existing rows not just INSERT; get_connection must accept str|Path; account_id → fixed params mapping
- [ML v4 vs v5 Backtest](learnings/2026-06-07_ml-v4-vs-v5-backtest.md) — v4 outperforms v5 at all thresholds; regime_encoded fix in features.py; ENCODED_FEATURES added to ALL_FEATURE_COLS; VPS switched to v4 model; partial TP enabled on Account C
- [Partial TP Bug Fix + Analysis](learnings/2026-06-07_partial-tp-bug-fix-and-analysis.md) — tp1_ratio hardcoded to 0.5 fixed in 3 traders; Option C estimation +$14,698 but M5 candle data shows tighter scale-in SL

## Retrospectives
- [2026-05-27 H4 Trend Filter + D1 Flip](retrospectives/2026-05/27/19.59_h4-trend-filter-d1-flip.md)
- [2026-05-28 Trend Features Pipeline Fix](retrospectives/2026-05/28/08.38_trend-features-pipeline.md)
- [2026-05-28 Trend Alignment Deploy Fix](retrospectives/2026-05/28/13.32_trend-alignment-deploy-fix.md)
- [2026-06-04 ML Parameter Audit + Deploy](retrospectives/2026-06/04/07.52_ml-parameter-audit-deploy.md) — audit 8 bugs, deploy 3 fixes, 2 บั๊กที่พบหลัง deploy (h4_trend NameError, INSERT placeholder mismatch)
- [2026-06-04 v4 Deploy Fresh Start](retrospectives/2026-06/04/21.48_v4-deploy-fresh-start.md) — pipeline → v4 training → sklearn version fix → deploy → fresh start
- [2026-06-05 ML Data Pipeline Fix + Counter-Trend](retrospectives/2026-06/05/20.03_ml-data-pipeline-fix-counter-trend.md) — ml_loss_proba NULL bug fix, counter-trend hard block + mean-reversion exception, deploy
- [2026-06-07 Data Integrity Trading Params](retrospectives/2026-06/07/08.48_data-integrity-trading-params.md) — 4 VPS deploys to fix data chain; backfill must UPDATE not just INSERT; 100% coverage on 10,167 trade_outcomes