"""Causal proof test for DrawdownProtector.set_time() in backtest mode.

ISSUE-035: DP set_time fix not verifiable in backtest — need synthetic worst-case test.

Hypothesis (CPT)
----------------
DrawdownProtector uses `datetime.now(timezone.utc)` for cooldown/rollover checks.
In a backtest, wall-clock time does not advance — the loop runs in seconds while
simulated time spans months. Without `set_time()`, `_check_rollover` never fires
(real midnight never crosses), so daily_pnl accumulates across the entire run
instead of resetting at UTC midnight, and `blocked_until` (cooldown) is set to
real_now + 4h which never elapses in real time during the backtest.

The fix: `set_time(timestamp)` lets the backtest drive simulated time. DP then
resets daily_pnl at midnight, expires cooldown correctly, and unblocks.

Causal test design
------------------
The same loss sequence is run through DP with and without `set_time`. The KEY
assertion is that behavior DIFFERS — proving set_time is the causal mechanism.

Concretely:
- After a daily-limit block fires (via record_pnl), the next-day check:
  - WITH set_time: rollover resets daily_pnl + cooldown expired → unblocks.
  - WITHOUT set_time: no rollover + real_now + 4h not elapsed → stays blocked.

If set_time is removed or bypassed, the "with set_time" path behaves like the
"without" path — the test fails (RED). It passes (GREEN) only when set_time
correctly drives both rollover and cooldown expiry.

References
----------
- broky/risk/drawdown_protection.py:94  set_time
- broky/risk/drawdown_protection.py:309 _check_rollover
- broky/risk/drawdown_protection.py:104  _now (the helper that makes set_time work)
- learning: 2026-07-01_dp-daily-limit-pegs-to-peak.md
- retro: 2026-07/01/12.50_e2e-backtest-live-mc-exness-oos.md
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from broky.risk.drawdown_protection import DrawdownProtector


# ─── Helpers ─────────────────────────────────────────────────────────────


def make_dt(day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Build a UTC datetime on a fixed day for deterministic tests.

    Day 1 = 2026-07-01 (a Wednesday, so weekday math is stable for weekly tests).
    """
    return datetime(2026, 7, day, hour, minute, 0, tzinfo=timezone.utc)


def force_daily_limit_block(dp: DrawdownProtector, set_time: bool, day: int = 1):
    """Drive DP through 4 losses × -$5 on $100 → daily_pnl hits 20% → block fires.

    account_limit_pct is set high by callers to isolate the daily mechanism.
    Returns the equity after the losses (so callers can keep using it).
    """
    equity = 100.0
    if set_time:
        dp.set_time(make_dt(day, 10))
    dp.check(equity)  # initialize

    # 4 trades × -$5 = -$20 = 20% of $100 → daily limit fires via record_pnl
    for h in (10, 12, 14, 16):
        ts = make_dt(day, h)
        if set_time:
            dp.set_time(ts)
        can, _ = dp.check(equity)
        assert can, f"Trade should be allowed before daily limit: day={day} h={h}"
        equity -= 5.0
        dp.record_pnl(pnl=-5.0, equity=equity)
    return equity


# ─── Causal proof: set_time drives rollover + cooldown ────────────────────


class TestSetTimeCausalProof:
    """The core causal tests: with vs without set_time, behavior MUST differ."""

    def test_set_time_unblocks_next_day_via_rollover_and_cooldown(self):
        """After Day 1 daily-limit block, Day 2 morning check unblocks.

        With set_time: rollover at midnight resets daily_pnl; cooldown
        (4h from Day 1 16:00) expired by Day 2 10:00 → unblock → can trade.
        """
        dp = DrawdownProtector(
            initial_equity=100.0,
            daily_limit_pct=0.20,
            weekly_limit_pct=0.30,
            account_limit_pct=0.80,  # isolate daily mechanism
            cooldown_hours=4,
        )
        equity = force_daily_limit_block(dp, set_time=True, day=1)

        # State immediately after Day 1: blocked, daily_pnl = -$20
        assert dp.state.blocked is True, "Daily limit should have fired"
        assert dp.state.daily_pnl == -20.0
        assert "Daily" in dp.state.block_reason or "daily" in dp.state.block_reason.lower()

        # Day 2, 10:00 — next-day check
        dp.set_time(make_dt(day=2, hour=10))
        can, reason = dp.check(equity)
        assert can is True, (
            f"With set_time, Day 2 morning should unblock (rollover + cooldown): {reason}"
        )
        # daily_pnl should have reset (either via _check_rollover or _unblock)
        assert dp.state.daily_pnl == 0.0, (
            f"daily_pnl should reset to 0 after midnight: {dp.state.daily_pnl}"
        )

    def test_without_set_time_stays_blocked_next_day(self):
        """Same sequence, NO set_time → Day 2 check stays blocked.

        Without set_time: _now() returns real wall-clock time which barely
        advances during the test (runs in <1s). blocked_until = real_now + 4h
        which hasn't elapsed → check returns False.

        This is the bug — and the proof that set_time causally fixes it.
        """
        dp = DrawdownProtector(
            initial_equity=100.0,
            daily_limit_pct=0.20,
            weekly_limit_pct=0.30,
            account_limit_pct=0.80,
            cooldown_hours=4,
        )
        equity = force_daily_limit_block(dp, set_time=False, day=1)

        # State after Day 1: blocked
        assert dp.state.blocked is True

        # "Day 2" check — but without set_time, _now() is real time. The
        # blocked_until was set to real_now + 4h, which won't have elapsed
        # in the few ms between Day 1 and Day 2 calls.
        can, reason = dp.check(equity)
        assert can is False, (
            "Without set_time, cooldown should not have expired (real time "
            "barely advanced). Stays blocked."
        )

    def test_set_time_causally_changes_behavior(self):
        """Side-by-side: same sequence, with vs without set_time.

        The two runs MUST produce different check() outcomes on the Day 2
        signal. If they were identical, set_time would be a no-op.
        """
        # With set_time
        dp_with = DrawdownProtector(
            initial_equity=100.0, daily_limit_pct=0.20,
            weekly_limit_pct=0.30, account_limit_pct=0.80, cooldown_hours=4,
        )
        eq_with = force_daily_limit_block(dp_with, set_time=True, day=1)
        dp_with.set_time(make_dt(day=2, hour=10))
        can_with, _ = dp_with.check(eq_with)

        # Without set_time
        dp_without = DrawdownProtector(
            initial_equity=100.0, daily_limit_pct=0.20,
            weekly_limit_pct=0.30, account_limit_pct=0.80, cooldown_hours=4,
        )
        eq_without = force_daily_limit_block(dp_without, set_time=False, day=1)
        can_without, _ = dp_without.check(eq_without)

        assert can_with != can_without, (
            "set_time must causally change behavior: "
            f"with={can_with}, without={can_without}. "
            "Identical results mean set_time is a no-op (the fix is broken)."
        )
        assert can_with is True, "With set_time: Day 2 should unblock"
        assert can_without is False, "Without set_time: Day 2 stays blocked"


# ─── Rollover unit tests ──────────────────────────────────────────────────


class TestSetTimeRollover:
    """Unit tests for the rollover mechanism driven by set_time."""

    def test_daily_pnl_resets_at_midnight(self):
        """Cross UTC midnight → daily_pnl resets to 0, daily_start advances."""
        dp = DrawdownProtector(
            initial_equity=100.0, daily_limit_pct=0.20,
            weekly_limit_pct=0.30, account_limit_pct=0.80,
        )
        # Day 1, 23:00 — initialize + record -$5
        dp.set_time(make_dt(day=1, hour=23))
        dp.check(100.0)
        dp.record_pnl(pnl=-5.0, equity=95.0)
        assert dp.state.daily_pnl == -5.0

        # Day 2, 01:00 — past midnight → rollover
        dp.set_time(make_dt(day=2, hour=1))
        dp.check(95.0)
        assert dp.state.daily_pnl == 0.0, (
            f"Daily PnL should reset at midnight: {dp.state.daily_pnl}"
        )

    def test_daily_pnl_does_not_reset_within_same_day(self):
        """Same UTC day, hours apart → no rollover → daily_pnl accumulates."""
        dp = DrawdownProtector(
            initial_equity=100.0, daily_limit_pct=0.20,
            weekly_limit_pct=0.30, account_limit_pct=0.80,
        )
        dp.set_time(make_dt(day=1, hour=10))
        dp.check(100.0)
        dp.record_pnl(pnl=-5.0, equity=95.0)
        assert dp.state.daily_pnl == -5.0

        # Same day, 22:00 — no rollover
        dp.set_time(make_dt(day=1, hour=22))
        dp.check(95.0)
        assert dp.state.daily_pnl == -5.0, (
            f"Same day should NOT reset daily_pnl: {dp.state.daily_pnl}"
        )

    def test_weekly_pnl_resets_on_monday(self):
        """Week runs Wed Jul 1 → Tue Jul 7. Monday Jul 6 → weekly reset.

        We use small losses spread across the week so daily never fires.
        """
        dp = DrawdownProtector(
            initial_equity=100.0,
            daily_limit_pct=0.50,  # high — isolate weekly
            weekly_limit_pct=0.30,
            account_limit_pct=0.80,
        )
        # Wed Jul 1 (day 1) — start
        dp.set_time(make_dt(day=1, hour=10))
        dp.check(100.0)

        # 6 small losses × -$3 = -$18 (18% weekly, under 30%; under 50% daily)
        # Spread across Wed-Sun (days 1-5, all before Monday)
        equity = 100.0
        schedule = [(1, 10), (1, 14), (2, 10), (3, 10), (4, 10), (5, 10)]
        for day, hour in schedule:
            ts = make_dt(day=day, hour=hour)
            dp.set_time(ts)
            can, _ = dp.check(equity)
            assert can, f"Should be allowed (under all limits): day={day}"
            equity -= 3.0
            dp.record_pnl(pnl=-3.0, equity=equity)

        # Sun Jul 5: weekly_pnl = -$18 (18% of $100)
        assert dp.state.weekly_pnl == -18.0

        # Monday Jul 6 (day 6), 10:00 — weekly rollover
        dp.set_time(make_dt(day=6, hour=10))
        dp.check(equity)
        assert dp.state.weekly_pnl == 0.0, (
            f"Weekly PnL should reset on Monday: {dp.state.weekly_pnl}"
        )


# ─── Cooldown / unblock tests ─────────────────────────────────────────────


class TestSetTimeCooldown:
    """Cooldown expiry driven by set_time."""

    def test_cooldown_not_expired_within_4h(self):
        """Block fires at 12:00 → 15:00 still blocked (3h < 4h cooldown)."""
        dp = DrawdownProtector(
            initial_equity=100.0, daily_limit_pct=0.20,
            weekly_limit_pct=0.30, account_limit_pct=0.80, cooldown_hours=4,
        )
        # Day 1, 12:00 — force daily limit
        dp.set_time(make_dt(day=1, hour=12))
        dp.check(100.0)
        for _ in range(4):
            dp.check(100.0)
            dp.record_pnl(pnl=-5.0, equity=95.0)
        assert dp.state.blocked

        # 15:00 same day — 3h later, cooldown still active
        dp.set_time(make_dt(day=1, hour=15))
        can, _ = dp.check(95.0)
        assert can is False, "Within 4h cooldown should still be blocked"

    def test_cooldown_expires_after_4h_with_set_time(self):
        """Block fires at 12:00 → 16:30 unblocked (4h30m > 4h cooldown).

        Note: after unblock, daily_pnl is reset to 0 by _unblock(), so the
        daily limit doesn't re-fire immediately.
        """
        dp = DrawdownProtector(
            initial_equity=100.0, daily_limit_pct=0.20,
            weekly_limit_pct=0.30, account_limit_pct=0.80, cooldown_hours=4,
        )
        dp.set_time(make_dt(day=1, hour=12))
        dp.check(100.0)
        for _ in range(4):
            dp.check(100.0)
            dp.record_pnl(pnl=-5.0, equity=95.0)
        assert dp.state.blocked

        # 16:30 same day — 4h30m > 4h → cooldown expired
        dp.set_time(make_dt(day=1, hour=16, minute=30))
        can, reason = dp.check(95.0)
        assert can is True, (
            f"After 4h+ cooldown, should unblock: {reason}"
        )


# ─── Peak drawdown with set_time ─────────────────────────────────────────


class TestSetTimePeakDrawdown:
    """Peak drawdown triggers across simulated days with set_time."""

    def test_peak_drawdown_fires_after_30pct_from_peak(self):
        """Build peak up to $200, crash to $130 (35% from peak) → block.

        Uses high daily/weekly limits to isolate peak drawdown.
        """
        dp = DrawdownProtector(
            initial_equity=100.0,
            daily_limit_pct=0.80,   # isolate peak
            weekly_limit_pct=0.80,
            account_limit_pct=0.30,  # 30% from peak
        )
        # Day 1: build peak up to $200
        dp.set_time(make_dt(day=1, hour=10))
        dp.check(100.0)
        dp.check(150.0)
        dp.check(200.0)  # peak = $200

        # Day 2: crash to $130 → 35% from $200 peak → over 30% → block
        dp.set_time(make_dt(day=2, hour=10))
        can, reason = dp.check(130.0)
        assert can is False, f"Peak drawdown should block: {reason}"
        assert "Peak drawdown" in reason or "peak" in reason.lower(), \
            f"Reason should mention peak drawdown: {reason}"


# ─── Backtest-loop integration test ──────────────────────────────────────


class TestBacktestLoopPattern:
    """Mimics the actual backtest loop pattern in scripts/backtest_live_monte_carlo.py.

    Per-signal: set_time → check → trade → record_pnl.
    """

    def test_backtest_loop_with_set_time_blocks_after_daily_limit(self):
        """8 signals across 2 days, daily_limit_pct=0.10 (smaller for cleaner test).

        Day 1: 2 trades ok, 2nd triggers daily limit via record_pnl. Signals 3,4
        blocked at check (cooldown not expired in simulated time).
        Day 2: rollover + cooldown expired. 2 trades ok, 2nd triggers daily
        limit. Signals 3,4 blocked.

        Total: 4 trades taken, 4 blocks at check.
        """
        dp = DrawdownProtector(
            initial_equity=100.0,
            daily_limit_pct=0.10,  # 10% = $10 → fires after 2 × -$5 losses
            weekly_limit_pct=0.80,
            account_limit_pct=0.80,  # isolate daily
            cooldown_hours=4,
        )
        signals = [
            (make_dt(day=1, hour=10), "BUY"),   # trade 1, daily=-$5
            (make_dt(day=1, hour=12), "SELL"),  # trade 2, daily=-$10 → block (cooldown until 16:00)
            (make_dt(day=1, hour=14), "BUY"),   # blocked (cooldown 14:00 < 16:00)
            (make_dt(day=1, hour=15, minute=30), "SELL"),  # blocked (15:30 < 16:00)
            (make_dt(day=2, hour=10), "BUY"),   # rollover + 18h>4h → unblock; trade 3, daily=-$5
            (make_dt(day=2, hour=12), "SELL"),  # trade 4, daily=-$10 → block (cooldown until 16:00)
            (make_dt(day=2, hour=14), "BUY"),   # blocked
            (make_dt(day=2, hour=15, minute=30), "SELL"),  # blocked
        ]

        equity = 100.0
        blocks = []
        trades_taken = 0

        for ts, _ in signals:
            dp.set_time(ts)
            can, reason = dp.check(equity)
            if not can:
                blocks.append((ts, reason))
                continue
            equity -= 5.0
            trades_taken += 1
            dp.record_pnl(pnl=-5.0, equity=equity)

        # 4 trades taken (2 per day), 4 blocks (2 per day at signals 3,4 and 7,8)
        assert trades_taken == 4, f"Expected 4 trades, got {trades_taken}"
        assert len(blocks) == 4, f"Expected 4 blocks, got {len(blocks)}: {blocks}"
        # All blocks should be daily-limit related
        for ts, reason in blocks:
            assert "Daily" in reason or "daily" in reason.lower(), \
                f"Block should be daily limit: {reason}"

    def test_backtest_loop_without_set_time_blocks_earlier_and_persists(self):
        """Same loop, NO set_time → behavior MUST differ.

        Without set_time: daily_pnl never resets, cooldown never expires (real
        time barely advances). After the 2nd trade triggers daily limit, all
        subsequent signals stay blocked — no rollover, no unblock.

        This is the causal proof: removing set_time changes the result.
        """
        # Build two DPs — same config, only set_time differs
        def make_dp():
            return DrawdownProtector(
                initial_equity=100.0, daily_limit_pct=0.10,
                weekly_limit_pct=0.80, account_limit_pct=0.80,
                cooldown_hours=4,
            )

        signals = [
            (make_dt(day=1, hour=10), "BUY"),
            (make_dt(day=1, hour=12), "SELL"),
            (make_dt(day=1, hour=14), "BUY"),
            (make_dt(day=1, hour=15, minute=30), "SELL"),
            (make_dt(day=2, hour=10), "BUY"),
            (make_dt(day=2, hour=12), "SELL"),
            (make_dt(day=2, hour=14), "BUY"),
            (make_dt(day=2, hour=15, minute=30), "SELL"),
        ]

        def run(use_set_time: bool):
            dp = make_dp()
            equity = 100.0
            blocks = []
            trades = 0
            for ts, _ in signals:
                if use_set_time:
                    dp.set_time(ts)
                can, reason = dp.check(equity)
                if not can:
                    blocks.append((ts, reason))
                    continue
                equity -= 5.0
                trades += 1
                dp.record_pnl(pnl=-5.0, equity=equity)
            return trades, blocks

        trades_with, blocks_with = run(use_set_time=True)
        trades_without, blocks_without = run(use_set_time=False)

        # Causal assertion: behavior MUST differ
        assert (trades_with, blocks_with) != (trades_without, blocks_without), (
            "set_time must causally change behavior. "
            f"With: {trades_with} trades, {len(blocks_with)} blocks. "
            f"Without: {trades_without} trades, {len(blocks_without)} blocks."
        )
        # With set_time: 4 trades + 4 blocks (deterministic, documented above)
        assert trades_with == 4, f"With set_time: expected 4 trades, got {trades_with}"
        assert len(blocks_with) == 4, f"With set_time: expected 4 blocks, got {len(blocks_with)}"
        # Without set_time: 2 trades then everything blocked (no rollover/unblock)
        assert trades_without == 2, (
            f"Without set_time: only 2 trades before daily limit, got {trades_without}"
        )
        assert len(blocks_without) == 6, (
            f"Without set_time: 6 signals blocked (no rollover/unblock), "
            f"got {len(blocks_without)}"
        )