"""Causal proof test: per-call boll_threshold for the ranging MR signal.

Hypothesis
----------
MR-bet variants need per-account Bollinger extremity thresholds (B=0.70,
C=0.80, D=0.85). Module constants are process-global across B/C/D trader
threads in one container, so the threshold must be a function parameter
passed down generate_signal → _generate_ranging_signal — not an env constant.

Causal proof
------------
_generate_ranging_signal with fixed bands (lower=100, middle=110, upper=120):
- boll_threshold=0.85, price=116 (band_position 0.80) → HOLD (mid-band)
- boll_threshold=0.85, price=118 (band_position 0.90) → SELL
- boll_threshold=None (legacy), price=116 → SELL (old 0.70 threshold)
- generate_signal passes boll_threshold through to the ranging path.

This test FAILS (RED) before the parameter exists (TypeError), PASSES after.
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
from broky.signals.generator import _generate_ranging_signal, generate_signal  # noqa: E402


class _FixedBoll:
    """Stub Bollinger result: lower=100, middle=110, upper=120 (range 20)."""

    def __init__(self):
        self.upper = pd.Series([120.0])
        self.middle = pd.Series([110.0])
        self.lower = pd.Series([100.0])


def _close_series(n: int = 120):
    idx = pd.date_range("2026-09-01", periods=n, freq="5min", tz="UTC")
    return pd.Series([110.0 + (i % 5) * 0.2 for i in range(n)], index=idx)


_SCORES = {"macd": -0.5, "volume": 1.2}


class TestMRBollThresholdCausal:
    """Causal proof: boll_threshold parameter controls MR entry extremity."""

    def _call(self, price: float, boll_threshold):
        return _generate_ranging_signal(
            close=_close_series(),
            current_price=price,
            timestamp=datetime(2026, 9, 21, 2, 0, tzinfo=timezone.utc),
            timeframe="M5",
            scores=dict(_SCORES),
            regime="ranging",
            min_confidence=0.55,
            boll_threshold=boll_threshold,
        )

    def test_threshold_085_pos080_holds(self, monkeypatch):
        monkeypatch.setattr(
            gen_mod, "calculate_bollinger",
            lambda close, period=20, std_dev=2.0: _FixedBoll(),
        )
        sig = self._call(116.0, 0.85)  # band_position = 0.80 < 0.85
        assert sig.signal_type == SignalType.HOLD
        assert "mid-band" in sig.reason

    def test_threshold_085_pos090_sells(self, monkeypatch):
        monkeypatch.setattr(
            gen_mod, "calculate_bollinger",
            lambda close, period=20, std_dev=2.0: _FixedBoll(),
        )
        sig = self._call(118.0, 0.85)  # band_position = 0.90 >= 0.85
        assert sig.signal_type == SignalType.SELL

    def test_none_falls_back_to_legacy_070(self, monkeypatch):
        monkeypatch.setattr(
            gen_mod, "calculate_bollinger",
            lambda close, period=20, std_dev=2.0: _FixedBoll(),
        )
        sig = self._call(116.0, None)  # 0.80 >= legacy 0.70 → SELL
        assert sig.signal_type == SignalType.SELL

    def test_generate_signal_passes_threshold_through(self, monkeypatch):
        """generate_signal(boll_threshold=0.85) must reach the ranging path:
        same price that legacy 0.70 would SELL becomes HOLD at 0.85."""
        def fake_scores(close, high, low, volume):
            scores = {
                "ema_cross": 0.1, "ema_trend": 0.05, "adx": 0.6,
                "macd": -0.5, "bollinger": 0.4, "volume": 0.5,
            }
            return scores, 15.0  # ADX 15 → ranging path
        monkeypatch.setattr(gen_mod, "calculate_indicator_scores", fake_scores)
        monkeypatch.setattr(
            gen_mod, "calculate_bollinger",
            lambda close, period=20, std_dev=2.0: _FixedBoll(),
        )
        close = _close_series()
        idx = close.index
        high = close + 1.0
        low = close - 1.0
        volume = pd.Series([100.0] * len(close), index=idx)

        sig_strict = generate_signal(
            close, high, low, volume,
            current_price=116.0,
            timestamp=datetime(2026, 9, 21, 18, 30, tzinfo=timezone.utc),
            min_confidence=0.55,
            boll_threshold=0.85,
        )
        assert "mid-band" in sig_strict.reason, (
            f"boll_threshold=0.85 must reach the ranging path (pos 0.80 → mid-band HOLD), "
            f"reason={sig_strict.reason!r}"
        )

        sig_legacy = generate_signal(
            close, high, low, volume,
            current_price=116.0,
            timestamp=datetime(2026, 9, 21, 18, 30, tzinfo=timezone.utc),
            min_confidence=0.55,
        )
        assert sig_legacy.signal_type == SignalType.SELL