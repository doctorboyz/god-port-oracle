# MT5 healthcheck "healthy" ≠ terminal logged in

**Date:** 2026-09-24 (deploy of 6-commit batch on mr-bet-bcd)
**Event:** mt5b/c/d recreated → all healthy → engine train started → auto-login FAILED → "(-6, Terminal: Authorization failed)" for ~15 min → recovered by itself once the terminal finished logging in.

## What happened

Sequence on the VPS after `up -d --no-deps oracle-engine-train`:

1. mt5b/c/d recreated (image changed by 8e19a7e), Wine boot ~30 min, healthcheck turned **healthy** (start-period 900s covers it).
2. Engine train started; `ensure_mt5_logged_in()` ran **immediately** → bridge replied "result expired" 3× (bridge still busy) → gave up: "manual VNC login required".
3. From ~02:58, every cycle's `connect()` got `(-6, 'Terminal: Authorization failed')` — because `exposed_initialize()` is a **no-args `mt5.initialize()`**: it only succeeds when the terminal GUI **is already logged in** (terminal auto-logs in via `MT5_CMD_OPTIONS=/login:... /server:...` + password remembered in the profile volume).
4. The healthcheck only proves the **RPyC port** is up — the terminal GUI finishes its auto-login **several minutes after** "healthy".
5. At ~03:03 the terminal finished logging in; every bridge connected on attempt 1, snapshots collected normally. No manual VNC login needed. No restart needed.

## Lesson

**"Container healthy" is not "terminal logged in".** After mt5 container recreation the acceptance signal is NOT the healthcheck — it is, in order:

1. `Connected to MT5 bridge at mt5X:8001 (attempt 1, symbol=XAUUSD)` in engine logs, and
2. `Snapshot #NNN collected (source=bridge, ...)` from the Collector.

The engine's auto-login (`scripts/oracle_runner.py:49`) retries only 3× with 5s sleeps and then gives up for the lifetime of the process — there is no later re-run. Two consequences:

- If train starts during the terminal-login window, auto-login fails and the log says "manual VNC login required" — **don't believe it yet**; the terminal may just still be logging in. Wait one full engine cycle (300s) after all-healthy before concluding anything.
- Since client `connect()` does initialize-only (no login), once the terminal IS logged in everything self-heals on the next cycle — a container restart is usually unnecessary.

**Candidate improvement (not yet implemented):** move auto-login to after first successful `initialize()`, or retry `ensure_mt5_logged_in()` on cycle when bridges fail (ISSUE board candidate).

Related: [[2026-09-23_compose-build-recreates-deps]] (the recreate incident), [[2026-09-23_env-set-does-not-mean-env-used]] (verify from real objects, not config echo — same disease: trusting a proxy signal instead of the real one).