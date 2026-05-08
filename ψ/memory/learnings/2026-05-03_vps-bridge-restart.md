---
name: VPS Bridge Restart Architecture
description: How the MT5 bridge auto-restarts with containers — entrypoint phases, user abc, VNC login
type: project
---

# VPS Bridge Restart Architecture

**Date**: 2026-05-04
**Status**: Working — All 3 accounts verified (A, B, C)

## Architecture

```
Container restart flow:
  Phase 0: Start nginx + KasmVNC (VNC web interface)
  Phase 1: Run gmag11 start.sh (Wine, MT5, Python) — WAIT for it to fully exit
  Phase 1.5: chown /config to abc, then fix numpy + install rpyc (as user abc)
  Phase 2: Start MT5 terminal (as user abc)
  Phase 3: Start bridge server (as user abc)
```

## Key Findings

1. **gmag11 start.sh MUST finish before chown** — gmag11 runs as root and creates root-owned files throughout its execution. Chowning after just `terminal64.exe` exists is too early. Must `wait` for gmag11's PID to exit first.
2. **gmag11 runs as user abc (uid 911)** — But its start.sh runs as root initially, creating files as root
3. **Wait for gmag11 init to complete** — Use `wait $PID` with timeout instead of polling for terminal64.exe
4. **KasmVNC + nginx must be started manually** — Our entrypoint bypasses s6-overlay
5. **nginx proxies to port 6901 (not 6900)** — KasmVNC serves both HTTP and websocket on 6901
6. **numpy downgrade must happen at runtime** — Wine needs Xvfb to run pip
7. **rpyc must be installed in Wine Python** — Bridge server runs under Wine Python
8. **MT5 first login must be manual via VNC** — Password not accepted via API
9. **Container names: mt5a, mt5b, mt5c** — Short, clean names
10. **Bridge listens on port 8001 internally** — Docker maps to 5005/5006/5007 externally
11. **VNC ports: 5900/5901/5902** — Mapped from container port 3000
12. **RPyC dicts don't support .get()** — Use bracket notation instead: `info['balance']` not `info.get('balance')`

## VNC Access

- Account A: http://vpsdeluna:5900 (mt5user/mt5password)
- Account B: http://vpsdeluna:5901
- Account C: http://vpsdeluna:5902

## Testing from macOS

```python
import rpyc
for port, name in [(5005,'A'), (5006,'B'), (5007,'C')]:
    c = rpyc.connect('vpsdeluna', port)
    c.root.initialize()
    info = c.root.account_info()
    print(f'{name}: login={info["login"]}, balance={info["balance"]}')
```

## Verified Accounts (2026-05-04)

| Account | Login | Balance | Server |
|---------|-------|---------|--------|
| A (Standard) | 463363150 | $100 | Exness-MT5Trial17 |
| B (Pro) | 463363160 | $500 | Exness-MT5Trial17 |
| C (Raw Spread) | 433532985 | $1,000 | Exness-MT5Trial7 |