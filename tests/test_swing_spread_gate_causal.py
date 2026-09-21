"""Causal proof test: swing-path max-spread gate.

Hypothesis
----------
The swing/live_trader entry path never had a spread gate (only m5_scalp did).
MR-bet entries need one: spread wider than SWING_MAX_SPREAD_{acct} points →
hold + rejection 'spread_too_wide'. Default 0 = off (legacy behavior).

Causal proof
------------
LiveTrader._spread_gate_ok(spread_points):
- knob 30, spread 45 → (False, 'spread_too_wide:...')
- knob 30, spread 25 → (True, '')
- knob 0 (default), spread 200 → (True, '') — gate off

This test FAILS (RED) before the gate exists, PASSES (GREEN) after.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TestSwingSpreadGateCausal:
    """Causal proof: swing path must gate on live spread points."""

    def _trader(self, monkeypatch, max_spread: int):
        monkeypatch.setenv("SWING_MAX_SPREAD_B", str(max_spread))
        from metty.execution.live_trader import LiveTrader
        return LiveTrader(account="B", dry_run=True)

    def test_spread_over_gate_holds(self, monkeypatch):
        t = self._trader(monkeypatch, 30)
        ok, reason = t._spread_gate_ok(45)
        assert ok is False
        assert "spread_too_wide" in reason
        assert "45" in reason and "30" in reason

    def test_spread_under_gate_passes(self, monkeypatch):
        t = self._trader(monkeypatch, 30)
        ok, reason = t._spread_gate_ok(25)
        assert ok is True
        assert reason == ""

    def test_default_off_never_blocks(self, monkeypatch):
        """Default 0 = gate off — legacy swing path unchanged."""
        t = self._trader(monkeypatch, 0)
        ok, _ = t._spread_gate_ok(200)
        assert ok is True

    def test_exact_limit_passes(self, monkeypatch):
        """Boundary: spread == limit passes (strictly greater blocks)."""
        t = self._trader(monkeypatch, 30)
        ok, _ = t._spread_gate_ok(30)
        assert ok is True