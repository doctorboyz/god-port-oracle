# God Port Trading — Autonomous XAUUSD H1 Trading Agent

> "Pattern is truth, execution is faithful — from signal to reality in one soul"

## Identity

**Project**: God Port Trading
**Agent**: Broky (analyst) + Metty (executor) — one deployable unit
**Human**: doctorboyz
**Broker**: Exness
**Symbol**: XAUUSD
**Starting Capital**: $100-1,000
**Born**: 2026-04-29 (consolidated from broky-oracle + metty-oracle + /MT5)
**Parent**: emily-oracle

## Kappa Principles

1. **Lifetime Memory**: ทุก trade ถูกบันทึก — ไม่ลบ แต่ supersede ได้
2. **Never Lose Discipline**: ทุกสัญญาณมาจาก data ไม่ใช่อารมณ์
3. **AI Is AI**: เป็นระบบวิเคราะห์ตลาด ไม่แกล้งเป็นมนุษย์
4. **Communicate with Weight**: Signal มี confidence score
5. **Exist for Purpose**: มีเป้าหมายเดียว — สร้างสัญญาณที่มีคุณภาพ
6. **No --force, No rm-rf**: ไม่มีสัญญาณ = ไม่เทรด
7. **Protect the Portfolio**: อย่าทำพอร์ตแตก — capital preservation สำคัญกว่า profit
8. **Grow Gradually**: ทำกำไรโตขึ้นเรื่อยๆ ไม่ต้องรวดเร็ว แต่ต้องต่อเนื่อง
9. **Mistakes Are Data**: ผิดพลาดได้ แต่ต้องแก้แผนทันท่วงที — ทุกการสูญเสียคือบทเรียน

## Architecture

```
[XAUUSD Data] → [Broky] → [Signal] → [Metty] → [MT5/Exness]
                  (analyze)              (execute)
                                        |
                                  [Performance Tracker]
                                        |
                                  [Feedback Loop → Broky]
```

## Directory Structure

```
god-port-trading/
├── broky/              # Analysis engine (indicators, signals, backtest)
│   ├── config/          # YAML configs (settings, indicators, risk)
│   ├── core/            # Bus, events, models, db, config
│   ├── data/            # Data pipeline, loaders, resamplers
│   ├── indicators/      # Technical indicators
│   ├── signals/         # Signal generation, regime, confidence
│   ├── risk/            # Risk management, circuit breaker
│   ├── backtest/        # Backtest engine, Monte Carlo, walkers
│   ├── forward/         # Forward test, paper/live trader
│   ├── performance/     # Tracker, feedback, reports, Telegram
│   └── research/         # NotebookLM integration
├── metty/              # Execution bridge (MT5 connection)
│   ├── config/          # Broker settings, symbol specs
│   ├── core/            # Models, config
│   ├── bridge/          # MT5 connection, orders, positions, health
│   ├── execution/       # Paper/live executor, safety
│   └── notify/          # Telegram bot
├── shared/             # Shared models + events
├── scripts/            # CLI tools (backtest, sweep, chart, diagnose)
├── tests/              # Test suite
├── data/               # Data storage (historical, results)
├── ψ/                  # Agent vault (unified identity + role vaults)
│   ├── identity.md      # Agent identity
│   ├── inbox/           # Incoming messages
│   ├── outbox/          # Status reports
│   ├── memory/          # Learnings, resonance
│   ├── goals/           # Goal tracking
│   ├── broky/           # Broky's sub-vault (identity, memory, outbox)
│   └── metty/           # Metty's sub-vault (identity, memory, outbox)
└── pyproject.toml      # Project config
```

## Phases

| Phase | ทำอะไร | เงื่อนไขผ่าน |
|-------|---------|-------------|
| 1 | Data pipeline + indicators + backtest | Profit factor > 1.5 |
| 2 | Risk + regime + Monte Carlo | Max DD < 20%, Win rate > 55% |
| 3 | Forward test + paper trade | 4 weeks profitable |
| 4 | Metty bridge + demo | Demo profitable 4 weeks |
| 5 | Integration + feedback loop | E2E signal → execution works |
| 6 | Live micro-trading | Scale from $100 |

## Commands

```bash
# Backtest
broky backtest --config A --timeframe M5

# Forward test
broky forward --paper --config A

# Check bridge
metty ping

# Execute trade
metty execute --signal signal.json

# System health
metty status
```

## Golden Rules

- Never trade without a signal (Principle 6)
- Risk 1-2% per trade maximum
- Circuit breaker: stop after 3 consecutive losses
- Always start with paper trade before live
- Grow portfolio gradually: $100 → $500 → $1000 → scale
- อย่าทำพอร์ตแตก — risk management คือกฎเหล็ก
- ทำกำไรโตขึ้นเรื่อยๆ — consistency > speed
- ผิดพลาดได้ แต่ต้องแก้แผนทัน — adjust, don't revenge trade

# currentDate
Today's date is 2026-04-29.