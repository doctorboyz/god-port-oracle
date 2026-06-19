---
name: x11-lock-watchdog-pattern
description: X11 lock files persist across Docker container restarts, causing KasmVNC to crash. Always clean stale locks before starting VNC. Use watchdog pattern for Wine/MT5 process recovery.
metadata:
  type: learning
  tags: [docker, x11, kasmvnc, mt5, watchdog, container, wine]
---

# X11 Lock + Watchdog Pattern for Container Recovery

## Problem
When a Docker container running KasmVNC + Wine/MT5 restarts, stale X11 lock files (`/tmp/.X99-lock`, `/tmp/.X11-unix/X99`) persist from the previous run. KasmVNC then fails with "Fatal server error: Server is already active for display 99" — which means no X11 display, which means Wine/MT5 terminal crashes in a loop (error 10053 = WSAECONNABORTED).

## Root Cause Chain
1. Container restart → `/tmp/` state preserved (not fresh)
2. Stale X11 lock → KasmVNC won't start → no display :99
3. No display → Wine can't render MT5 terminal → process crash
4. Bridge can't connect → "result expired" errors
5. User sees: can't trade, VNC 502

## Solution (Two-part)

### Part 1: Clean stale locks before KasmVNC start (Phase 0)
```bash
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null
```

### Part 2: Watchdog for MT5 terminal crashes (Phase 4)
Background process that:
- Checks `pgrep -f terminal64.exe` every 10 seconds
- On crash: removes X11 lock, restarts terminal, increments crash counter
- After 10 rapid crashes: stops watchdog, logs VNC URL for manual intervention
- After 60s stability: resets crash counter

## Why This Matters

**Container restart ≠ fresh start.** Docker preserves `/tmp/` state on restart. Any process that creates lock files in `/tmp/` needs explicit cleanup logic in the entrypoint. This is a pattern that applies to any containerized GUI app using X11.

## General Pattern

For any containerized app that:
- Uses X11 display (KasmVNC, Xvfb, VNC)
- Runs Wine processes
- Creates lock files in `/tmp/`

Add cleanup before display start + watchdog for process recovery. The cost of not doing this is random crashes on container restart that look like application bugs but are actually infrastructure issues.

**Why:** This happened multiple times before it was fixed permanently. Recurring infrastructure issues should always get permanent fixes, not manual workarounds.

**How to apply:** When any service crashes twice with the same root cause, immediately write a permanent fix (entrypoint cleanup + watchdog), not another manual restart. Check `/tmp/` for stale state in all container restart scenarios.