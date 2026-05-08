---
name: Wine Prefix Ownership Bug
description: gmag11 start.sh creates /config/.wine owned by root on fresh volumes — must chown to abc
type: project
---

# Wine Prefix Ownership Bug

**Date**: 2026-05-04
**Status**: Fixed in entrypoint.sh, pending VPS deployment

## Problem

On fresh Docker volumes, gmag11's `start.sh` runs as root and creates `/config/.wine` owned by `root:root`. When our entrypoint later runs `sudo -u abc wine python /app/mt5_bridge_server.py`, Wine refuses: `wine: '/config/.wine' is not owned by you`.

This only affects fresh deployments. mt5a worked because its volume was preserved from a previous deployment where the prefix was already owned by `abc`.

## Fix

Added `chown -R abc:abc /config` after Phase 1 (gmag11 init completes) and before Phase 1.5 (pip commands as user abc):

```bash
echo "[Phase 1] Fixing /config ownership for user abc..."
chown -R abc:abc /config
```

## Why not Dockerfile?

Cannot fix in Dockerfile because:
1. Volume `/config` is mounted at runtime, not build time
2. gmag11's init creates the Wine prefix during container startup
3. Ownership must be fixed after init completes, not before

## Verification

After deploying, check inside container:
```bash
docker exec mt5b ls -la /config/.wine
# Should show: drwxr-xrpx X abc 911 /config/.wine
```