---
name: vps-mt5-bridge-status
description: VPS MT5 bridge connection status — accounts B and C working, A needs VNC login
type: project
---

# VPS MT5 Bridge Status (2026-05-04)

## Working Accounts
| Account | Port | Symbol | Status | Notes |
|---------|------|--------|--------|-------|
| B (Pro) | 5006 | XAUUSD | Working | Full candle data, sentiment, snapshots |
| C (Raw Spread) | 5007 | XAUUSD | Working | Full candle data, bid/ask=0 (market closed) |
| A (Standard) | 5005 | XAUUSDm? | **Needs VNC login** | Error: -10004 No IPC connection |

## How to Fix Account A
1. Open VNC at `http://100.68.106.101:5900` (noVNC web interface)
2. Login to MT5 terminal with account 463363150
3. Server: Exness-MT5Trial17
4. After manual login, the RPyC bridge will work automatically

## VPS SSH Access
```
Host: vpsdeluna (or 100.68.106.101)
User: root
Key: ~/.ssh/id_ed25519_server
```

## Container Ports
| Container | VNC Port | RPyC Port | Internal |
|-----------|----------|-----------|----------|
| mt5a | 5900 | 5005 | 8001 |
| mt5b | 5901 | 5006 | 8001 |
| mt5c | 5902 | 5007 | 8001 |

## Live Collector Status
- Account B: ✅ Working, `source=bridge`, collecting snapshots every 5 min
- Account C: ✅ Working, `source=bridge`
- Account A: ❌ Needs VNC login, falls back to CSV
- Total snapshots in DB: 16,572+ (including historical CSV data)

**Why**: After container restarts, MT5 terminals need manual VNC login to establish broker connection. The RPyC bridge cannot login programmatically — it only exposes the Python API.

**How to apply**: When containers restart, check `conn.root.initialize()` first. If it returns False, VNC login is required. Use `vncdeluna:5900/5901/5902` ports.