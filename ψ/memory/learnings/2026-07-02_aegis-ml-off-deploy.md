# Lesson: AEGIS-only deploy — disabling ML filter exposed the gate chain

**Date**: 2026-07-02
**Source**: session 46d8bb6f — deploy GP-AEGIS to demo B/C/D with ML filter off
**Related**: [[2026-07-01_e2e-backtest-entry-trailing-block]], [[2026-07-01_live-monte-carlo-backtest]], [[2026-07-01_block-verification]]

## What I learned

**"No trades" is a chain of gates, not one diagnosis.** When demo B/C/D had 0 trades in 24h post-deploy, the cause was not one thing — it was the chain:

```
signal generation → ML filter (V4+v6 OR-gate) → reversal gate → counter-trend penalty x0.5
  → confidence threshold 0.45 → lot sizing (risk 1% of $200 = $2 → 0.002 lots)
  → broker min 0.01 → REJECT
```

Counting rejection reasons in the log showed 380 hold / 10 skip with these gates firing:
- ranging regime hold (~138×) — ADX<25 = "ranging = พัก" iron law
- counter-trend penalty x0.5 (~30×) — confidence dropped from 0.65 → 0.07-0.12, below 0.45 threshold
- ml_lot_too_small:0.0000 (9×) — risk 1% on $200 balance computes lot 0.002 < broker min 0.01
- m5 scalp asian session block (34×)
- ML filter P(LOSS)=87% (3×)

## The proof

Local MC test that achieved PF 4.13-5.40 used `scripts/backtest_live_monte_carlo.py` — imports only risk modules (sizing, drawdown_protection, circuit_breaker, trade_blocker) + reversal eval + trailing replay. **No `trade_outcome_predictor` import.** So local test = AEGIS alone.

Demo deploy had `ML_FILTER_ENABLED=1` with V4+v6 OR-gate ensemble → ML was the dominant blocker.

Per user request, set `ML_FILTER_ENABLED=0` in `docker-compose.vps.yml` for `oracle-engine-train` (B/C/D) only. Real-A (`oracle-engine`) kept `ML_FILTER_ENABLED=1` — IRON LAW preserved.

After `docker compose up -d oracle-engine-train`, AEGIS opened its first trade within 60 seconds:
```
TRADE_FILLED | account=B | dir=BUY | price=4059.17 | lots=0.01 | sl=4047.64 | tp=4088.01 | ticket=2166523300
```

One config change → one restart → one trade. Clean causation: ML filter was the blocker.

## Why this matters

1. **Comparing local PF to live 0-trades was a category error.** Local had no ML filter; live had ML filter. Same AEGIS, different gate stack. Always state which gates are active when reporting a result.

2. **The "disable the suspected gate and restart" pattern is the fastest causation proof.** Faster than hours of log archaeology. One line, one restart, one trade.

3. **signal_group per account is a design pattern.** B=volume, C=ob_os, D=ma. Each account tests a different signal source on the same market. They will not trade in sync. C's "counter-trend penalty killed it" is correct behavior, not a bug — at 05:28 UTC, C saw a counter-trend BUY without made_higher_high → reversal gate rejected → penalty x0.5 → conf 0.07. That is AEGIS working as designed.

## How to apply

- When debugging "system not trading": count rejection reasons per gate, do not guess. `docker logs ... | grep -oE "reason='[^']*'" | sort | uniq -c | sort -rn`
- Before claiming "X matches Y" between backtest and live: list the gates active in each. Different gate stack = different test.
- For small-balance demo accounts: risk 1% may compute lot below broker min 0.01. Either top up balance, raise risk%, or use the largest-balance account as primary test.
- For causation proof: disable the suspected gate, restart, watch. If trade opens in <2 min → that gate was the blocker.

## Open follow-ups

- `ml_lot_too_small` still blocks B/D from testing exit features. C ($849) is the viable exit-feature test account.
- `live_collector: No numeric types to aggregate` (5× in 24h) — transient D1 disconnect. Track rate; if it climbs, investigate bridge stability.
- Demo DD thresholds (10/20/50% / 2h) differ from system report (20/30/30% / 4h) — decide which is canonical.