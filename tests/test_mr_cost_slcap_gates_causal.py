"""Causal proof test: TradeBlocker sl_dollar_cap + cost_coverage gates.

Hypothesis
----------
MR-bet needs two new pre-trade verdicts in TradeBlocker (single source of
truth for block verdicts):
- sl_dollar_cap: if |entry - SL| in PRICE units > cap ($12), block — the trade
  is SKIPPED, never shrunk (shrinking SL changes the trade thesis).
- cost_coverage: if TP distance < tp_cost_mult × spread price (3× round-trip
  cost), block — friction eats the edge (High-WR grinder lesson: PF 0.99).
Both default OFF (0.0) so m5_scalp and Real-A are untouched.

Causal proof
------------
Pure TradeBlocker:
- SL 12.5 > cap 12 → blocked 'sl_dollar_cap'; SL 8 → pass
- TP 0.5 < 3 × 0.30 spread → blocked 'cost_coverage'; TP 1.0 → pass
- defaults (no kwargs) → both pass always
Env wiring: LiveTrader SL_CAP_B=12 / MR_COST_MULT_B=3.0 reach the blocker.

This test FAILS (RED) before the feature exists (TypeError), PASSES after.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from broky.risk.trade_blocker import BlockInput, TradeBlocker  # noqa: E402


def _base_input(**overrides):
    """A clean trade that passes all legacy checks."""
    base = dict(
        open_positions=0, max_positions=1,
        daily_trades_today=0, weekly_trades_this_week=0,
        lots=0.01, risk_pct=0.005,
        sl_distance_pct=0.30,
        equity=1000.0, margin_required=5.0, free_margin=900.0,
    )
    base.update(overrides)
    return BlockInput(**base)


class TestSLDollarCapCausal:
    """sl_dollar_cap: SL wider than the $ cap → skip (never shrink)."""

    def test_sl_over_cap_blocks(self):
        b = TradeBlocker(max_sl_distance_price=12.0)
        v = b.check(_base_input(sl_distance_price=12.5))
        assert v.blocked, "SL 12.5 > cap 12.0 must block"
        assert v.block_name == "sl_dollar_cap"

    def test_sl_under_cap_passes(self):
        b = TradeBlocker(max_sl_distance_price=12.0)
        v = b.check(_base_input(sl_distance_price=8.0))
        assert not v.blocked

    def test_default_cap_off_never_blocks(self):
        """Default 0.0 = gate off — m5_scalp / Real-A unchanged."""
        b = TradeBlocker()
        v = b.check(_base_input(sl_distance_price=25.0))
        assert not v.blocked


class TestCostCoverageCausal:
    """cost_coverage: TP must cover ≥ mult × spread (round-trip cost)."""

    def test_tp_below_cost_blocks(self):
        b = TradeBlocker(tp_cost_mult=3.0)
        v = b.check(_base_input(tp_distance_price=0.5, spread_price=0.30))
        assert v.blocked, "TP 0.5 < 3 × 0.30 = 0.90 must block"
        assert v.block_name == "cost_coverage"

    def test_tp_above_cost_passes(self):
        b = TradeBlocker(tp_cost_mult=3.0)
        v = b.check(_base_input(tp_distance_price=1.0, spread_price=0.30))
        assert not v.blocked

    def test_default_mult_off_never_blocks(self):
        b = TradeBlocker()
        v = b.check(_base_input(tp_distance_price=0.05, spread_price=0.50))
        assert not v.blocked


class TestEnvWiringToTrader:
    """LiveTrader env knobs SL_CAP_* / MR_COST_MULT_* must reach the blocker."""

    def test_env_reaches_blocker(self, monkeypatch):
        monkeypatch.setenv("SL_CAP_B", "12.0")
        monkeypatch.setenv("MR_COST_MULT_B", "3.0")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t._trade_blocker.max_sl_distance_price == 12.0
        assert t._trade_blocker.tp_cost_mult == 3.0

    def test_no_env_defaults_off(self, monkeypatch):
        monkeypatch.delenv("SL_CAP_B", raising=False)
        monkeypatch.delenv("MR_COST_MULT_B", raising=False)
        monkeypatch.delenv("SL_CAP", raising=False)
        monkeypatch.delenv("MR_COST_MULT", raising=False)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t._trade_blocker.max_sl_distance_price == 0.0
        assert t._trade_blocker.tp_cost_mult == 0.0