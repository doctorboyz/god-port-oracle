---
name: gmag11 Race Condition Fix
description: gmag11 start.sh creates root-owned files in background — must wait for PID exit, not just file existence
type: project
---

# gmag11 Race Condition Fix

**Date**: 2026-05-03
**Status**: Fixed and deployed

## Problem

gmag11's `start.sh` runs in background (`&`) and creates files in `/config/.wine` owned by root throughout its 7-step process. Our entrypoint was polling for `terminal64.exe` to exist and then chowning, but gmag11 was STILL running and creating root-owned files.

Three iterations of fixes:
1. **Pre-init chown** — chown before Phase 1. Failed: gmag11 still creates files after chown
2. **Pre + post-init chown** — chown before AND after Phase 1. Failed: gmag11 still running during post-init chown
3. **Wait for PID** — `wait $GMAG11_PID` to ensure gmag11 fully exits before chowning. **This works.**

## The Fix

```bash
/original_start.sh &
GMAG11_PID=$!

# Wait for gmag11 to FULLY complete (not just for terminal64.exe)
elapsed=0
while kill -0 ${GMAG11_PID} 2>/dev/null && [ ${elapsed} -lt ${GMAG11_TIMEOUT} ]; do
    sleep 10
    elapsed=$((elapsed + 10))
done

# NOW safe to fix ownership
chown -R abc:abc /config
```

## Why File-Polling Isn't Enough

- `terminal64.exe` appears at step 2 of gmag11's 7-step process
- Steps 3-7 continue creating root-owned files (Python install, pip packages, mt5linux, etc.)
- Polling for file existence only tells you the file exists, not that the process is done

## Lesson

**When modifying files created by a background process, always wait for the process to exit.** File-existence polling is a heuristic, not a guarantee. `wait $PID` is deterministic.