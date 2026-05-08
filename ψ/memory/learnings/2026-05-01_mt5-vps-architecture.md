---
name: MT5 Bridge on VPS
description: Deploying MT5 Docker containers on x86_64 VPS, connecting from macOS via RPyC
type: project
---

# MT5 Bridge on x86_64 VPS

**Why**: Wine cannot run inside Docker + Rosetta on Apple Silicon. The gmag11/MetaTrader5-Docker image works perfectly on real x86_64 hardware.

## Architecture (Updated)

```
[macOS: oracle-engine]
     │
     │ RPyC over network (port 5005/5006/5007)
     │
[Ubuntu x86_64 VPS]
  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
  │mt5-acct-a    │ │mt5-acct-b    │ │mt5-acct-c    │
  │$100, 1:2000  │ │$500, 1:500   │ │$1000, 1:500  │
  │port 5005     │ │port 5006     │ │port 5007     │
  │Wine+Xvfb+MT5 │ │Wine+Xvfb+MT5 │ │Wine+Xvfb+MT5 │
  │Python+mt5linux│ │Python+mt5linux│ │Python+mt5linux│
  └──────────────┘ └──────────────┘ └──────────────┘
```

## Setup Steps

1. Provision Ubuntu x86_64 VPS (DigitalOcean Droplet $6/mo or similar)
2. Install Docker + Docker Compose on VPS
3. Clone repo on VPS, configure .env with MT5 credentials
4. `docker compose up -d` on VPS
5. Update metty config on macOS to point to VPS IP instead of container names
6. Test RPyC connection from macOS

## Key Changes from Local Docker Plan
- `docker-compose.yml`: Remove `platform: linux/amd64` (VPS is native x86_64)
- `metty/config/settings.yaml`: Bridge hosts point to VPS IP instead of container names
- VPS firewall: Open ports 5005, 5006, 5007
- SSH tunnel optional for additional security