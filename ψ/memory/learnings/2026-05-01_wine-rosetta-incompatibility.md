---
name: Wine Rosetta Incompatibility
description: Wine cannot run inside Docker + Rosetta on Apple Silicon — GDT selector error is fundamental, not fixable
type: project
---

# Wine + Docker + Rosetta (Apple Silicon) Incompatibility

**Date**: 2026-05-01
**Status**: Resolved — use x86_64 VPS instead

## Problem
Wine (any version 6.x through 11.x) crashes inside Docker containers running under Rosetta 2 x86_64 emulation on Apple Silicon macOS. The error manifests as:

```
wine: dlls/ntdll/unix/virtual.c:267: anon_mmap_fixed: Assertion `!((UINT_PTR)start & host_page_mask)' failed.
rosetta error: invalid gdt selector index 5
```

## Root Cause
Rosetta 2 cannot properly translate x86 segment descriptor operations (GDT selectors, FS/GS register manipulation) that Wine requires. This is a fundamental limitation of running x86_64 Windows applications through Rosetta, not a Wine bug.

## What Doesn't Work
- Docker + `--platform linux/amd64` (Rosetta) on macOS ARM
- gmag11/MetaTrader5-Docker on macOS ARM
- Any Wine version inside Rosetta-emulated containers
- Full QEMU emulation via OrbStack (too slow for practical use)

## What Works
- **x86_64 VPS** (Ubuntu, DigitalOcean, AWS EC2, etc.) — Wine runs natively
- **SiliconMetaTrader5** project using Colima + QEMU — slow but works on Apple Silicon
- **Native Windows machine** — obvious but not our target

## Solution
Deploy MT5 Docker containers on an Ubuntu x86_64 VPS:
- gmag11/MetaTrader5-Docker image works perfectly on real x86_64
- Connect from macOS via RPyC over the network
- Same docker-compose.yml with VPS IP instead of container names

## Why: Data Collection Focus
Our goal (Phase 4) is data collection for ML, not low-latency live trading. VPS latency (~100-200ms) is acceptable for collecting candle data and sending orders. For Phase 6 (live trading with millisecond precision), a VPS closer to the broker server would be needed.