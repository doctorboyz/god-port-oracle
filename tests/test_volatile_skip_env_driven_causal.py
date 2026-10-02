"""Causal proof test: REGIME_VOLATILE_SKIP env is read by the generator (ISSUE-100).

Hypothesis
----------
Bug: broky/signals/generator.py hardcodes `REGIME_VOLATILE_SKIP = False`
while docker-compose.vps.yml (oracle-engine-train) sets `REGIME_VOLATILE_SKIP=1`
with a comment claiming the skip is on. The env var is a silent no-op — the
compose "lies" and volatile-regime trades never get skipped on the live VPS.

Causal proof
------------
Import the generator in a FRESH subprocess (module constants are read once at
import; reload-based testing is forbidden per tests/test_regime_consistency.py
because it re-triggers StrategyRegistry) with REGIME_VOLATILE_SKIP=1 in env.
Before fix: flag imports as False (hardcoded). After fix: flag imports as True.

Plus a learning_mode control: the volatile-skip branch must mirror the
RANGING_HARD_BLOCK sibling and bypass when learning_mode=True, so ML outcome
collection keeps flowing on train containers.

RED before fix, GREEN after. Controls pass both sides.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.models import MarketRegime, SignalType  # noqa: E402
from broky.signals import generator as gen_mod  # noqa: E402
from broky.signals.generator import generate_signal  # noqa: E402

_PROBE = (
    "import sys; from broky.signals.generator import REGIME_VOLATILE_SKIP; "
    "print('ON' if REGIME_VOLATILE_SKIP else 'OFF')"
)


def _probe_flag(env_extra: dict | None) -> str:
    """Import the generator in a fresh subprocess with the given env and read the flag."""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    if env_extra:
        env.update(env_extra)
    else:
        env.pop("REGIME_VOLATILE_SKIP", None)
    out = subprocess.run(
        [sys.executable, "-c", _PROBE],
        env=env, capture_output=True, text=True, timeout=120,
        cwd=str(ROOT),
    )
    assert out.returncode == 0, f"probe import failed: {out.stderr}"
    return out.stdout.strip()


def _make_ohlcv(n: int = 120):
    idx = pd.date_range("2026-09-01", periods=n, freq="5min", tz="UTC")
    base = 4000.0
    close = pd.Series([base + (i % 7) * 0.5 for i in range(n)], index=idx)
    high = close + 1.0
    low = close - 1.0
    volume = pd.Series([100.0 + (i % 5) * 10 for i in range(n)], index=idx)
    return close, high, low, volume


class TestVolatileSkipEnvDrivenCausal:
    """Causal proof: the env var must actually reach the module constant."""

    def test_env_set_activates_flag(self):
        """RED before fix: hardcoded False ignores env. GREEN after: env wins."""
        assert _probe_flag({"REGIME_VOLATILE_SKIP": "1"}) == "ON"

    def test_env_zero_flag_off(self):
        """Control: explicit 0 keeps the flag off (passes both sides)."""
        assert _probe_flag({"REGIME_VOLATILE_SKIP": "0"}) == "OFF"

    def test_env_unset_flag_off(self):
        """Control: unset env preserves legacy off behavior (passes both sides)."""
        assert _probe_flag(None) == "OFF"

    def test_learning_mode_bypasses_volatile_skip(self, monkeypatch):
        """RED before fix: skip fires regardless. GREEN: mirrors RANGING sibling."""
        close, high, low, volume = _make_ohlcv()
        monkeypatch.setattr(gen_mod, "REGIME_VOLATILE_SKIP", True)
        monkeypatch.setattr(
            gen_mod, "classify_regime",
            lambda adx, bw=None: MarketRegime.VOLATILE.value,
        )
        signal = generate_signal(
            close, high, low, volume,
            current_price=4000.0,
            timestamp=datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc),
            min_confidence=0.55,
            learning_mode=True,
        )
        assert "volatile regime skipped" not in signal.reason

    def test_learning_mode_false_still_skips(self, monkeypatch):
        """Control: production path (learning_mode=False) still skips volatile."""
        close, high, low, volume = _make_ohlcv()
        monkeypatch.setattr(gen_mod, "REGIME_VOLATILE_SKIP", True)
        monkeypatch.setattr(
            gen_mod, "classify_regime",
            lambda adx, bw=None: MarketRegime.VOLATILE.value,
        )
        signal = generate_signal(
            close, high, low, volume,
            current_price=4000.0,
            timestamp=datetime(2026, 10, 2, 2, 0, tzinfo=timezone.utc),
            min_confidence=0.55,
            learning_mode=False,
        )
        assert signal.signal_type == SignalType.HOLD
        assert "volatile regime skipped" in signal.reason