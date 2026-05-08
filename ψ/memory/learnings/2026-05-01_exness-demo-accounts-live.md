# Exness Demo Account Configuration — Live

**Created**: 2026-05-01
**IMPORTANT**: Passwords are NOT stored here. Add them to `.env` only.

## Account Details

| Account | Nickname | Type | Login | Server | Balance | Leverage | Signal Group |
|---------|----------|------|-------|--------|---------|----------|-------------|
| A | Standard | Standard | 463363150 | Exness-MT5Trial17 | $100 | 1:2000 | Volume |
| B | Pro | Pro | 463363160 | Exness-MT5Trial17 | $500 | 1:500 | OB/OS |
| C | Raw Spread | Raw Spread | 433532985 | Exness-MT5Trial7 | $1,000 | 1:500 | MA |

## Key Notes

- A and B share server **Exness-MT5Trial17** — same MT5 terminal can connect to both
- C is on **Exness-MT5Trial7** — needs separate MT5 terminal
- B leverage is 1:500 (not 1:2000 as originally planned) — this is fine, adds diversity
- A is 1:2000 (highest risk, smallest capital)
- C is 1:500 with Raw Spread (commission-based, lowest risk)

## Control Variables for ML

| Variable | Account A | Account B | Account C |
|----------|-----------|-----------|-----------|
| Balance | $100 | $500 | $1,000 |
| Leverage | 1:2000 | 1:500 | 1:500 |
| Spread | ~0.2-0.3 pips | ~0.1 pips | 0.0 pips + $3.50/lot |
| Execution | Market | Market | Market |
| Commission | None | None | $3.50/side/lot |

## Setup Steps (Remaining)

1. [ ] Add passwords to `.env` file (MT5_PASSWORD_A, MT5_PASSWORD_B, MT5_PASSWORD_C)
2. [ ] Test MT5 connection locally (Wine + mt5linux)
3. [ ] Test Docker MT5 bridge
4. [ ] Start data collection loop