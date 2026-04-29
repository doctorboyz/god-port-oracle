# MT5 Trading System — Broky + Metty

> "Pattern is truth — the market speaks in data"

Algorithmic trading system for XAUUSD on Exness, powered by the Kappa ecosystem.

## Quick Start

```bash
cd ~/MT5

# Install dependencies
pip install -e ".[dev]"

# Run backtest
broky backtest --config A --timeframe M5

# Check MT5 bridge
metty ping

# Paper trade
broky forward --paper

# Live trade (AFTER paper trading is profitable)
broky forward --live --config A
```

## The Two Cells

| Cell | Role | Kappanet ID |
|------|------|-------------|
| **Broky** | Market analysis, signals, backtesting | `kappa:doctorboyz:broky` |
| **Metty** | MT5 execution, bridge, notifications | `kappa:doctorboyz:metty` |

## Trading Phases

| Phase | Capital | Risk/Trade | Max Lot |
|-------|---------|-----------|---------|
| Backtest | $1,000 (sim) | 3% | 0.10 |
| Forward | $1,000 (sim) | 2% | 0.05 |
| Paper | $1,000 (demo) | 1% | 0.01 |
| Live Start | $100-1,000 | 1% | 0.01 |

## Data

XAUUSD Premium Data at `data/xau-data/`:
- M1, M5, M15, M30 (200K rows each)
- H1 (100K), H4 (27K), D1 (5K)
- Session-classified M5 data (Asian/London/NY/Overlap)

## Architecture

```
Data → Broky (analyze) → Signal → Metty (execute) → MT5/Exness
                                              ↓
                                    Performance Tracker
                                              ↓
                                    Feedback Loop → Broky
```

## License

MIT