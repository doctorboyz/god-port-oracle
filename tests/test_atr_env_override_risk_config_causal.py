"""Causal proof test: ATR_MULTIPLIER_A env override skipped when risk_config passed.

Hypothesis
----------
Fix #3 (commit f5b623d) bumped ATR_MULTIPLIER_A 1.5 → 2.0 in .env and
docker-compose.vps.yml. Container env verified ATR_MULTIPLIER_A=2.0. But the
first new trade after deploy (2026-07-13 03:10 UTC, swing SELL @ 4056.28)
recorded atr_multiplier=2.5, not 2.0.

Bug: live_trader.py:206 wraps the env override in `if not risk_config:`.
oracle-engine constructs LiveTrader WITH a risk_config (from account_registry
default atr_multiplier=2.5), so the env override block is skipped — env intent
is silently ignored. Fix #1 (trailing TP) was added OUTSIDE the block so it
applied; Fix #3 was added INSIDE so it didn't. m5_scalp_trader.py:170 has the
same bug.

Causal proof
------------
Construct LiveTrader with an explicit risk_config (atr_multiplier=2.5) and
env ATR_MULTIPLIER_A=2.0. Before fix: trader.risk.atr_multiplier == 2.5 (env
ignored). After fix: == 2.0 (env wins). Same for M5ScalpTrader.

This test FAILS (RED) before the fix, PASSES (GREEN) after.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metty.execution.live_trader import RiskConfig  # noqa: E402
from metty.execution.m5_scalp_trader import M5ScalpRiskConfig  # noqa: E402


class TestATREnvOverrideRiskConfigCausal:
    """Causal proof: env override must win even when risk_config is passed."""

    def test_live_trader_env_wins_over_risk_config(self, monkeypatch):
        """RED before fix: env=2.0, risk_config=2.5 → trader uses 2.5 (env skipped).
        GREEN after fix: → trader uses 2.0 (env wins)."""
        monkeypatch.setenv("ATR_MULTIPLIER_A", "2.0")
        from metty.execution.live_trader import LiveTrader
        risk_config = RiskConfig(atr_multiplier=2.5)
        t = LiveTrader(account="A", dry_run=True, risk_config=risk_config)
        assert t.risk.atr_multiplier == pytest.approx(2.0, abs=1e-6), (
            f"ATR_MULTIPLIER_A=2.0 env must override risk_config=2.5, "
            f"got {t.risk.atr_multiplier} (env skipped — bug in if-not-risk_config block)"
        )

    def test_live_trader_no_env_keeps_risk_config(self, monkeypatch):
        """Sanity: without env, risk_config value is respected (no silent shrink)."""
        monkeypatch.delenv("ATR_MULTIPLIER_A", raising=False)
        monkeypatch.delenv("ATR_MULTIPLIER", raising=False)
        from metty.execution.live_trader import LiveTrader
        risk_config = RiskConfig(atr_multiplier=2.5)
        t = LiveTrader(account="A", dry_run=True, risk_config=risk_config)
        assert t.risk.atr_multiplier == pytest.approx(2.5, abs=1e-6)

    def test_m5_scalp_trader_env_wins_over_risk_config(self, monkeypatch):
        """Same bug in m5_scalp_trader.py:170 — env must win."""
        monkeypatch.setenv("ATR_MULTIPLIER_A", "2.0")
        from metty.execution.m5_scalp_trader import M5ScalpTrader
        risk_config = M5ScalpRiskConfig(atr_multiplier=2.5)
        t = M5ScalpTrader(account="A", dry_run=True, risk_config=risk_config)
        assert t.risk.atr_multiplier == pytest.approx(2.0, abs=1e-6), (
            f"M5ScalpTrader ATR_MULTIPLIER_A=2.0 must override risk_config=2.5, "
            f"got {t.risk.atr_multiplier}"
        )

    def test_live_trader_default_when_no_env_no_config(self, monkeypatch):
        """No env, no risk_config → registry default 2.5 (sanity, no regression)."""
        monkeypatch.delenv("ATR_MULTIPLIER_A", raising=False)
        monkeypatch.delenv("ATR_MULTIPLIER", raising=False)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="A", dry_run=True)
        # Default in per_account_atr fallback is "2.5" (registry default).
        assert t.risk.atr_multiplier == pytest.approx(2.5, abs=1e-6)