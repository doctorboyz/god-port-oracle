---
name: RPyC Netref Dict Limitations
description: RPyC remote dicts only support bracket access, not .get() or dict() — use explicit args
type: project
---

# RPyC Netref Dict Limitations

**Date**: 2026-05-03
**Status**: Active pattern — must follow

## Problem

When passing dicts over RPyC from macOS to Wine Python (or vice versa), the dicts become "netref" proxy objects. These proxies:

1. **Don't support `.get()`** — `AttributeError: cannot access 'get'`
2. **Can't be passed to `dict()`** — same error
3. **Can't be used as MqlTradeRequest** — MT5's `order_send()` needs a local Python dict, not a netref proxy
4. **Bracket access works** — `result['key']` is fine

## Solution

For `order_send` and similar functions that need a dict constructed inside Wine Python:
- Pass **individual arguments** (not a dict) over RPyC
- Construct the dict inside the bridge server (Wine Python side)
- Return results as plain dicts (the bridge already uses `_to_dict()` for this)

## Example (WRONG)

```python
# From macOS — this FAILS
result = c.root.order_send({'action': 1, 'symbol': 'XAUUSDm', ...})
# Error: cannot access 'get' (bridge tries request.get('action', ...))
```

## Example (CORRECT)

```python
# From macOS — pass explicit args
result = c.root.order_send(
    action=1, symbol='XAUUSDm', volume=0.01, order_type=0,
    price=ask, deviation=20, magic=99999, comment='test',
    type_time=0, type_filling=2
)
# Bridge constructs dict inside Wine Python and calls mt5.order_send()
```

## Also Affected

- Any `result.get('key')` on a dict returned from RPyC — use `result['key']` instead
- `copy_rates_from`, `account_info`, `symbol_info_tick` — all return plain dicts via `_to_dict()`, bracket access works fine