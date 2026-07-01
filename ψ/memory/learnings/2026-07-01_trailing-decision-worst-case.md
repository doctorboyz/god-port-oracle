# Trailing Stop Decision + Worst-Case Prevention

> Date: 2026-07-01
> Related: [[2026-07-01_trailing-bar-by-bar-replay]]
> Status: CHOSEN — pending deploy to Real-A (waiting user go-ahead)

## Decision: Keep 2 trailing variants

After bar-by-bar replay on Exness (1131 trades) + premium (1996 trades, 2023-2026), two variants tied for top WR and survived both datasets. **Both are kept as the chosen trailing stops.**

### Variant 1: D-simple 0.20/0.10 (fixed-%)
- activation_pct = 0.20% (arm when price moves 0.20% in favor)
- trail_pct = 0.15% — wait, trail = 0.10% (lock profit 0.10% below peak)
- **Best Exness PnL in top-WR tier**

### Variant 2: ATR-trail k=0.5 act=0.20 (volatility-adaptive)
- activation_pct = 0.20% (arm when price moves 0.20% in favor)
- trail_pct = (0.5 × ATR_at_entry) / entry_price × 100  (volatility-adaptive)
- **Best cross-dataset robustness (premium 3yr nearly break-even)**

## Worst-Case Records (for prevention)

These are the observed worst cases across both datasets. If live trading exceeds these, alert + pause.

### D-simple 0.20/0.10 worst cases
| Metric | Exness | Premium 3yr |
|---|---|---|
| MaxDD | $466 (May-June) | $271 |
| Worst single trade | -$15.23 (avg SL trade) | -$15.23 avg SL |
| Max consecutive losses | (same as SL sequence) | 7+ in ranging 2023 |
| Losing year | n/a (only May-June) | 2023, 2025, 2026 — only 2024 profitable (+$91) |
| WR floor | 57.5% (Exness) | 30.1% (2023 ranging) |
| PnL floor | n/a | -$57.86 (2023) |

### ATR-trail k=0.5 act=0.20 worst cases
| Metric | Exness | Premium 3yr |
|---|---|---|
| MaxDD | $508 (May-June) | $241 (lowest of all variants) |
| Worst single trade | -$15.23 avg SL | -$15.23 avg SL |
| WR floor | 57.5% (Exness) | 37.8% (3yr avg) |
| PnL floor | n/a | -$7.75 (3yr total — nearly break-even) |

### Hard prevention rules
- If live MaxDD exceeds **$600** on Real-A with either variant → pause, investigate
- If WR drops below **45%** over 50-trade rolling window → pause
- If 5 consecutive SL hits with armed trailing (trailing arms then reverses through trail_level) → regime mismatch, pause
- If premium-style losing year (negative PnL over ~700 trades) → strategy degradation, retrain

## What we DO NOT use (rejected)
- **D-simple 0.30/0.15** — previous MFE/MAE recommendation, WORSE than let-run on premium (-$526 vs -$385). Confirmed bad via bar-by-bar.
- **D-simple 0.40/0.20** — too wide, trailing rarely arms, performs like let-run with extra cost
- **Breakeven 0.20 + trail 0.15** — too many BE exits (196 zero-PnL trades on Exness), drags WR down to 39.9%

## Deploy Notes (when user approves)
- ATR-trail needs `atr_at_entry` field — already in live_trades schema
- Trailing logic goes in `metty/execution/live_trader.py` position-monitoring loop
- Conservative intra-bar assumption used in backtest (adverse-first) — live execution uses tick-level checks so live performance may be slightly BETTER than backtest
- IRON LAW: deploy to Real-A only after user explicit approval; backtest verification artifacts already saved at /tmp/premium_replay.log and scripts/trailing_replay.py