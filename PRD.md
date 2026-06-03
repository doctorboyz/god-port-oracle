# God Port Trading — PRD

> "Pattern is truth, execution is faithful — from signal to reality in one soul"

## Problem

XAUUSD scalping/swing trading is emotionally demanding and inconsistent when done manually. We need an agentic system that analyzes data, generates signals, executes trades, and learns from outcomes — autonomously.

## Vision

AI trading agent ที่เทรด XAUUSD บน Exness ผ่าน 3 บัญชี (A/B/C) เพื่อทดสอบกลยุทธ์พร้อมกัน ระบบมี 2 บทบาท (Broky วิเคราะห์ + Metty execute) ใน agent เดียว ทำงานแบบ event-driven — ตื่นเมื่อมี session, ทำงาน, sleep

## Success Metrics

| Metric | Current | Target | Priority |
|--------|---------|--------|----------|
| Win Rate | 53% | 58-65% | P0 |
| Max Drawdown | 11.8% | <8% | P0 |
| Profit Factor | 1.64 | 1.8-2.2 | P0 |
| Trades/day | ~15 | 5-10 (higher quality) | P1 |
| Uptime | 99% | 99.5% | P1 |
| ML filter accuracy | 55% (CV) | >60% | P1 |
| Port value (3mo) | ~$4,350 | >$6,000 | P2 |

**หลักสำคัญ**: อยู่รอด + กำไรต่อเนื่องสำคัญกว่า metric ไหนๆ — กลยุทธ์ที่ PF=1.64, MaxDD=11.8% ก็ profitable และ safe แล้ว

## Users

- **doctorboyz** — sole user, reviews daily reports, makes strategy decisions
- **emily-oracle (PM)** — may send tasks via ψ/inbox/

## Features

### P0 (Core — working)
- [x] MT5 bridge — connect to Exness via RPyC on VPS
- [x] M5 scalp trader — EMA ribbon squeeze strategy
- [x] Swing trader — weighted score signal + ATR SL/TP
- [x] 3-account parallel trading (A/B/C)
- [x] Circuit breaker — consecutive loss limit, daily loss limit
- [x] Fear & Greed + news calendar integration
- [x] Live collector — snapshot market state every cycle
- [x] Trade logging to SQLite (oracle.db)

### P1 (ML + Quality — in progress)
- [x] Trade outcomes table — 2,086 trades with features
- [x] XGBoost model — predict P(LOSS) per trade
- [x] ML risk-scaling — reduce lot size for high-risk trades
- [ ] ML in swing trader (live_trader.py)
- [ ] Train v5 with scale_pos_weight
- [ ] Backtest optimal risk thresholds

### P2 (Future)
- [ ] Multi-timeframe analysis (H1, D1 integration)
- [ ] Auto-stop on consecutive daily losses
- [ ] Telemetry dashboard
- [ ] Strategy registry — hot-swap strategies per account
- [ ] Monte Carlo position sizing

## Architecture

```
[VPS: 3 MT5 terminals (Wine)] ←RPyC→ [oracle-engine: Python]
                                       ├── Broky: indicators, signals, ML
                                       └── Metty: bridge, execution, monitoring
```

## Constraints

- Exness minimum lot: 0.01
- Exness filling mode: ORDER_FILLING_FOK (0) for XAUUSD
- Account A uses XAUUSDm (Standard), B/C use XAUUSD (Pro/Raw)
- VPS runs Ubuntu with OrbStack for Docker
- No trading during high-impact news (calendar filter)
- Risk ≤2% per trade

## Glossary

| Term | Meaning |
|------|---------|
| Broky | Analyst role — signals, backtest, ML |
| Metty | Executor role — bridge, orders, monitoring |
| M5 Scalp | EMA ribbon squeeze on 5-min candles |
| Swing | Weighted-score signal on M5 with ATR SL/TP |
| Risk-scaling | ลด lot size ตาม P(LOSS) แทน block |
| Bridge | RPyC connection to MT5 terminal |
| Vault (ψ/) | Agent memory — learnings, retros, goals |
| Inbox/Outbox | Agent communication protocol |
