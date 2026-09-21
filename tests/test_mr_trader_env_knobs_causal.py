"""Causal proof test: per-account MR env knobs wired into LiveTrader.

Hypothesis
----------
MR-bet config is delivered entirely via per-account env on oracle-engine-train.
LiveTrader.__init__ must read (defaults preserve legacy behavior exactly):
- TIME_STOP_BARS_{acct} (default 288) → risk.time_stop_bars (MR-bet: 12 = 1h)
- TRAILING_TP_ENABLED_{acct} (default "1") → risk.trailing_tp_enabled (MR: 0)
- STRATEGY_ID_{acct} → self.strategy_id (MR: mr-bet-B — separates DB stats)
- CIRCUIT_BREAKER_COOLDOWN_MINUTES_{acct} (default 15) → CB cooldown (MR: 1440)
- SWING_MAX_SPREAD_{acct} (default 0 = off) → self._max_spread_points (MR: 30)
- MR_BOLL_THRESHOLD_{acct} (default 0.70) → self._mr_boll_threshold (per-variant)

Causal proof
------------
Set env → construct LiveTrader(account="B") → knob value on the instance.
Unset env → legacy default (Real-A and any other container unchanged).

This test FAILS (RED) before the knobs exist, PASSES (GREEN) after.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _clear_env(monkeypatch, *names):
    for n in names:
        monkeypatch.delenv(n, raising=False)


_ALL_ENV = [
    "TIME_STOP_BARS_B", "TIME_STOP_BARS", "TRAILING_TP_ENABLED_B",
    "TRAILING_TP_ENABLED", "STRATEGY_ID_B", "CIRCUIT_BREAKER_COOLDOWN_MINUTES_B",
    "CIRCUIT_BREAKER_COOLDOWN_MINUTES", "SWING_MAX_SPREAD_B", "SWING_MAX_SPREAD",
    "MR_BOLL_THRESHOLD_B", "MR_BOLL_THRESHOLD",
]


class TestMRTraderEnvKnobsCausal:
    """Causal proof: each env var lands on its knob with the legacy default."""

    def test_time_stop_bars_env(self, monkeypatch):
        monkeypatch.setenv("TIME_STOP_BARS_B", "12")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t.risk.time_stop_bars == 12

    def test_time_stop_bars_default_288(self, monkeypatch):
        _clear_env(monkeypatch, *_ALL_ENV)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t.risk.time_stop_bars == 288

    def test_trailing_tp_disabled(self, monkeypatch):
        monkeypatch.setenv("TRAILING_TP_ENABLED_B", "0")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t.risk.trailing_tp_enabled is False

    def test_trailing_tp_default_on(self, monkeypatch):
        _clear_env(monkeypatch, *_ALL_ENV)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t.risk.trailing_tp_enabled is True

    def test_strategy_id_env(self, monkeypatch):
        monkeypatch.setenv("STRATEGY_ID_B", "mr-bet-B")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t.strategy_id == "mr-bet-B"

    def test_strategy_id_default_swing(self, monkeypatch):
        _clear_env(monkeypatch, *_ALL_ENV)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t.strategy_id == "swing-B"

    def test_circuit_breaker_cooldown_env(self, monkeypatch):
        monkeypatch.setenv("CIRCUIT_BREAKER_COOLDOWN_MINUTES_B", "1440")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t.circuit_breaker._cooldown_minutes == 1440

    def test_circuit_breaker_cooldown_default_15(self, monkeypatch):
        _clear_env(monkeypatch, *_ALL_ENV)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t.circuit_breaker._cooldown_minutes == 15

    def test_swing_max_spread_env(self, monkeypatch):
        monkeypatch.setenv("SWING_MAX_SPREAD_B", "30")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t._max_spread_points == 30

    def test_swing_max_spread_default_off(self, monkeypatch):
        _clear_env(monkeypatch, *_ALL_ENV)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t._max_spread_points == 0

    def test_mr_boll_threshold_env(self, monkeypatch):
        monkeypatch.setenv("MR_BOLL_THRESHOLD_B", "0.80")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t._mr_boll_threshold == 0.80

    def test_mr_boll_threshold_default_070(self, monkeypatch):
        _clear_env(monkeypatch, *_ALL_ENV)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t._mr_boll_threshold == 0.70

    def test_mr_mode_follows_generator_flag(self, monkeypatch):
        """trader._mr_mode mirrors generator.TRENDING_HARD_BLOCK at init."""
        from broky.signals import generator as gen_mod
        monkeypatch.setattr(gen_mod, "TRENDING_HARD_BLOCK", True)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t._mr_mode is True

    def test_mr_mode_default_off(self, monkeypatch):
        from broky.signals import generator as gen_mod
        monkeypatch.setattr(gen_mod, "TRENDING_HARD_BLOCK", False)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t._mr_mode is False

    def test_boll_threshold_passed_to_generator(self, monkeypatch):
        """The trader must forward its per-account threshold to generate_signal."""
        monkeypatch.setenv("MR_BOLL_THRESHOLD_B", "0.80")
        from metty.execution import live_trader as lt_mod
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)

        captured = {}

        def fake_generate_signal(**kwargs):
            captured.update(kwargs)
            from shared.models import Signal, SignalType
            from datetime import datetime, timezone
            return Signal(
                signal_type=SignalType.HOLD, confidence=0.0, price=4000.0,
                timestamp=datetime.now(timezone.utc),
            )

        monkeypatch.setattr(lt_mod, "generate_signal", fake_generate_signal)

        import pandas as pd
        idx = pd.date_range("2026-09-01", periods=120, freq="5min", tz="UTC")
        m5 = pd.DataFrame({
            "open": 4000.0, "high": 4001.0, "low": 3999.0,
            "close": [4000.0 + (i % 5) * 0.2 for i in range(120)],
            "volume": 100.0,
        }, index=idx)
        candles = {"M5": m5, "H1": None, "H4": None, "D1": None}
        t._generate_signal(candles)
        assert captured.get("boll_threshold") == 0.80, (
            f"trader._mr_boll_threshold must reach generate_signal, "
            f"got {captured.get('boll_threshold')}"
        )