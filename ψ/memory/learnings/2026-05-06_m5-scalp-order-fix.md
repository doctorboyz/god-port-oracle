# M5 Scalp Order Fix & Architecture

**Date**: 2026-05-06
**Context**: M5 Scalp orders all failing with "No response" while swing orders work fine

## Problem
M5 Scalp trader creates 3 separate MT5Bridge connections per cycle (fetch candles, get spread, send order) using `asyncio.run()` for each. The rapid connect/disconnect/reconnect pattern caused "Order rejected: No response" for all M5 Scalp orders across all 3 accounts.

## Root Cause
The `send_order_sync()` method creates a fresh bridge, connects, resolves symbol, fetches tick, sends order, and disconnects — all in one `asyncio.run()` call. Meanwhile, the candle fetch and spread fetch also create separate bridges with their own `asyncio.run()` calls. The rapid cycling of connections may be causing race conditions or state issues with the RPyC bridge server.

## Fix
1. **Shared bridge per cycle**: Modified `run_once()` to create one `MT5Bridge` instance per cycle and pass it to `_fetch_candles()`, `_get_spread()`, and order execution
2. **Retry logic**: Added 3-attempt retry for M5 Scalp order sending with 1-second delay between attempts
3. **Debug logging**: Added `order_send` result type/keys logging and detailed error context in `client.py`

## Cross-Account Note
Account A (Standard) and B (Pro) are on Exness-MT5Trial17 server — tickets start with `1902...`
Account C (Raw Spread) is on Exness-MT5Trial7 server — tickets start with `4006...`
Seeing "C trades in A's VNC" is likely from viewing god-port logs which show all accounts, not from actual cross-contamination.

## Key Code Changes
- `m5_scalp_trader.py`: `_fetch_candles(bridge=None)` and `_get_spread(bridge=None)` accept optional bridge
- `m5_scalp_trader.py`: `run_once()` creates single bridge, passes to sub-methods
- `m5_scalp_trader.py`: Order retry loop (3 attempts, 1s delay)
- `client.py`: Added debug logging for `order_send` result

## Status
Deployed to VPS. M5 Scalp currently holding (no signals). Awaiting next BUY/SELL signal to confirm fix.

## Additional Risks Found
- Account A equity at $46.92 (from $100 start) with 8 open positions and -$34 floating loss
- 75 open trades in DB (55 real, 20 phantom with ticket=None)
- Learning mode has no position limits — opens unlimited positions