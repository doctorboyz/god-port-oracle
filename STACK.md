# God Port Trading — Technology Stack

## Language & Runtime
- **Python 3.11+** — primary language
- **asyncio** — async bridge communication

## Data & ML
- **pandas, numpy** —数据处理
- **XGBoost** — trade outcome prediction (P(LOSS))
- **scikit-learn** — calibration, feature importance
- **TA-Lib** — technical indicators fallback (custom indicators preferred)

## Database
- **SQLite** (oracle.db) — trades, signals, snapshots, outcomes
  - `live_trades` — executed trades with entry/exit/pnl
  - `signals` — generated signals with indicator breakdown
  - `snapshots` — market state every cycle
  - `trade_outcomes` — features + outcome for ML training

## Broker Connection
- **RPyC** — Python RPC bridge to MT5 terminals on VPS
- **MetaTrader 5** — 3 terminals (Wine on Ubuntu) for accounts A/B/C

## Infrastructure
- **Docker** (OrbStack on Mac, Docker Compose on VPS)
- **Ubuntu VPS** (100.68.106.101) — production runtime
- **OrbStack** — local Mac development

## DevOps
- **GitHub** — source control, main branch
- **Docker Compose** — container orchestration
  - `oracle-engine` — main Python process
  - `mt5a`, `mt5b`, `mt5c` — MT5 + RPyC bridge per account

## Monitoring & Comms
- **Telegram Bot** — trade notifications (metty/notify/)
- **Python logging** — structured logs to stdout/stderr
- **Docker health checks** — container-level monitoring

## External APIs
- **Finnhub** — economic news, Fear & Greed alternative
- **Fear & Greed Index** — market sentiment

## Testing
- **pytest** — 283 tests, all passing
- Test categories: unit (indicators, models, signals), integration (M5 scalp, data pipeline)

## Agent Memory (ψ/)
- **Markdown files** — vault for learnings, retros, goals
- **ψ/inbox/ψ/outbox** — agent communication protocol
