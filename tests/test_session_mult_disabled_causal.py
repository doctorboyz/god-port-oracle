"""Causal proof test: SESSION_CONFIDENCE_MULT_DISABLED — Asian-hour starvation fix.

Hypothesis
----------
SESSION_CONFIDENCE_MULTIPLIER[ASIAN] = 0.70 is live in generator.py (the
"disabled" comment is stale). MR confidence is capped at 0.65, so 0.65 × 0.70
= 0.455 < MIN_CONFIDENCE 0.55 — golden hours UTC 0/1/6 (Asian session) become
mathematically unreachable. MR-bet needs SESSION_CONFIDENCE_MULT_DISABLED=1 on
the train container so session multipliers stop applying.

Causal proof
------------
MR SELL at band extreme, timestamp 02:00 UTC (Asian session):
- flag OFF (RED): confidence 0.65 × 0.70 = 0.455 < 0.55 → HOLD
- flag ON (GREEN): confidence stays 0.65 → SELL fires

This test FAILS (RED) before the flag exists, PASSES (GREEN) after.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.models import SignalType  # noqa: E402
from broky.signals import generator as gen_mod  # noqa: E402
from broky.signals.generator import generate_signal  # noqa: E402


class _FixedBoll:
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


def _mr_asian(monkeypatch):
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
    monkeypatch.setattr(gen_mod, "TRENDING_HARD_BLOCK", True)


class TestSessionMultDisabledCausal:
    """Causal proof: Asian-hour multiplier starves MR signals unless disabled."""

    def _gen(self, monkeypatch, flag: bool):
        _mr_asian(monkeypatch)
        monkeypatch.setattr(gen_mod, "SESSION_CONFIDENCE_MULT_DISABLED", flag)
        close, high, low, volume = _candles()
        return generate_signal(
            close, high, low, volume,
            current_price=118.0,  # band extreme → MR SELL
            timestamp=datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc),  # ASIAN
            min_confidence=0.55,
        )

    def test_flag_off_asian_starves_mr(self, monkeypatch):
        """RED baseline: 0.65 × 0.70 = 0.455 < 0.55 → the starvation bug."""
        sig = self._gen(monkeypatch, flag=False)
        assert sig.signal_type == SignalType.HOLD, (
            "Legacy behavior: Asian multiplier must starve the MR signal (the bug)"
        )

    def test_flag_on_asian_mr_fires(self, monkeypatch):
        """GREEN: SESSION_CONFIDENCE_MULT_DISABLED=1 → conf 0.65 → SELL fires."""
        sig = self._gen(monkeypatch, flag=True)
        assert sig.signal_type == SignalType.SELL, (
            f"With session mult disabled the Asian-hour MR SELL must fire, "
            f"got {sig.signal_type.value} reason={sig.reason!r}"
        )
        assert sig.confidence >= 0.60, (
            f"confidence must not be session-scaled, got {sig.confidence}"
        )

    def test_flag_off_non_asian_not_affected(self, monkeypatch):
        """Sanity: flag off + overlap session (×1.1) — legacy path unchanged."""
        _mr_asian(monkeypatch)
        monkeypatch.setattr(gen_mod, "SESSION_CONFIDENCE_MULT_DISABLED", False)
        close, high, low, volume = _candles()
        sig = generate_signal(
            close, high, low, volume,
            current_price=118.0,
            timestamp=datetime(2026, 9, 21, 14, 0, tzinfo=timezone.utc),  # OVERLAP
            min_confidence=0.55,
        )
        assert sig.signal_type == SignalType.SELL