---
name: exness-filling-mode
description: Exness requires ORDER_FILLING_FOK (0) not IOC (2). Retcode 10030 = unsupported filling mode.
type: project
---

# Exness Filling Mode

**Why**: Orders were failing with retcode 10030 (unsupported filling mode). The code was using `ORDER_FILLING_IOC = 2` but Exness requires `ORDER_FILLING_FOK = 0`.

**How to apply**: Always use `ORDER_FILLING_FOK = 0` for Exness demo accounts. The `filling_mode` in `symbol_info` returns 3 (supports both), but only FOK actually works.

**Key files**:
- `metty/bridge/client.py` — `ORDER_FILLING_FOK = 0` constant and both `order_send` calls