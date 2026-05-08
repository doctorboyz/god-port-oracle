---
name: Exness XAUUSDm Order Send
description: How order_send works with Exness demo accounts — filling modes, AutoTrading, market hours
type: project
---

# Exness XAUUSDm Order Send

**Date**: 2026-05-03
**Status**: Verified (order pipeline works, market closed on Sunday)

## Order Send Parameters

```python
# Working order_send call for Exness XAUUSDm
result = c.root.order_send(
    action=1,               # TRADE_ACTION_DEAL
    symbol='XAUUSDm',       # Exness symbol name (note the 'm' suffix)
    volume=0.01,            # Minimum lot
    order_type=0,           # ORDER_TYPE_BUY (1=SELL)
    price=ask,              # Use ask for BUY, bid for SELL
    deviation=20,           # Max slippage
    magic=99999,            # Magic number
    comment='bridge_test',
    type_time=0,             # ORDER_TIME_GTC
    type_filling=0,         # ORDER_FILLING_FOK (filling_mode=3 supports both FOK and IOC)
    position=0,             # 0 for new order, ticket number for closing
)
```

## Filling Modes

- `symbol_info('XAUUSDm')['filling_mode'] = 3` — supports both FOK (0) and IOC (2)
- Use `type_filling=0` (FOK) for Exness — IOC (2) returns "Unsupported filling mode"

## Common retcodes

| retcode | Meaning | Action |
|---------|---------|--------|
| 10009 | TRADE_RETCODE_DONE | Order filled |
| 10018 | MARKET_CLOSED | Wait for market hours |
| 10027 | AUTOTRADING_DISABLED | Enable AutoTrading in MT5 |
| 10030 | UNSUPPORTED_FILLING_MODE | Change type_filling |

## Closing a Position

```python
# Close BUY position by SELL
close = c.root.order_send(
    action=1, symbol='XAUUSDm', volume=0.01,
    order_type=1,            # ORDER_TYPE_SELL
    position=ticket,        # Position ticket to close
    price=bid,              # Use bid for SELL
    deviation=20, magic=99999, comment='close',
    type_time=0, type_filling=0
)
```

## Account Types

| Account | Login | Type | Balance | Leverage | Server |
|---------|-------|------|---------|----------|--------|
| A | 463363150 | Standard | $100 | 1:2000 | Exness-MT5Trial17 |
| B | 463363160 | Pro | $500 | 1:500 | Exness-MT5Trial17 |
| C | 433532985 | Raw Spread | $1,000 | 1:500 | Exness-MT5Trial7 |

## Requirements

1. **AutoTrading must be enabled** in MT5 (Ctrl+E or toolbar button) — green = enabled
2. **Symbol must be in Market Watch** — call `symbol_select('XAUUSDm', True)` first, then wait ~1s for data to load
3. **Market must be open** — Gold market closed on weekends (retcode 10018)
4. **First MT5 login must be manual via VNC** — API login doesn't work