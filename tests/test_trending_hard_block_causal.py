"""Causal proof test: TRENDING_HARD_BLOCK — ADX>=20 must HOLD in MR-only mode.

Hypothesis
----------
MR-bet framework (B/C/D, 2026-09-21) trades mean reversion ONLY. The generator
needs a flag TRENDING_HARD_BLOCK: when True, any cycle with ADX >= 20 returns
HOLD immediately (reason 'trending_hard_block') — the MR-only mode must never
fall through to the trend-following path. When False (Real-A default), nothing
changes.

Causal proof
------------
Flag on + ADX 30 → HOLD with 'trending_hard_block'.
Flag on + ADX 22 (regime label 'ranging' by classify_regime, but ADX>=20) →
HOLD 'trending_hard_block' — the block must sit at the ADX switch, not at the
regime label (ADX 20-25 would otherwise leak into the MR path).
Flag on + ADX 15 → passes the block (proceeds to ranging MR path).
Flag off + ADX 30 → no 'trending_hard_block' reason (Real-A unchanged).

This test FAILS (RED) before the feature exists, PASSES (GREEN) after.
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


def _make_ohlcv(n: int = 120):
    idx = pd.date_range("2026-09-01", periods=n, freq="5min", tz="UTC")
    base = 4000.0
    close = pd.Series([base + (i % 7) * 0.5 for i in range(n)], index=idx)
    high = close + 1.0
    low = close - 1.0
    volume = pd.Series([100.0 + (i % 5) * 10 for i in range(n)], index=idx)
    return close, high, low, volume


def _force_adx(monkeypatch, adx: float):
    """Control latest_adx without touching the rest of the pipeline."""
    def fake_scores(close, high, low, volume):
        scores = {
            "ema_cross": 0.4, "ema_trend": 0.2, "adx": adx / 25.0,
            "macd": 0.3, "bollinger": 0.2, "volume": 0.5,
        }
        return scores, adx
    monkeypatch.setattr(gen_mod, "calculate_indicator_scores", fake_scores)


class TestTrendingHardBlockCausal:
    """Causal proof: TRENDING_HARD_BLOCK gates the ADX switch itself."""

    def test_flag_on_adx30_blocks(self, monkeypatch):
        monkeypatch.setattr(gen_mod, "TRENDING_HARD_BLOCK", True)
        close, high, low, volume = _make_ohlcv()
        _force_adx(monkeypatch, 30.0)
        signal = generate_signal(
            close, high, low, volume,
            current_price=4000.0,
            timestamp=datetime(2026, 9, 21, 18, 30, tzinfo=timezone.utc),
            min_confidence=0.55,
        )
        assert signal.signal_type == SignalType.HOLD
        assert "trending_hard_block" in signal.reason, (
            f"ADX=30 with TRENDING_HARD_BLOCK=1 must HOLD with trending_hard_block, "
            f"got {signal.signal_type.value} reason={signal.reason!r}"
        )

    def test_flag_on_adx22_blocks_before_ranging_path(self, monkeypatch):
        """ADX 20-25: classify_regime labels it 'ranging' but the MR-only mode
        must still block — the gate is the ADX value, not the regime label."""
        monkeypatch.setattr(gen_mod, "TRENDING_HARD_BLOCK", True)
        close, high, low, volume = _make_ohlcv()
        _force_adx(monkeypatch, 22.0)
        signal = generate_signal(
            close, high, low, volume,
            current_price=4000.0,
            timestamp=datetime(2026, 9, 21, 18, 30, tzinfo=timezone.utc),
            min_confidence=0.55,
        )
        assert signal.signal_type == SignalType.HOLD
        assert "trending_hard_block" in signal.reason

    def test_flag_on_adx15_passes_block(self, monkeypatch):
        """ADX<20: not blocked by trending_hard_block (MR path reachable)."""
        monkeypatch.setattr(gen_mod, "TRENDING_HARD_BLOCK", True)
        close, high, low, volume = _make_ohlcv()
        _force_adx(monkeypatch, 15.0)
        signal = generate_signal(
            close, high, low, volume,
            current_price=4000.0,
            timestamp=datetime(2026, 9, 21, 18, 30, tzinfo=timezone.utc),
            min_confidence=0.55,
        )
        assert "trending_hard_block" not in signal.reason, (
            f"ADX=15 must pass the trending_hard_block gate, reason={signal.reason!r}"
        )

    def test_flag_off_adx30_no_block(self, monkeypatch):
        """Real-A guard: flag off → legacy behavior, no trending_hard_block."""
        monkeypatch.setattr(gen_mod, "TRENDING_HARD_BLOCK", False)
        close, high, low, volume = _make_ohlcv()
        _force_adx(monkeypatch, 30.0)
        signal = generate_signal(
            close, high, low, volume,
            current_price=4000.0,
            timestamp=datetime(2026, 9, 21, 18, 30, tzinfo=timezone.utc),
            min_confidence=0.55,
        )
        assert "trending_hard_block" not in signal.reason

    def test_flag_on_learning_mode_bypasses(self, monkeypatch):
        """learning_mode must still collect trend-path outcomes (ML training)."""
        monkeypatch.setattr(gen_mod, "TRENDING_HARD_BLOCK", True)
        close, high, low, volume = _make_ohlcv()
        _force_adx(monkeypatch, 30.0)
        signal = generate_signal(
            close, high, low, volume,
            current_price=4000.0,
            timestamp=datetime(2026, 9, 21, 18, 30, tzinfo=timezone.utc),
            min_confidence=0.55,
            learning_mode=True,
        )
        assert "trending_hard_block" not in signal.reason