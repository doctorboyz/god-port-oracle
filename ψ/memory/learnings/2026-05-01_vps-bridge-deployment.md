---
name: VPS Bridge Deployment
description: MT5 Bridge on x86_64 VPS working — RPyC over network from macOS
type: project
---

# VPS Bridge Deployment

**Date**: 2026-05-01
**Status**: Working — Account A connected

## Architecture

```
[macOS: oracle-engine]
     │
     │ RPyC over network (port 5005/5006/5007)
     │
[Ubuntu x86_64 VPS @ vpsdeluna (31.97.71.229)]
  ┌──────────────────────────────────────────┐
  │ mt5-account-a                            │
  │ Login: 463363150 (Standard $100, 1:2000) │
  │ Port: 5005 → 8001 (RPyC)                │
  │ VNC: 5900 → 3000                         │
  │ Wine Python → MetaTrader5 → Exness        │
  └──────────────────────────────────────────┘
```

## Key Findings

1. **gmag11 image works on x86_64** — Must use as-is (don't build custom Dockerfile). Runs as user `abc`, not `root`
2. **numpy 2.x incompatible** with MetaTrader5 — Must downgrade to `numpy<2` in Wine Python: `wine python -m pip install 'numpy<2' --force-reinstall`
3. **mt5linux 1.0.3 doesn't support `-w` flag** — Use custom RPyC service running in Wine Python instead
4. **MT5 doesn't accept password via command line** — Must login via VNC manually first time
5. **Exness symbol suffix** — XAUUSD is `XAUUSDm` on Exness (suffix `m`)
6. **RPyC namedtuples don't serialize** — Must convert to dict before sending over RPyC
7. **VNC accessible at** `http://vpsdeluna:5900` (user: mt5user, pass: mt5password)

## Bridge Server Script

`mt5_bridge_server.py` — Custom RPyC service running in Wine Python that:
- Exposes MetaTrader5 functions as RPyC service methods
- Converts namedtuples/numpy arrays to dicts/lists for serialization
- Listens on port 8001 inside container, mapped to 5005 externally

## Setup Steps

1. `docker compose -f docker-compose.vps.yml up -d mt5-account-a`
2. Wait ~3 min for gmag11 initialization (MT5 install, Python, etc.)
3. Downgrade numpy: `docker exec -u abc -e DISPLAY=:99 -e WINEPREFIX=/config/.wine CONTAINER wine python -m pip install 'numpy<2' --force-reinstall`
4. Start bridge server: `docker exec -u abc -e DISPLAY=:99 -e WINEPREFIX=/config/.wine CONTAINER bash -c 'nohup wine python /app/mt5_bridge_server.py 8001 > /tmp/bridge.log 2>&1 &'`
5. Login to MT5 via VNC (http://vpsdeluna:5900)
6. Test from macOS: `python3 -c "import rpyc; c=rpyc.connect('vpsdeluna',5005); print(c.root.version())"`

## Pending

- Deploy Account B and C
- Make bridge server auto-start with container (add to entrypoint)
- Create persistent docker-compose with proper health checks