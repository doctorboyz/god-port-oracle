# MT5: symbol_info.spread is static, not live

**Date**: 2026-05-16
**Context**: M5 scalp strategy produced zero signals for 10+ days because `_get_spread()` used `mt5.symbol_info()` which returns `spread=0` (static default). Fixed by using `mt5.symbol_info_tick()` to compute actual spread from bid/ask.
**Source**: rrr: god-port-oracle

## Key Insight

`mt5.symbol_info(symbol).spread` returns a **static reference value** (0 by default), NOT the current market spread. This is documented in MT5 API but easy to overlook because the field name "spread" suggests live data.

**Actual spread**: `(symbol_info_tick.ask - symbol_info_tick.bid) / symbol_info_tick.point`

**How to apply**: Always use `symbol_info_tick` for runtime spread filtering. The static `symbol_info.spread` is useless for live trading decisions. Any spread check using symbol_info will silently fail (return 0 or None), blocking strategies that depend on spread data.

## Also applies to

- M1 scalp trader (scalp_trader.py `_get_spread()`) — same pattern, same fix
- Any code path that compares spread to a threshold for trade eligibility
