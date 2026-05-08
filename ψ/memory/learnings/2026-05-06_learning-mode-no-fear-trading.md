---
name: learning-mode-no-fear-trading
description: Learning mode bypasses all trading blockers while recording factors for post-analysis. Key pattern: append "(learning: X)" to reason strings.
type: project
---

# Learning Mode — No Fear Trading

**Why**: Phase 1 goal is learning, not profitability. Need maximum trade frequency to analyze which factors (confidence, session, spread, ribbon state) correlate with wins. Blocking on any single filter means zero data.

**How to apply**: When LEARNING_MODE=1 env var is set:
- Signal generators: bypass confidence, session, spread, ADX, direction thresholds. Still compute and record all values. Append "(learning: X below Y)" to reason strings.
- Traders: bypass circuit breaker, cooldown, news filter, existing position checks. Log "(learning: bypass X)" for each.
- Direction threshold: M5 lowered from 0.20→0.05, swing from 0.30→0.05.

**Key files**:
- `broky/signals/generator.py` — `learning_mode` param in `generate_signal()`
- `broky/signals/m5_scalp_generator.py` — `learning_mode` param in `generate_m5_scalp_signal()`
- `metty/execution/live_trader.py` — `self.learning_mode` from env var
- `metty/execution/m5_scalp_trader.py` — `self.learning_mode` from env var
- `docker-compose.vps.yml` — `LEARNING_MODE=${LEARNING_MODE:-0}`

**Lesson**: Always copy the working pattern. PersistentMT5Bridge failed silently; MT5Bridge (same as LiveCollector) works reliably. Verify env vars after deploy before checking logs.