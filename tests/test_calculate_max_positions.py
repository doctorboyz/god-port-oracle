"""Unit tests for `_calculate_max_positions` dynamic position sizing logic.

Tests the formula: min(cap, max(min_positions, floor(equity / equity_per_position)))

- `min_positions` (env `MIN_POSITIONS_{account}`, default 1) is the floor.
- `cap` (env `MAX_POSITIONS_CAP_{account}`, default 5) is the hard ceiling.
- Cap always wins when cap < floor (semantically: cap is a hard limit).

Both `metty.execution.live_trader.LiveTrader._calculate_max_positions` and
`metty.execution.m5_scalp_trader.M5ScalpTrader._calculate_max_positions`
share identical logic. These tests verify the formula directly by binding
the method to a lightweight stub with just the three required attributes
(`_equity_per_position`, `_max_positions_cap`, `_min_positions`), avoiding
full trader construction (which needs MT5 bridge, DB, env vars, etc.).

References: ISSUE-030, learning 2026-06-26_dynamic-max-positions-from-equity.md,
            learning 2026-07-09_min2-h4-fallback-ranging-block.md (Real-A min=2)
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from metty.execution.live_trader import LiveTrader
from metty.execution.m5_scalp_trader import M5ScalpTrader


@dataclass
class _StubTrader:
    """Minimal stub with the attributes the method reads.

    Includes `display_name` because the method's logger.debug call references it
    (lazy-formatted, only triggered when DEBUG logging is enabled during tests).
    """
    _equity_per_position: float
    _max_positions_cap: int
    _min_positions: int = 1
    display_name: str = "test-trader"


def _make_method(trader_cls, equity_per_position: float, cap: int, min_positions: int = 1):
    """Bind the trader class's `_calculate_max_positions` to a stub instance."""
    stub = _StubTrader(
        _equity_per_position=equity_per_position,
        _max_positions_cap=cap,
        _min_positions=min_positions,
    )
    return trader_cls._calculate_max_positions.__get__(stub, _StubTrader)


# Test both trader classes share identical behavior
TRADER_CLASSES = [LiveTrader, M5ScalpTrader]


# ─── Default config: equity_per_position=200, cap=5 ──────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("trader_cls", TRADER_CLASSES, ids=["LiveTrader", "M5ScalpTrader"])
class TestCalculateMaxPositionsDefault:
    """Tests with default config ($200/position, cap=5)."""

    @pytest.mark.parametrize("equity,expected", [
        (0, 1),              # edge: zero equity → 1 (not 0)
        (-100, 1),           # negative equity → 1 (safety)
        (-0.01, 1),          # tiny negative → 1
        (1, 1),              # $1 → floor(1/200)=0 → max(1, 0) = 1
        (199, 1),            # $199 → floor=0 → 1 (just below one position)
        (200, 1),            # $200 → floor=1 → 1 (exactly one position)
        (201, 1),            # $201 → floor=1 → 1
        (399, 1),            # $399 → floor=1 → 1
        (400, 2),            # $400 → floor=2 → 2 (two positions)
        (600, 3),            # $600 → floor=3 → 3
        (800, 4),            # $800 → floor=4 → 4
        (1000, 5),           # $1000 → floor=5 → 5 (exactly at cap)
        (1200, 5),           # $1200 → floor=6, capped at 5
        (10_000, 5),         # very large → capped at 5
        (1_000_000, 5),      # huge → still capped at 5
    ])
    def test_default_scaling(self, trader_cls, equity, expected):
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5)
        assert method(equity) == expected

    def test_returns_int(self, trader_cls):
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5)
        result = method(500.0)
        assert isinstance(result, int), "max_positions must be int (used in comparisons and arithmetic)"

    def test_minimum_is_one(self, trader_cls):
        """Even with $0 equity, must return 1 — never 0 (would block all trading)."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5)
        assert method(0) >= 1
        assert method(-1000) >= 1

    def test_never_exceeds_cap(self, trader_cls):
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5)
        for equity in [1000, 5000, 1_000_000, 1e12]:
            assert method(equity) <= 5


# ─── Custom configs ──────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("trader_cls", TRADER_CLASSES, ids=["LiveTrader", "M5ScalpTrader"])
class TestCalculateMaxPositionsCustomConfig:
    """Tests with non-default equity_per_position and cap values."""

    def test_low_cap_2(self, trader_cls):
        """Cap=2 — even large accounts limited to 2 positions."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=2)
        assert method(200) == 1
        assert method(400) == 2
        assert method(600) == 2  # capped
        assert method(10_000) == 2

    def test_cap_1(self, trader_cls):
        """Cap=1 — always 1 position regardless of equity (cap is hard ceiling)."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=1)
        assert method(0) == 1
        assert method(500) == 1
        assert method(10_000) == 1

    def test_cap_below_floor_cap_wins(self, trader_cls):
        """cap=1, floor=2 — cap wins (hard ceiling), result=1 not 2.

        Formula: min(cap, max(floor, calc)) → min(1, max(2, ...)) = 1.
        Cap is a hard limit that cannot be overridden by the floor.
        """
        method = _make_method(trader_cls, equity_per_position=200.0, cap=1, min_positions=2)
        assert method(0) == 1
        assert method(500) == 1
        assert method(10_000) == 1

    def test_high_equity_per_position(self, trader_cls):
        """$1000/position — needs more equity per slot."""
        method = _make_method(trader_cls, equity_per_position=1000.0, cap=5)
        assert method(999) == 1
        assert method(1000) == 1
        assert method(2000) == 2
        assert method(5000) == 5  # cap
        assert method(9999) == 5  # capped

    def test_tiny_equity_per_position(self, trader_cls):
        """$50/position — small accounts get many slots."""
        method = _make_method(trader_cls, equity_per_position=50.0, cap=10)
        assert method(50) == 1
        assert method(100) == 2
        assert method(500) == 10  # cap
        assert method(10_000) == 10

    def test_fractional_equity_per_position(self, trader_cls):
        """Fractional equity_per_position (e.g. $33.33) — int floor still works."""
        method = _make_method(trader_cls, equity_per_position=33.33, cap=10)
        # 100 / 33.33 = 3.0003 → floor = 3
        assert method(100) == 3
        # 33.33 / 33.33 = 1.0 → floor = 1
        assert method(33.33) == 1


# ─── Edge cases ──────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("trader_cls", TRADER_CLASSES, ids=["LiveTrader", "M5ScalpTrader"])
class TestCalculateMaxPositionsEdgeCases:
    """Edge cases that have caused production bugs."""

    def test_zero_equity_does_not_return_zero(self, trader_cls):
        """Regression: equity=0 must not return 0 (would block all trading)."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5)
        result = method(0)
        assert result == 1, f"equity=0 returned {result}, expected 1"

    def test_negative_equity_safe(self, trader_cls):
        """Negative equity (unrealized loss > balance) must return 1, not crash."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5)
        result = method(-500.50)
        assert result == 1

    def test_boundary_just_below_next_slot(self, trader_cls):
        """$399.99 should give 1, $400 should give 2 — boundary correctness."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5)
        assert method(399.99) == 1
        assert method(400.00) == 2

    def test_boundary_at_cap(self, trader_cls):
        """$1000 (exactly cap*per_position) should give 5, $999.99 gives 4."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5)
        assert method(999.99) == 4
        assert method(1000.00) == 5

    def test_float_equity_not_int_equity(self, trader_cls):
        """Method must handle float equity (MT5 returns float)."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5)
        # Float division then int floor — must not use int division on float
        assert method(450.75) == 2  # 450.75 // 200 = 2.0 → int = 2

    def test_very_small_positive_equity(self, trader_cls):
        """$0.01 equity — must still return 1, not 0."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5)
        assert method(0.01) == 1

    def test_large_equity_no_overflow(self, trader_cls):
        """$1B equity — must not overflow, still returns cap."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5)
        assert method(1_000_000_000) == 5


# ─── Real-A config scenario (min_positions=2 from 2026-07-09) ────────────

@pytest.mark.unit
@pytest.mark.parametrize("trader_cls", TRADER_CLASSES, ids=["LiveTrader", "M5ScalpTrader"])
class TestCalculateMaxPositionsRealAScenario:
    """Verify Real-A scenario with min_positions=2 (2026-07-09 fix).

    Real-A equity had dropped to $346-379 → floor(372/200)=1 → only 1 position
    allowed → 35+ signals blocked in 24h. Fix: MIN_POSITIONS_A=2 so even at
    low equity, 2 simultaneous positions are allowed.

    Config: equity_per_position=200, cap=5, min_positions=2 (Real-A).
    """

    def test_real_a_at_200_usd(self, trader_cls):
        """Real-A at $200 with min=2: 2 positions (was 1 before fix).

        Without min=2: floor(200/200)=1 → max(1, min(5, 1)) = 1.
        With min=2:    min(5, max(2, 1)) = 2.
        """
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5, min_positions=2)
        assert method(200.00) == 2

    def test_real_a_at_78_usd_pre_topup(self, trader_cls):
        """Real-A at $78 (low equity): 2 positions with min=2 (was 1).

        floor(78/200)=0 → min(5, max(2, 0)) = 2.
        Floor protects against position-limit block when equity drops.
        """
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5, min_positions=2)
        assert method(78.00) == 2

    def test_real_a_at_372_usd_today(self, trader_cls):
        """Real-A at $372 (today's low equity): 2 positions, not 1.

        This is the exact scenario that blocked 35+ signals on 2026-07-09.
        floor(372/200)=1 → min(5, max(2, 1)) = 2.
        """
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5, min_positions=2)
        assert method(372.00) == 2

    def test_real_a_at_400_usd(self, trader_cls):
        """Real-A at $400: 2 positions (calculated=2, floor=2, no change)."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5, min_positions=2)
        assert method(400.00) == 2

    def test_real_a_at_600_usd(self, trader_cls):
        """Real-A at $600: 3 positions (calculated=3 > floor=2)."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5, min_positions=2)
        assert method(600.00) == 3

    def test_real_a_at_0_equity(self, trader_cls):
        """Real-A at $0 with min=2: 2 positions (early return uses floor)."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5, min_positions=2)
        assert method(0) == 2


# ─── Default (B/C/D) — min_positions=1, behavior unchanged ───────────────

@pytest.mark.unit
@pytest.mark.parametrize("trader_cls", TRADER_CLASSES, ids=["LiveTrader", "M5ScalpTrader"])
class TestCalculateMaxPositionsDefaultMin:
    """Verify default min_positions=1 (B/C/D demo accounts, no env override).

    These tests confirm the floor change does NOT affect accounts without
    MIN_POSITIONS_{account} env var set — preserving B/C/D demo behavior.
    """

    def test_default_min_is_one(self, trader_cls):
        """Without MIN_POSITIONS env, floor defaults to 1 (B/C/D unchanged)."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5, min_positions=1)
        assert method(0) == 1
        assert method(199) == 1
        assert method(200) == 1
        assert method(400) == 2

    def test_default_zero_equity(self, trader_cls):
        """Default (min=1): $0 equity → 1 position (preserve old behavior)."""
        method = _make_method(trader_cls, equity_per_position=200.0, cap=5, min_positions=1)
        assert method(0) == 1