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
- [god-port2 (VibeTrading)](../learn/god-port2/2026-05-07_LEARNINGS.md) — Agent-first crypto trading framework; @vibe decorator, AST validation, LLM strategy analysis, position sizing library, exchange ABC
- [Claude Code Stock DB](learnings/2026-05-08_claude-code-stock-database.md) — 17,000+ stock DB via prompt, potential multi-asset expansion

## Retrospectives
- [2026-05-03 Bridge Fix + Order Send](retrospectives/2026-05/03/17.22_3-bridge-fix-order-send.md)
- [2026-05-06 Learning Mode Deploy](retrospectives/2026-05/06/07.28_learning-mode-deploy.md)