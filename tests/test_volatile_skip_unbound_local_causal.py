"""Causal proof test: volatile-skip branch references weighted_score before assignment.

Hypothesis
----------
Bug: generator.py volatile-skip branch (REGIME_VOLATILE_SKIP=1, regime=volatile)
returns a Signal that references `weighted_score` at a point in generate_signal
where it has not been assigned yet (assignment happens much further down, after
the reversal-feature computation). Enabling REGIME_VOLATILE_SKIP on
oracle-engine-train (planned MR-bet config) crashes every cycle with
UnboundLocalError instead of returning a HOLD.

Causal proof
------------
Call generate_signal with REGIME_VOLATILE_SKIP=True and data classified as
volatile. Before fix: raises UnboundLocalError. After fix: returns HOLD with
reason containing "volatile".

This test FAILS (RED) before the fix, PASSES (GREEN) after.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.models import MarketRegime, SignalType  # noqa: E402
from broky.signals import generator as gen_mod  # noqa: E402
from broky.signals.generator import generate_signal  # noqa: E402


def _make_ohlcv(n: int = 120):
    idx = pd.date_range("2026-09-01", periods=n, freq="5min", tz="UTC")
    base = 4000.0
    close = pd.Series([base + (i % 7) * 0.5 for i in range(n)], index=idx)
    high = close + 1.0
    low = close - 1.0
    volume = pd.Series([100.0 + (i % 5) * 10 for i in range(n)], index=idx)
    return close, high, low, volume


class TestVolatileSkipUnboundLocalCausal:
    """Causal proof: volatile-skip path must return HOLD, not crash."""

    def test_volatile_skip_returns_hold_not_unbound_local(self, monkeypatch):
        """RED before fix: UnboundLocalError. GREEN after fix: HOLD 'volatile'."""
        close, high, low, volume = _make_ohlcv()
        monkeypatch.setattr(gen_mod, "REGIME_VOLATILE_SKIP", True)
        # Force volatile classification (ADX>=25 + BW>0.01) — the branch under test.
        monkeypatch.setattr(
            gen_mod, "classify_regime",
            lambda adx, bw=None: MarketRegime.VOLATILE.value,
        )
        signal = generate_signal(
            close, high, low, volume,
            current_price=4000.0,
            timestamp=datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc),
            min_confidence=0.55,
        )
        assert signal.signal_type == SignalType.HOLD
        assert "volatile" in signal.reason

    def test_volatile_skip_off_no_regression(self, monkeypatch):
        """Sanity: flag off → volatile path never reached (legacy behavior)."""
        close, high, low, volume = _make_ohlcv()
        monkeypatch.setattr(gen_mod, "REGIME_VOLATILE_SKIP", False)
        monkeypatch.setattr(
            gen_mod, "classify_regime",
            lambda adx, bw=None: MarketRegime.VOLATILE.value,
        )
        signal = generate_signal(
            close, high, low, volume,
            current_price=4000.0,
            timestamp=datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc),
            min_confidence=0.55,
        )
        assert "volatile regime skipped" not in signal.reason