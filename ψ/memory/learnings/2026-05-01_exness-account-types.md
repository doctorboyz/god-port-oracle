# Exness Account Types — Reference for Demo Account Setup

**Source**: Exness official documentation
**Date**: 2026-05-01

## Account Types Overview

| Type | Spread | Commission | Execution | Min Deposit | Best For |
|------|--------|-----------|-----------|-------------|----------|
| Standard | From 0.2-0.3 pips | No | Market | $10 | All traders, general use |
| Standard Cent | From 0.3 pips | No | Market | $1 | Beginners, cent-account users |
| Pro | From 0.1 pips | No | Instant/Market | $200 | Experienced traders |
| Raw Spread | From 0.0 pips | ~$3.50/side per lot | Market | $200 | Scalpers, high-volume |
| Zero | 0.0 pips on 30+ pairs | From $0.2/side per lot | Market | $200 | Scalpers, EAs |

## Key Details

- **Leverage**: Up to 1:Unlimited available (subject to equity restrictions)
- **Min lot**: 0.01 for all account types
- **Platform**: MT4 and MT5 supported (suffix differs per type)
- **Multiple accounts**: Can create multiple trading accounts per Personal Area

## Account Suffixes

Each account type has a server suffix that identifies it on MT5:
- Standard: specific suffix
- Standard Cent: specific suffix
- Pro: specific suffix
- Raw Spread: specific suffix
- Zero: specific suffix

(Check Exness Personal Area for exact suffixes when creating accounts)

## ML Demo Account Recommendations

For our 3-account ML data collection setup:

| Account | Recommended Type | Why | Target Balance | Leverage |
|---------|-----------------|-----|---------------|----------|
| A (Volume group) | **Standard Cent** | Lowest deposit, good for testing volume signals with small capital | $1 (=100 cents) | 1:2000 |
| B (OB/OS group) | **Standard** | No commission, market execution, moderate capital | $100 | 1:2000 |
| C (MA group) | **Pro** | Instant execution + lowest no-commission spreads, better for trend-following | $200 | 1:500 |

**Why these choices:**
- **Account A (Cent)**: Testing volume signals with smallest capital. Cent account lets us trade with $1 and see real pip movement without risking significant funds.
- **Account B (Standard)**: Mid-range test. No commission means cleaner PnL tracking for OB/OS signals. Market execution is fine for non-scalping signals.
- **Account C (Pro)**: Largest capital, lower leverage. Instant execution avoids slippage on MA/trend signals which may need precise entry. Lower leverage (1:500) tests more conservative risk management.

**Alternative**: If we want to test commission impact on ML, swap Account C to **Raw Spread** or **Zero** — the ML model can learn whether commission-based accounts affect win rate for specific indicator patterns.

## Setup Steps

1. Go to Exness Personal Area
2. Create 3 demo accounts:
   - Account A: Standard Cent, $1, 1:2000 leverage
   - Account B: Standard, $100, 1:2000 leverage
   - Account C: Pro, $200, 1:500 leverage
3. Record credentials (login, password, server) for each
4. Add to `.env` file:
   ```
   MT5_LOGIN_A=...
   MT5_PASSWORD_A=...
   MT5_LOGIN_B=...
   MT5_PASSWORD_B=...
   MT5_LOGIN_C=...
   MT5_PASSWORD_C=...
   ```
5. Note the server suffix for each account type