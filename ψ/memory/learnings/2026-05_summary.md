# May 2026 Learnings Summary

Consolidated from 35 individual dated files (2026-05-01 through 2026-05-28).

## Week 1 (May 1-7) -- Foundation & Bridge

### 2026-05-01 -- Exness Account Types Reference
Five account types on Exness: Standard (from 0.2 pips, no commission), Standard Cent ($1 min), Pro (from 0.1 pips, instant execution), Raw Spread (0.0 pips + commission), Zero. Different server suffixes per type. For our ML setup: Account A = Standard Cent (volume signals, $1), B = Standard (OB/OS, $100), C = Pro (MA/trend, $200).

### 2026-05-01 -- ML Data Collection Architecture
ML goal is data collection, not profitable trading. Four signal groups fire independently with simple triggers to capture diverse market conditions. When any group triggers, record ALL indicator values as feature snapshot. Cross-group correlations are the gold -- volume signal + RSI oversold together may have much higher WR. SQLite for data, PyTorch for deep learning. Timeline: weeks of data collection before ML can start.

### 2026-05-01 -- MT5 VPS Architecture
MT5 Docker containers on x86_64 VPS, connected from macOS via RPyC. Three containers (mt5-acct-a/b/c) with different balance/leverage. VPS required because Wine cannot run inside Docker + Rosetta on Apple Silicon.

### 2026-05-01 -- Exness Demo Account Configuration
Three demo accounts created: A (Standard $100, 1:2000, Exness-MT5Trial17), B (Pro $500, 1:500, Exness-MT5Trial17), C (Raw Spread $1000, 1:500, Exness-MT5Trial7). A and B share server; C needs separate MT5 terminal. Control variables: balance, leverage, spread type, commission.

### 2026-05-01 -- Wine Rosetta Incompatibility
Wine crashes inside Docker under Rosetta 2 with "invalid gdt selector index 5". This is a fundamental Apple Silicon limitation -- Rosetta cannot translate x86 segment descriptor operations. Solution: deploy on x86_64 VPS. Wine runs natively there. Not fixable on macOS ARM Docker.

### 2026-05-01 -- VPS Bridge Deployment
MT5 bridge on x86_64 VPS (vpsdeluna) working for Account A. gmag11 image works on native x86_64. numpy 2.x incompatible with MetaTrader5 -- must downgrade to numpy<2. mt5linux 1.0.3 doesn't support -w flag; custom RPyC bridge server used instead. MT5 first login must be manual via VNC. Exness symbol suffix: XAUUSDm (note the 'm').

### 2026-05-03 -- RPyC Netref Dict Limitations
RPyC remote dicts only support bracket access (result['key']), not .get(), .keys(), or dict() construction. For order_send: pass individual arguments over RPyC, construct the MqlTradeRequest dict inside Wine Python. This is the third time hitting this issue -- now formally documented.

### 2026-05-03 -- Exness Order Send
Working order_send parameters for XAUUSDm: action=1 (TRADE_ACTION_DEAL), order_type=0/1 (BUY/SELL), price=ask/bid, deviation=20, magic=99999, type_time=0 (GTC), type_filling=0 (FOK). Exness filling_mode=3 supports both FOK and IOC, but only FOK works. Common retcodes: 10009=DONE, 10018=MARKET_CLOSED, 10027=AUTOTRADING_DISABLED, 10030=UNSUPPORTED_FILLING. AutoTrading must be enabled in MT5.

### 2026-05-03 -- VPS Bridge Restart Architecture
Container restart flow: Phase 0 (nginx+KasmVNC) → Phase 1 (gmag11 init -- wait for PID exit) → Phase 1.5 (chown, fix numpy, install rpyc as user abc) → Phase 2 (start MT5 terminal) → Phase 3 (start bridge server). Key: gmag11 start.sh runs as root, creates root-owned files. Must wait for PID to fully exit before chown. 3 iterations to fix the race condition.

### 2026-05-04 -- VPS MT5 Bridge Status
Accounts B (Pro, port 5006) and C (Raw Spread, port 5007) working. Account A needs VNC login (no IPC connection). Container ports: VNC 5900/5901/5902, RPyC 5005/5006/5007, internal bridge 8001. Live collector: B and C collecting snapshots every 5 min; A falls back to CSV. 16,572+ total snapshots.

### 2026-05-04 -- MT5 Bridge JSON Transfer (166x speedup)
copy_rates_from_pos_json returns JSON string instead of netref list. 500 candles: 0.18s JSON vs 30+ seconds netref. Always use _json methods for candle data. Symbol name discovery: XAUUSDm (Standard/micro) vs XAUUSD (Pro/Raw). Resampler handles both Title Case and lowercase column names.

### 2026-05-04 -- gmag11 Race Condition
gmag11 start.sh runs in background and creates root-owned files throughout its 7-step process. Polling for terminal64.exe is insufficient -- process continues creating files after file appears. Solution: wait $GMAG11_PID to ensure process fully exits before chown. "When modifying files created by a background process, always wait for the process to exit."

### 2026-05-04 -- Live Collector Architecture
Data flow: bridge candles + Fear & Greed (15min cache) + Finnhub news (15min cache) + calendar (1hr cache) → compute all indicators → snapshot → DB. Account IDs: 1=historical, 2=live_A, 3=live_B, 4=live_C. CLI: collect-live --cycles 1 or --interval 300. Bridge-first, CSV-fallback ensures collection works during development.

### 2026-05-04 -- Finnhub API Gotchas
Finnhub returns string timestamps ("2026-05-04 00:00:00"), not unix ints. Uses ISO country codes (US, CA, EU) not currency codes (USD, CAD, EUR) -- must map explicitly. social_sentiment not on free tier. calendar_economic works on free tier. Free tier rate limit: 60 calls/min. Always test API integrations against live service; parse defensively.

### 2026-05-04 -- Wine Prefix Ownership Bug
On fresh Docker volumes, gmag11's start.sh creates /config/.wine owned by root. Wine refuses: "is not owned by you." Cannot fix in Dockerfile because volume mounted at runtime, not build time. Fixed by chown -R abc:abc /config after Phase 1 (gmag11 init completes), before Phase 1.5 (pip commands as user abc).

### 2026-05-05 -- SQLite Schema Migration Ordering
Indexes on new columns added to existing tables must go in migration (after ALTER TABLE), not in SCHEMA_SQL. SCHEMA_SQL runs as single executescript(). For existing tables, CREATE TABLE IF NOT EXISTS is a no-op but CREATE INDEX on new columns fails. Any CREATE INDEX referencing a newly-added column must be in _migrate_*() function.

### 2026-05-05 -- Python Enum Forward Reference in Pydantic
When adding Enum field to Pydantic model, the Enum class must be defined BEFORE the model class. Unlike type hints (which can use from __future__ import annotations), field DEFAULT values like trading_mode: TradingMode = TradingMode.SWING require the actual class at definition time.

### 2026-05-05 -- Scalping Best Practices
Parameters validated by 4+ professional sources: sessions = London+Overlap+NY (was missing NY), ADX threshold = 10 (was 15 -- too restrictive on M1), min confidence = 0.45 (was 0.55), direction threshold = 0.15 (was 0.30), max spread = 35 pts (was 30). M5 consensus sweet spot over M1 for XAUUSD. 3-confirmation rule: trend + momentum + volume. ATR-based SL/TP > fixed pips.

### 2026-05-05 -- EMA Ribbon + HTF Alignment + M5 Scalp
6-EMA Ribbon Cloud (8/13/21/34/55/89 Fibonacci) is the most professional XAUUSD scalp strategy. Three cloud layers: Fast/Mid/Slow. 4-level ATR-based TP scaling (1.0x/1.5x/2.5x/4.0x). HTF alignment (D1 200 EMA + H4 10/200 EMA) improves WR 10-15pp (45%→55-60%). Professional consensus: M5 is the sweet spot, M1 too noisy.

### 2026-05-05 -- M1 Scalp Architecture
Per-account threads: collector (300s M5), swing-trader (300s M5), scalp-trader (60s M1). Same oracle-engine, separate threads, separate risk config. Scalp vs Swing: no D1 trend filter, London+Overlap only, lower ADX/confidence thresholds, tighter risk (1%, 1x ATR SL, 1.5x R:R, 20 min max hold). DB tagging: trading_mode + strategy_id columns.

### 2026-05-06 -- Exness Filling Mode
Orders were failing with retcode 10030 (unsupported filling mode). Code was using ORDER_FILLING_IOC=2 but Exness requires ORDER_FILLING_FOK=0. Always use type_filling=0 for Exness demo accounts even though symbol_info shows filling_mode=3 (supports both).

### 2026-05-06 -- M5 Scalp Order Fix
M5 Scalp orders all failing with "No response" while swing orders worked. Root cause: 3 separate MT5Bridge instances per cycle (fetch, spread, order) with rapid connect/disconnect. Fix: shared bridge per cycle + 3-attempt retry. Additional findings: Account A equity $46.92 from $100, 75 open trades (20 phantom with ticket=None), learning mode has no position limits.

### 2026-05-06 -- Learning Mode: No Fear Trading
LEARNING_MODE=1 bypasses all blockers (confidence, session, spread, ADX, direction thresholds) while recording all factors. Appends "(learning: X below Y)" to reason strings. Direction threshold lowered to 0.05. Goal: maximum trade frequency for post-analysis to discover which factors correlate with wins. Always copy the working pattern -- MT5Bridge works reliably where PersistentMT5Bridge fails silently.

### 2026-05-07 -- Trading Results Analysis
After ~3 days live learning mode, 1091 trades analyzed. M5 Scalp profitable (+$385, 41% WR), Swing losing (-$124, 34% WR). 57% exits via stop-loss (-$7,773). Account A critical: 75% drawdown ($24.99 from $100). Account B negative free margin (-$88.50). 499 phantom trades (46% of records have ticket=NULL). Recommendations: stop swing on A+B, add position limits, clean phantoms, review SL placement.

## Week 2 (May 8-13) -- Expansion & Diagnostics

### 2026-05-08 -- Claude Code Stock Database
Claude Code can connect to 17,000+ stock database via prompt. Potential multi-asset expansion beyond XAUUSD using BrokerABC interface + new broker implementations.

### 2026-05-10 -- Vibe Trading Integration Baseline
P2 integration complete, LEARNING_MODE activated on all 3 demo accounts (2026-05-09). Pre-vibe baseline: WR 37-39%, all accounts profitable via R:R (wins larger than losses). All positions identical due to same signal. Vibe trading config: both phase, DRY_RUN=0, M5_SCALP_ENABLED=1, max 5 positions per account. This is the first real test of full system (P0+P1+P2+learning).

### 2026-05-11 -- Per-Account A/B/C Strategy Testing
Live results: 21.5% WR, -$604.58 loss. Root cause: SL too tight at 2x ATR on XAUUSD. Solution: per-account overrides -- A(ATR 3.0, RR 3.0, Conf 0.35), B(2.5, 2.5, 0.45), C(2.0, 2.0, 0.60). Env var approach for no-code config changes. Anti-rationalization: losses were configuration fault, not market conditions.

### 2026-05-13 -- ML Data Quality & Feature Importance
Analyzed 1,540 trades + 7,456 snapshots. Confidence threshold determines data quality: conf < 0.5 = noise (33% WR, -$93), conf 0.5-0.7 = quality (59% WR, +$3,709). Top 7 features: ichimoku cloud (senkou_a/b), ema_50, sma_50, ema_200, dema_21, sma_10. SELL fundamentally harder (35% WR vs 49% BUY) -- different features needed. CV accuracy 87-94% is fake (time-series correlation). Deployed quality config: conf 0.50/0.55/0.65, positions 3/3/3, LEARNING_MODE=0.

## Week 3 (May 16-18) -- Bug Hunting

### 2026-05-16 -- MT5 Spread: Static vs Tick Data
mt5.symbol_info().spread returns static reference value (0 by default), NOT live market spread. Actual spread = (ask - bid) / point from symbol_info_tick. This bug caused M5 scalp to produce zero signals for 10+ days. Any spread check using symbol_info silently fails. Fix: use symbol_info_tick for all runtime spread filtering.

### 2026-05-18 -- asyncio.run() + Thread Context = Fragile
M5 scalp spread bug resurfaced during London session. Root cause: connect-disconnect-reconnect across separate asyncio.run() calls from non-main thread. Error hidden by logger.debug() at INFO log level. Fix: PersistentMT5Bridge with single connection per cycle, logger.warning() for errors, ensure_connected_sync() for thread contexts.

## Week 4 (May 21-28) -- ML Pipeline & Trend Filters

### 2026-05-22 -- ML Risk-Scaling vs Hard Blocking
When ML model has weak signal (P(LOSS) distribution bimodal, accuracy ~55-65%), calibration doesn't help and hard blocking rejects almost all trades. Solution: risk-scaling -- P(LOSS)<50%=1.0, >85%=0.0, 50-85%=linear multiplier on lot size. Works because probability ranking is useful even when absolute probabilities are poorly calibrated. Needs >2000 samples for proper calibration.

### 2026-05-25 -- ML Last-Mile Integration
Spent 2 sessions optimizing XGBoost, calibration, risk-scaling without asking: "which trader will use ML?" Swing trader (the one that actually trades daily) had no ML. M5 scalp blocked by session/signal filters. Checklist before model training: ML init, signal generation, feature computation, risk-scaling, trade execution -- verify end-to-end before optimizing.

### 2026-05-27 -- Trend Filter Asymmetry
Hard trend filters create asymmetric risk: D1 bullish → SELL blocked (good) but BUY allowed with no protection against falling market. D1 EMA 50/200 takes weeks to flip; market can move 1-3% before filter acknowledges change. 8 consecutive losing BUYs (-$126) before D1 finally flipped. H4 EMA 10/50 flips ~4x faster. ML compounds problem when trained on regime-specific but biased data. Silent ML crashes = silent capital destruction.

### 2026-05-28 -- Decorator Import-Time Side Effects
@strategy decorator registers with global registry at import time. Accidental placement on utility function caused ValueError: "Strategy 'swing' already registered" -- entire import chain broke. Utility functions don't need decorators. Import chain test should be mandatory pre-deploy. Before refactoring near decorators, check what the decorator does at import time.

### 2026-05-28 -- Trend Features Data Pipeline Blind Spot
2,078 trade_outcomes had zero trend features. Root cause: backfill_trade_outcomes() intentionally stripped d1_trend as "metadata key." Single innocuous comment created blind spot -- ML models trained without knowing market regime. Pipeline: collector → feature_snapshots (has d1_trend) → backfill strips it → ML training → model blind to regime. Never strip columns from training data based on assumptions about what's "not a feature." Add validation tests for features_json structure.
