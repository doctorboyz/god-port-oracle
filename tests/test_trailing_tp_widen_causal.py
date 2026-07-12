"""Causal proof test: trailing TP params cause win:loss inversion (Fix #1, 2026-07-12).

Hypothesis
----------
Real-A post-deploy win:loss = 0.82:1 (winner $6.79, loser $8.24) inverts the design
RR_RATIO=2.5:1. Bug occurs because trailing TP defaults
  - trailing_activation_pct = 0.20% (arm too low — triggers at $8 MFE)
  - trailing_trail_pct       = 0.10% (trail too tight — exits on smallest pullback)
choke winners before they reach the 2.5R TP. Losers hit full SL unchanged →
win:loss inverts.

Causal proof
------------
Same price path (BUY, entry=4100, SL=4088, TP=4130, RR=2.5) with normal pullbacks:
  - Tight params (0.20/0.10) → exits at trailing_tp (~$10.89 gain, never sees TP)
  - Wide params  (0.40/0.20) → exits at take_profit ($30 gain = 2.5R achieved)

If the test demonstrates wider params produce higher exit gain AND reach TP on the
same path, the cause is proven: trailing TP defaults choke winners.

The env-var override test (TestTrailingTPEnvOverride) verifies the fix mechanism:
TRAILING_ACTIVATION_PCT_A / TRAILING_TRAIL_PCT_A override the defaults per account.
This test FAILS before the env hook is added, PASSES after.

References
----------
- Learning: ψ/memory/learnings/2026-07-12_real-a-post-deploy-3fixes-check.md
- Production code: metty/execution/live_trader.py:1242-1273 (trailing TP decision)
- Sister: metty/execution/m5_scalp_trader.py (same logic)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _simulate_trailing_exit(
    entry: float,
    sl: float,
    tp: float,
    prices: list[float],
    arm_pct: float,
    trail_pct: float,
    direction: str = "BUY",
) -> tuple[str, float, float]:
    """Mirror production trailing TP decision (live_trader.py:1242-1273).

    Walks `prices` bar-by-bar, returns (exit_reason, exit_price, peak_mfe_dollars).
    Order of checks per bar matches production: SL floor → trailing → TP.
    """
    mfe = 0.0
    for price in prices:
        if direction == "BUY":
            favorable = price - entry
            if favorable > mfe:
                mfe = favorable
            if sl > 0 and price <= sl:
                return "stop_loss", sl, mfe
            gain_pct = mfe / entry * 100.0
            if gain_pct >= arm_pct:
                peak = entry + mfe
                trailing_level = peak * (1 - trail_pct / 100.0)
                if price <= trailing_level:
                    return "trailing_tp", round(trailing_level, 2), mfe
            if tp > 0 and price >= tp:
                return "take_profit", tp, mfe
        else:  # SELL
            favorable = entry - price
            if favorable > mfe:
                mfe = favorable
            if sl > 0 and price >= sl:
                return "stop_loss", sl, mfe
            gain_pct = mfe / entry * 100.0
            if gain_pct >= arm_pct:
                trough = entry - mfe
                trailing_level = trough * (1 + trail_pct / 100.0)
                if price >= trailing_level:
                    return "trailing_tp", round(trailing_level, 2), mfe
            if tp > 0 and price <= tp:
                return "take_profit", tp, mfe
    return "open", prices[-1], mfe


# Price path: BUY entry=4100, SL=4088 (-12, ~0.29%), TP=4130 (+30, ~0.73%, RR=2.5).
# Path reaches TP at the end with two normal pullbacks along the way.
BUY_PATH = [4105.0, 4115.0, 4110.0, 4120.0, 4115.0, 4130.0]


class TestTrailingTPChokesWinnersCausalProof:
    """Causal proof: trailing TP defaults are the cause of win:loss inversion."""

    def test_tight_params_exit_at_trailing_tp_before_TP_reached(self):
        """With current production defaults (0.20/0.10), the path exits at
        trailing_tp with a small gain — never reaches the 2.5R TP."""
        exit_reason, exit_price, _ = _simulate_trailing_exit(
            entry=4100.0, sl=4088.0, tp=4130.0, prices=BUY_PATH,
            arm_pct=0.20, trail_pct=0.10, direction="BUY",
        )
        assert exit_reason == "trailing_tp", (
            f"Tight params should exit at trailing_tp, got {exit_reason}"
        )
        gain = exit_price - 4100.0
        # Trailing exits well below TP gain of $30 — this is the choke
        assert gain < 15.0, (
            f"Tight trailing should choke winner below $15, got ${gain:.2f}"
        )

    def test_wide_params_let_path_reach_TP(self):
        """With wider params (0.40/0.20), the same path survives pullbacks and
        reaches the 2.5R TP — win:loss restored toward design."""
        exit_reason, exit_price, _ = _simulate_trailing_exit(
            entry=4100.0, sl=4088.0, tp=4130.0, prices=BUY_PATH,
            arm_pct=0.40, trail_pct=0.20, direction="BUY",
        )
        assert exit_reason == "take_profit", (
            f"Wide params should reach TP, got {exit_reason}"
        )
        gain = exit_price - 4100.0
        assert gain == pytest.approx(30.0, abs=0.01), (
            f"TP should lock $30 (2.5R), got ${gain:.2f}"
        )

    def test_wide_params_higher_exit_than_tight_on_same_path(self):
        """Direct A/B comparison: same path, only params differ → wide params
        produce higher exit gain. This proves params cause the gap."""
        _, tight_exit, _ = _simulate_trailing_exit(
            entry=4100.0, sl=4088.0, tp=4130.0, prices=BUY_PATH,
            arm_pct=0.20, trail_pct=0.10, direction="BUY",
        )
        _, wide_exit, _ = _simulate_trailing_exit(
            entry=4100.0, sl=4088.0, tp=4130.0, prices=BUY_PATH,
            arm_pct=0.40, trail_pct=0.20, direction="BUY",
        )
        tight_gain = tight_exit - 4100.0
        wide_gain = wide_exit - 4100.0
        assert wide_gain > tight_gain * 2.0, (
            f"Wide params should more than double the winner: "
            f"tight=${tight_gain:.2f}, wide=${wide_gain:.2f}"
        )

    def test_sell_path_symmetric(self):
        """Symmetric SELL path: tight chokes, wide reaches TP."""
        sell_path = [4095.0, 4085.0, 4090.0, 4080.0, 4085.0, 4070.0]
        tight_reason, tight_exit, _ = _simulate_trailing_exit(
            entry=4100.0, sl=4112.0, tp=4070.0, prices=sell_path,
            arm_pct=0.20, trail_pct=0.10, direction="SELL",
        )
        wide_reason, wide_exit, _ = _simulate_trailing_exit(
            entry=4100.0, sl=4112.0, tp=4070.0, prices=sell_path,
            arm_pct=0.40, trail_pct=0.20, direction="SELL",
        )
        assert tight_reason == "trailing_tp"
        assert wide_reason == "take_profit"
        tight_gain = 4100.0 - tight_exit
        wide_gain = 4100.0 - wide_exit
        assert wide_gain > tight_gain * 2.0


class TestTrailingTPEnvOverride:
    """GREEN test for the fix: env vars TRAILING_ACTIVATION_PCT_A / _TRAIL_PCT_A
    override the RiskConfig defaults per account. FAILS before env hook added,
    PASSES after."""

    def test_env_overrides_trailing_activation_pct_for_account_A(self, monkeypatch):
        monkeypatch.setenv("TRAILING_ACTIVATION_PCT_A", "0.40")
        monkeypatch.setenv("TRAILING_TRAIL_PCT_A", "0.20")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="A", dry_run=True)
        assert t.risk.trailing_activation_pct == pytest.approx(0.40, abs=1e-6), (
            f"TRAILING_ACTIVATION_PCT_A=0.40 should override default, "
            f"got {t.risk.trailing_activation_pct}"
        )
        assert t.risk.trailing_trail_pct == pytest.approx(0.20, abs=1e-6), (
            f"TRAILING_TRAIL_PCT_A=0.20 should override default, "
            f"got {t.risk.trailing_trail_pct}"
        )

    def test_no_env_keeps_default(self, monkeypatch):
        monkeypatch.delenv("TRAILING_ACTIVATION_PCT_A", raising=False)
        monkeypatch.delenv("TRAILING_TRAIL_PCT_A", raising=False)
        monkeypatch.delenv("TRAILING_ACTIVATION_PCT", raising=False)
        monkeypatch.delenv("TRAILING_TRAIL_PCT", raising=False)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="A", dry_run=True)
        assert t.risk.trailing_activation_pct == pytest.approx(0.20, abs=1e-6)
        assert t.risk.trailing_trail_pct == pytest.approx(0.10, abs=1e-6)

    def test_per_account_isolation(self, monkeypatch):
        """A uses wider, B keeps default (no env) — isolation per account."""
        monkeypatch.setenv("TRAILING_ACTIVATION_PCT_A", "0.40")
        monkeypatch.setenv("TRAILING_TRAIL_PCT_A", "0.20")
        monkeypatch.delenv("TRAILING_ACTIVATION_PCT_B", raising=False)
        monkeypatch.delenv("TRAILING_TRAIL_PCT_B", raising=False)
        from metty.execution.live_trader import LiveTrader
        ta = LiveTrader(account="A", dry_run=True)
        tb = LiveTrader(account="B", dry_run=True)
        assert ta.risk.trailing_activation_pct == pytest.approx(0.40, abs=1e-6)
        assert tb.risk.trailing_activation_pct == pytest.approx(0.20, abs=1e-6)

    def test_m5_scalp_trader_also_reads_env(self, monkeypatch):
        """m5_scalp_trader must read the same env vars (parity with swing)."""
        monkeypatch.setenv("TRAILING_ACTIVATION_PCT_A", "0.40")
        monkeypatch.setenv("TRAILING_TRAIL_PCT_A", "0.20")
        from metty.execution.m5_scalp_trader import M5ScalpTrader
        t = M5ScalpTrader(account="A", dry_run=True)
        assert t.risk.trailing_activation_pct == pytest.approx(0.40, abs=1e-6)
        assert t.risk.trailing_trail_pct == pytest.approx(0.20, abs=1e-6)