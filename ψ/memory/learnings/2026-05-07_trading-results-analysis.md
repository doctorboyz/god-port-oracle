# Trading Results Analysis — 2026-05-07

**Date**: 2026-05-07
**Context**: First comprehensive trading results analysis after ~3 days of live learning mode

## Account Status (Live MT5)

| Account | Balance | Equity | Margin | Free Margin | Positions | Floating P/L |
|---------|---------|--------|--------|-------------|-----------|--------------|
| A (Standard $100) | $24.99 | $65.67 | $54.50 | $11.17 | 23 | +$40.68 |
| B (Pro $500) | $41.26 | $72.62 | $161.12 | -$88.50 | 19 | +$29.16 |
| C (Raw Spread $1000) | $465.92 | $462.08 | $151.58 | $310.50 | 18 | -$3.84 |

**Critical**: Account B has NEGATIVE free margin (-$88.50). Account A started at $100, now balance $24.99 (75% drawdown).

## Closed Trades Summary (1016 total)

### By Account + Mode

| Account | Mode | Trades | Wins | Losses | WR | PnL | Avg PnL |
|---------|------|--------|------|--------|----|-----|---------|
| A | m5_scalp | 84 | 37 | 47 | 44.0% | +$110.37 | +$1.31 |
| A | swing | 250 | 81 | 169 | 32.4% | -$321.54 | -$1.29 |
| B | m5_scalp | 84 | 33 | 51 | 39.3% | +$9.44 | +$0.11 |
| B | scalp | 1 | 0 | 1 | 0% | -$5.71 | -$5.71 |
| B | swing | 257 | 90 | 167 | 35.0% | -$1.27 | -$0.00 |
| C | m5_scalp | 85 | 34 | 51 | 40.0% | +$265.58 | +$3.12 |
| C | swing | 255 | 91 | 164 | 35.7% | +$199.22 | +$0.78 |

### By Strategy

| Strategy | Mode | Count | Wins | Losses | WR | PnL |
|----------|------|-------|------|--------|----|------|
| m5-scalp-A | m5_scalp | 84 | 37 | 47 | 44.0% | +$110.37 |
| m5-scalp-B | m5_scalp | 84 | 33 | 51 | 39.3% | +$9.44 |
| m5-scalp-C | m5_scalp | 85 | 34 | 51 | 40.0% | +$265.58 |
| swing-A | swing | 250 | 81 | 169 | 32.4% | -$321.54 |
| swing-B | swing | 257 | 90 | 167 | 35.0% | -$1.27 |
| swing-C | swing | 255 | 91 | 164 | 35.7% | +$199.22 |

### Exit Reasons

| Exit Reason | Count | PnL |
|-------------|-------|-----|
| stop_loss | 577 | -$7,772.74 |
| take_profit | 258 | +$7,326.38 |
| max_holding | 181 | +$702.45 |

## Key Findings

### 1. M5 Scalp is PROFITABLE, Swing is NOT (for small accounts)

- **M5 Scalp**: +$385.39 total across all accounts, WR ~41%
- **Swing**: -$123.59 total, WR ~34%
- M5 Scalp wins despite <50% WR because the avg win is larger than avg loss
- Swing loses because stop-loss hits too frequently (577/1016 = 57% of exits are SL)

### 2. Stop-Loss is the #1 Problem

- 577 out of 1016 trades exited via stop-loss (57%)
- Total SL losses: -$7,772.74
- TP exits: 258 trades for +$7,326.38
- The SL:TP ratio is about 2.2:1 in frequency but the total PnL per SL (-$13.47 avg) vs per TP (+$28.40 avg) shows TPs are bigger — just not frequent enough

### 3. Account A is in CRITICAL Condition

- Started $100, balance now $24.99 (75% drawdown)
- 23 open positions on a $24.99 balance (extreme over-leveraging)
- Swing WR for A: 32.4% — nearly 2 losses for every win
- M5 Scalp is profitable on A (+$110.37) but can't overcome swing losses (-$321.54)

### 4. Account B is MARGIN CALLED Risk

- Free margin: -$88.50 (NEGATIVE)
- This means B cannot open new positions and existing positions could be force-closed
- 19 open positions on $41.26 balance

### 5. Phantom Trades Problem

- 499 trades with ticket=NULL in the DB (46% of all records!)
- These are recorded as "open" but never actually executed in MT5
- This corrupts position counting and PnL calculations

### 6. Swing Opens Too Many Positions

- 75 total open positions (26 on A, 23 on B, 22 on C including 1 phantom)
- Learning mode has no position limits
- Each 5-minute cycle can open a new position regardless of existing ones

## Recommendations

1. **STOP SWING TRADING ON ACCOUNTS A AND B** — they're hemorrhaging capital
2. **Add position limits** — max 5 positions per $100 balance, max 10 per $500
3. **Fix phantom trades** — mark all ticket=NULL as closed with exit_reason="phantom"
4. **Review SL placement** — 57% SL hit rate means SLs are too tight OR entries are wrong
5. **Scale M5 Scalp** — it's the profitable strategy, consider allocating more capital to it
6. **Account B needs immediate attention** — negative free margin = imminent margin call

## Data Integrity Note

- Total DB records: 1091 (75 open + 1016 closed)
- 499 phantom trades (ticket=NULL) need cleanup
- Open positions in MT5: ~60 real positions vs 75 in DB (499 phantom "open" were already excluded from real count)