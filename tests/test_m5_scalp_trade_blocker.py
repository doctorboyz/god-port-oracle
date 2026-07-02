"""Causal proof tests for m5_scalp_trader TradeBlocker gate (Task #82, 2026-07-02).

Hypothesis
----------
m5_scalp_trader lacked the TradeBlocker hard safety gate (audit 2026-07-02
mismatch #3). Without it, misconfigured SL/lots/risk_pct that pass DP+CB could
still reach the broker — no hard_max_lots, no margin_safety, no sl_sanity, no
anti-churn daily/weekly count limit.

Task #82 ported TradeBlocker from live_trader into m5_scalp_trader:
  - Constructed in __init__ after DrawdownProtector
  - _get_free_margin method added (queries real MT5 free_margin, ISSUE-060)
  - Gate wired in _run_once_connected after min-lot reject, before calendar context

These tests exercise the TradeBlocker directly through the M5ScalpTrader's
`_trade_blocker` instance to prove:
  1. hard_max_lots block fires when lots > 0.50
  2. margin_safety block fires when free_margin < margin_required * safety_factor
  3. position_limit block fires when open_positions >= max_positions
  4. Normal trade passes all blocks

References
----------
- Ported from live_trader in Task #82 (audit 2026-07-02)
- Sister implementation: metty/execution/live_trader.py:1797-1827
- broky/risk/trade_blocker.py:TradeBlocker, BlockInput, BlockVerdict
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _make_trader():
    from metty.execution.m5_scalp_trader import M5ScalpTrader
    return M5ScalpTrader(account="C", dry_run=True)


class TestTradeBlockerWired:
    """Task #82: TradeBlocker must be constructed in __init__."""

    def test_trade_blocker_instance_exists(self):
        from broky.risk.trade_blocker import TradeBlocker
        t = _make_trader()
        assert hasattr(t, "_trade_blocker"), "m5 trader must construct _trade_blocker"
        assert isinstance(t._trade_blocker, TradeBlocker), (
            "_trade_blocker must be a TradeBlocker instance"
        )

    def test_get_free_margin_method_exists(self):
        t = _make_trader()
        assert callable(getattr(t, "_get_free_margin", None)), (
            "_get_free_margin method must be ported from live_trader"
        )

    def test_gate_is_wired_in_run_once_connected(self):
        """The gate code must appear in _run_once_connected source."""
        import inspect
        from metty.execution.m5_scalp_trader import M5ScalpTrader
        src = inspect.getsource(M5ScalpTrader._run_once_connected)
        assert "self._trade_blocker.check" in src, (
            "TradeBlocker.check must be called in _run_once_connected"
        )
        assert "BlockInput(" in src, "BlockInput must be constructed in the gate"
        assert "trade_blocker:" in src, (
            "rejection reason must tag trade_blocker for triage"
        )


class TestHardMaxLotsBlock:
    """hard_max_lots block must fire when lots exceed the hard cap."""

    def test_blocks_lots_above_hard_cap(self):
        from broky.risk.trade_blocker import BlockInput
        t = _make_trader()
        # Force a high hard_max_lots env at construct time would require re-init;
        # instead use the configured blocker with lots > hard_max_lots
        hard_cap = t._trade_blocker.hard_max_lots
        verdict = t._trade_blocker.check(BlockInput(
            open_positions=0,
            max_positions=5,
            daily_trades_today=0,
            weekly_trades_this_week=0,
            lots=hard_cap + 0.10,  # over cap
            risk_pct=0.01,
            sl_distance_pct=0.40,
            equity=1000.0,
            margin_required=10.0,
            free_margin=1000.0,
            learning_mode=False,
        ))
        assert verdict.blocked is True, (
            f"lots={hard_cap + 0.10} > hard_max_lots={hard_cap} must block"
        )
        assert verdict.block_name == "hard_max_lots", (
            f"expected block_name='hard_max_lots', got '{verdict.block_name}'"
        )

    def test_allows_lots_at_hard_cap(self):
        from broky.risk.trade_blocker import BlockInput
        t = _make_trader()
        hard_cap = t._trade_blocker.hard_max_lots
        verdict = t._trade_blocker.check(BlockInput(
            open_positions=0,
            max_positions=5,
            daily_trades_today=0,
            weekly_trades_this_week=0,
            lots=hard_cap,  # exactly at cap
            risk_pct=0.01,
            sl_distance_pct=0.40,
            equity=1000.0,
            margin_required=10.0,
            free_margin=1000.0,
            learning_mode=False,
        ))
        # At cap should not trigger hard_max_lots block (may trigger others; verify not hard_max_lots)
        if verdict.blocked:
            assert verdict.block_name != "hard_max_lots", (
                "lots exactly at hard_max_lots must not fire hard_max_lots block"
            )


class TestMarginSafetyBlock:
    """margin_safety block must fire when free_margin < margin_required * safety_factor."""

    def test_blocks_when_free_margin_insufficient(self):
        from broky.risk.trade_blocker import BlockInput
        t = _make_trader()
        # margin_required=100, safety=0.80 → need free_margin ≥ 80
        verdict = t._trade_blocker.check(BlockInput(
            open_positions=0,
            max_positions=5,
            daily_trades_today=0,
            weekly_trades_this_week=0,
            lots=0.05,
            risk_pct=0.01,
            sl_distance_pct=0.40,
            equity=1000.0,
            margin_required=100.0,
            free_margin=50.0,  # < 80 → blocked
            learning_mode=False,
        ))
        assert verdict.blocked is True, (
            "free_margin=50 < margin_required*0.80=80 must block on margin_safety"
        )
        assert verdict.block_name == "margin_safety", (
            f"expected block_name='margin_safety', got '{verdict.block_name}'"
        )

    def test_allows_when_free_margin_sufficient(self):
        from broky.risk.trade_blocker import BlockInput
        t = _make_trader()
        verdict = t._trade_blocker.check(BlockInput(
            open_positions=0,
            max_positions=5,
            daily_trades_today=0,
            weekly_trades_this_week=0,
            lots=0.05,
            risk_pct=0.01,
            sl_distance_pct=0.40,
            equity=1000.0,
            margin_required=100.0,
            free_margin=200.0,  # > 80 → OK
            learning_mode=False,
        ))
        if verdict.blocked:
            assert verdict.block_name != "margin_safety", (
                "free_margin=200 ≥ 80 must not fire margin_safety block"
            )


class TestPositionLimitBlock:
    """position_limit block must fire when open_positions >= max_positions."""

    def test_blocks_when_position_limit_exceeded(self):
        from broky.risk.trade_blocker import BlockInput
        t = _make_trader()
        verdict = t._trade_blocker.check(BlockInput(
            open_positions=5,
            max_positions=5,  # at limit
            daily_trades_today=0,
            weekly_trades_this_week=0,
            lots=0.05,
            risk_pct=0.01,
            sl_distance_pct=0.40,
            equity=1000.0,
            margin_required=10.0,
            free_margin=1000.0,
            learning_mode=False,
        ))
        assert verdict.blocked is True, (
            "open_positions=5 >= max_positions=5 must block on position_limit"
        )
        assert verdict.block_name == "position_limit", (
            f"expected block_name='position_limit', got '{verdict.block_name}'"
        )


class TestNormalTradeAllowed:
    """A normal, well-configured trade must pass all TradeBlocker checks."""

    def test_normal_trade_passes(self):
        from broky.risk.trade_blocker import BlockInput
        t = _make_trader()
        verdict = t._trade_blocker.check(BlockInput(
            open_positions=0,
            max_positions=5,
            daily_trades_today=0,
            weekly_trades_this_week=0,
            lots=0.01,           # small lot
            risk_pct=0.01,        # 1% risk
            sl_distance_pct=0.40, # normal SL distance
            equity=1000.0,
            margin_required=4.0,  # 0.01 * 100 * 2000 / 500 = 4
            free_margin=1000.0,
            learning_mode=False,
        ))
        assert verdict.blocked is False, (
            f"normal trade must pass all checks; got blocked by {verdict.block_name}: {verdict.reason}"
        )


class TestRecordTradeOpenWired:
    """Task #83: record_trade_open must be called at 3 sites (dry-run, live, scale-in)."""

    def test_three_record_trade_open_call_sites(self):
        import inspect
        from metty.execution.m5_scalp_trader import M5ScalpTrader
        src = inspect.getsource(M5ScalpTrader)
        n = src.count("self._drawdown_protector.record_trade_open()")
        assert n >= 3, (
            f"expected ≥3 record_trade_open() call sites (dry-run, live, scale-in), got {n}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])