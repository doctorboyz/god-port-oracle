---
name: mt5-bridge-json-transfer
description: MT5 bridge JSON transfer method — 0.18s for 500 candles vs 30+ seconds netref
type: project
---

# MT5 Bridge JSON Transfer Method

## Performance Breakthrough
- **Old method**: `copy_rates_from_pos` → netref list → per-key round-trips → 30+ seconds for 500 candles
- **New method**: `copy_rates_from_pos_json` → JSON string → single transfer → 0.18s for 500 candles
- **~166x speedup** for large datasets

## Server-side Implementation (docker/mt5/mt5_bridge_server.py)
```python
def exposed_copy_rates_from_pos_json(self, symbol, timeframe, start_pos, count):
    """Returns JSON string instead of netref list."""
    import MetaTrader5 as mt5
    return _numpy_to_json(mt5.copy_rates_from_pos(symbol, timeframe, start_pos, count))
```

## Client-side Handling (metty/bridge/client.py)
```python
# In get_candles():
json_str = conn.root.copy_rates_from_pos_json(resolved, tf, 0, count)
local_rates = json.loads(json_str)  # Single transfer, local parsing
df = pd.DataFrame(local_rates)
```

## Symbol Name Discovery
- Exness Standard account uses **XAUUSDm** (micro lot)
- Exness Pro/Raw Spread accounts use **XAUUSD**
- `_resolve_symbol()` tries aliases automatically: XAUUSDm → XAUUSD → XAUUSD.i → XAUUSDb

## Resampler Fix
- `resample_timeframe()` now handles both Title Case and lowercase column names
- Also handles DataFrames with RangeIndex (from `reset_index()`) by detecting timestamp columns
- Returns columns in the same casing as input

**Why**: RPyC netref proxies block `.keys()`, `.get()`, `.values()` — making large data transfers impractically slow. JSON method transfers entire dataset in one network call.

**How to apply**: Always use `_json` methods for candle data. Use `_netref_to_dict()` with known columns for small data (account info, tick data).