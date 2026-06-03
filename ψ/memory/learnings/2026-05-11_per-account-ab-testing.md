# Per-Account A/B/C Strategy Testing

**Date**: 2026-05-11
**Context**: Live trading showed 21.5% WR, -$604.58 loss. Root cause: SL too tight (2x ATR), confidence threshold too low (0.45).

## Key Insight

XAUUSD needs at least 3x ATR for stop-loss. 2x ATR ≈ $10-12 SL gets hit by normal gold volatility. Per-account strategy tuning enables simultaneous testing of different parameter sets.

## Configuration

| Account | ATR Mult | RR Ratio | Min Conf | Philosophy |
|---------|----------|----------|----------|------------|
| A (Standard) | 3.0 | 3.0 | 0.35 | Wide SL, accept more trades, give room |
| B (Pro) | 2.5 | 2.5 | 0.45 | Balanced approach |
| C (Raw Spread) | 2.0 | 2.0 | 0.60 | Conservative, fewer but higher quality |

## Implementation

- `metty/execution/live_trader.py`: Per-account overrides from env vars
- `metty/execution/m5_scalp_trader.py`: Same overrides for M5 scalp
- `.env`: A/B/C config values
- `docker-compose.vps.yml`: Passthrough env vars

## Why

- **Why**: Single config across all accounts is a blunt instrument. Different accounts can test different hypotheses simultaneously.
- **How to apply**: Monitor results per account over 1-2 sessions. Promote winning config. The env var approach means no code changes needed to adjust — just update .env and redeploy.

## RPyC Lessons (Reinforced)

- Cannot use `.get()` or `.keys()` on netref dicts — use bracket access or `"key" in dict`
- Exness requires ORDER_FILLING_FOK (type_filling=0), not IOC
- order_send uses positional args, not dict kwargs

## Anti-Rationalization Note

Initial reaction was "market conditions are bad" — truth was simpler: parameters were wrong for XAUUSD volatility. The 21.5% WR wasn't market fault; it was configuration fault.