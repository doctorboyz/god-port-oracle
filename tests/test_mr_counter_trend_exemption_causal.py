"""Causal proof test: MR counter-trend exemption (triple-kill fix).

Hypothesis
----------
A ranging MR signal is counter-trend by nature (BUY at lower band in a bearish
D1, SELL at upper band in a bullish D1). Three existing gates kill every MR
signal: (1) generator COUNTER_TREND_CONFIDENCE_MULT ×0.5, (2) the D1/H4 trend
filter hard-block, (3) live_trader gate 4a1. In MR-only mode
(TRENDING_HARD_BLOCK=1 + regime=ranging) gates 1-2 must exempt the signal;
the trader gate must exempt when trader._mr_mode and signal regime is ranging.

Causal proof
------------
Generator: MR SELL at band extreme with d1_trend=bullish —
- flag OFF (RED): counter-trend penalty + trend filter → HOLD/blocked
- flag ON (GREEN): exemption → SELL survives at full confidence
Trader gate helper: ta=-1 + regime=ranging + _mr_mode → NOT blocked;
_mr_mode off (Real-A) → still blocked; regime trending → still blocked.

This test FAILS (RED) before the fix, PASSES (GREEN) after.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.models import Signal, SignalType  # noqa: E402
from broky.signals import generator as gen_mod  # noqa: E402
from broky.signals.generator import generate_signal  # noqa: E402


class _FixedBoll:
    """Stub Bollinger: lower=100, middle=110, upper=120 — price 118 → pos 0.90."""

    def __init__(self):
        self.upper = pd.Series([120.0])
        self.middle = pd.Series([110.0])
        self.lower = pd.Series([100.0])


def _candles(n: int = 120):
    idx = pd.date_range("2026-09-01", periods=n, freq="5min", tz="UTC")
    close = pd.Series([110.0 + (i % 5) * 0.2 for i in range(n)], index=idx)
    high = close + 1.0
    low = close - 1.0
    volume = pd.Series([100.0 + (i % 5) * 10 for i in range(n)], index=idx)
    return close, high, low, volume


def _mr_scenario(monkeypatch):
    """Ranging MR setup: ADX 15, fixed bands, price at upper band → MR SELL."""
    def fake_scores(close, high, low, volume):
        scores = {
            "ema_cross": 0.1, "ema_trend": 0.05, "adx": 0.6,
            "macd": -0.5, "bollinger": 0.4, "volume": 0.5,
        }
        return scores, 15.0
    monkeypatch.setattr(gen_mod, "calculate_indicator_scores", fake_scores)
    monkeypatch.setattr(
        gen_mod, "calculate_bollinger",
        lambda close, period=20, std_dev=2.0: _FixedBoll(),
    )


class TestMRCounterTrendExemptionCausal:
    """Causal proof: MR-only mode must exempt ranging MR from trend gates."""

    def _gen(self, monkeypatch, flag: bool):
        _mr_scenario(monkeypatch)
        monkeypatch.setattr(gen_mod, "TRENDING_HARD_BLOCK", flag)
        close, high, low, volume = _candles()
        return generate_signal(
            close, high, low, volume,
            current_price=118.0,  # band_position 0.90 → MR SELL
            timestamp=datetime(2026, 9, 21, 18, 30, tzinfo=timezone.utc),
            d1_trend="bullish", h4_trend="bullish",  # SELL is counter-trend
            min_confidence=0.55,
        )

    def test_flag_off_mr_sell_blocked(self, monkeypatch):
        """RED baseline: legacy gates kill the MR SELL vs bullish D1."""
        sig = self._gen(monkeypatch, flag=False)
        assert sig.signal_type == SignalType.HOLD, (
            "Legacy behavior: counter-trend MR SELL must be blocked (this is the bug)"
        )

    def test_flag_on_mr_sell_survives(self, monkeypatch):
        """GREEN: MR-only mode exempts the ranging MR SELL from trend gates."""
        sig = self._gen(monkeypatch, flag=True)
        assert sig.signal_type == SignalType.SELL, (
            f"TRENDING_HARD_BLOCK=1 + regime=ranging must exempt MR SELL from "
            f"counter-trend gates, got {sig.signal_type.value} reason={sig.reason!r}"
        )
        # No penalty applied either — confidence must be the raw MR confidence
        # (0.65 cap), not halved.
        assert sig.confidence >= 0.60, (
            f"MR exemption must not halve confidence, got {sig.confidence}"
        )

    def test_flag_on_but_trending_regime_not_exempt(self, monkeypatch):
        """Exemption is regime-scoped: a trending-regime counter-trend signal
        must still be blocked even with the flag on (no blanket bypass)."""
        def fake_scores(close, high, low, volume):
            scores = {
                "ema_cross": -0.6, "ema_trend": -0.3, "adx": 1.2,
                "macd": -0.7, "bollinger": 0.2, "volume": 0.5,
            }
            return scores, 30.0  # ADX 30 → trending path, strong SELL score
        monkeypatch.setattr(gen_mod, "calculate_indicator_scores", fake_scores)
        monkeypatch.setattr(gen_mod, "TRENDING_HARD_BLOCK", True)
        close, high, low, volume = _candles()
        sig = generate_signal(
            close, high, low, volume,
            current_price=110.0,
            timestamp=datetime(2026, 9, 21, 18, 30, tzinfo=timezone.utc),
            d1_trend="bullish", h4_trend="bullish",
            min_confidence=0.55,
        )
        # Note: with TRENDING_HARD_BLOCK on, ADX 30 → trending_hard_block HOLD
        # anyway; the point is it never leaks through as an exempt counter-trend.
        assert "trending_hard_block" in sig.reason or sig.signal_type == SignalType.HOLD


class TestTraderGate4a1MRExemption:
    """Trader gate 4a1 helper: MR-only exemption is mode+regime scoped."""

    def _signal(self, regime: str, ta: int = -1):
        return Signal(
            signal_type=SignalType.SELL, confidence=0.65, price=4000.0,
            timestamp=datetime(2026, 9, 21, 18, 30, tzinfo=timezone.utc),
            indicators={"trend_alignment": ta},
            regime=regime,
        )

    def _trader(self, monkeypatch, mr_mode: bool):
        monkeypatch.setattr(gen_mod, "TRENDING_HARD_BLOCK", mr_mode)
        from metty.execution.live_trader import LiveTrader
        return LiveTrader(account="B", dry_run=True)

    def test_mr_mode_ranging_exempt(self, monkeypatch):
        t = self._trader(monkeypatch, mr_mode=True)
        sig = self._signal("ranging")
        assert t._counter_trend_gate_mr_exempt(sig) is False, (
            "MR mode + ranging regime must exempt the counter-trend rejection"
        )

    def test_mr_mode_off_still_blocks(self, monkeypatch):
        """Real-A guard: without MR mode, counter-trend gate unchanged."""
        t = self._trader(monkeypatch, mr_mode=False)
        sig = self._signal("ranging")
        assert t._counter_trend_gate_mr_exempt(sig) is True

    def test_mr_mode_trending_regime_still_blocks(self, monkeypatch):
        t = self._trader(monkeypatch, mr_mode=True)
        sig = self._signal("trending")
        assert t._counter_trend_gate_mr_exempt(sig) is True

    def test_aligned_signal_never_blocks(self, monkeypatch):
        t = self._trader(monkeypatch, mr_mode=True)
        sig = self._signal("ranging", ta=1)
        assert t._counter_trend_gate_mr_exempt(sig) is False