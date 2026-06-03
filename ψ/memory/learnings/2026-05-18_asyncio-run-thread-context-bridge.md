# asyncio.run() + Thread Context = Fragile for Bridge Reconnection

## Pattern

When calling `asyncio.run()` from non-main threads (e.g., M5 scalp trader running in `threading.Thread`), avoid connect-disconnect-reconnect patterns. Each `asyncio.run()` creates and destroys its own event loop. While this works for single operations (connect → fetch → disconnect in one `asyncio.run()`), doing a second `asyncio.run()` to reconnect can fail silently.

Instead: establish a persistent connection once per cycle using `PersistentMT5Bridge.ensure_connected_sync()`, then reuse it for all operations (candle fetch, spread check, order execution), and disconnect in a `finally` block.

## Context

- M5 scalp trader runs in `threading.Thread` (oracle_runner.py:417)
- Each cycle created `MT5Bridge` and called `fetch_candles_sync()` (connect→fetch→disconnect) then `get_spread_sync()` (reconnect→fetch spread)
- `get_spread_sync()` reconnect failed silently — `logger.debug()` hid the exception
- Result: all M5 scalp cycles returned "spread data unavailable" during London session

## Solution

1. Use `PersistentMT5Bridge` instead of `MT5Bridge`
2. `bridge.ensure_connected_sync()` at cycle start
3. `bridge.fetch_candles_persistent_sync()` instead of `fetch_candles_sync()`
4. `bridge.get_spread_sync()` reuses existing connection (returns True immediately from ping check)
5. `asyncio.run(bridge.disconnect())` in `finally` block

## Related

- Never use `logger.debug()` in exception handlers — minimum `logger.warning()` for failures
- `PersistentMT5Bridge` extends `MT5Bridge` with keep-alive and auto-reconnect
- `ensure_connected_sync()` is only on `PersistentMT5Bridge`, not `MT5Bridge`
