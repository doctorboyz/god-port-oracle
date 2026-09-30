"""Causal proof test: A-D-only knob dicts generalized to P-accounts.

Hypothesis
----------
LiveTrader.__init__ reads six risk knobs through hardcoded A/B/C/D dicts
(ATR_MULTIPLIER, PARTIAL_TP_ENABLED, TP1_RATIO, RR_SCALE_IN,
TRAILING_ACTIVATION_PCT, TRAILING_TRAIL_PCT — live_trader.py:211-280).
A P-account (paper farm variant) falls off the dict and its
KNOB_P{n} env is SILENTLY IGNORED — exactly the P1-ATR_MULTIPLIER hazard
that already bit the portfolio deploy (compose set it, trader never read it).
For a farm where each variant is configured purely via per-name env,
an ignored knob = a silently wrong strategy.

Causal proof
------------
Set KNOB_{P11} → construct LiveTrader(account="P11", dry_run=True) on a
seeded tmp DB → knob must land on risk.*. Env unset → risk_config value
stands (caller's default survives). A-D controls: dict semantics unchanged.

This test FAILS (RED) before the generalization, PASSES (GREEN) after.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from metty.core.db import init_db, insert_account  # noqa: E402
from metty.execution.live_trader import RiskConfig  # noqa: E402


@pytest.fixture()
def p11_db(tmp_path):
    """Seeded tmp DB so LiveTrader can resolve P11's account_id."""
    db = tmp_path / "farm_test.db"
    init_db(db)
    insert_account(
        name="P11", balance=100.0, leverage=2000,
        bridge_host="none", bridge_port=8001, signal_group="farm",
        db_path=db,
    )
    return db


_ALL_KNOB_ENV = [
    "ATR_MULTIPLIER", "PARTIAL_TP_ENABLED", "TP1_RATIO", "RR_SCALE_IN",
    "TRAILING_ACTIVATION_PCT", "TRAILING_TRAIL_PCT",
]


def _clear_knob_env(monkeypatch, account):
    for knob in _ALL_KNOB_ENV:
        monkeypatch.delenv(f"{knob}_{account}", raising=False)
        monkeypatch.delenv(knob, raising=False)


class TestKnobGeneralizationCausal:
    """P-account per-name env must land on risk.* — not be silently ignored."""

    def test_atr_multiplier_p11_env_lands(self, monkeypatch, p11_db):
        monkeypatch.setenv("ATR_MULTIPLIER_P11", "2.0")
        _clear_knob_env(monkeypatch, "B")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="P11", db_path=p11_db, dry_run=True,
                       risk_config=RiskConfig(risk_per_trade=0.01))
        assert t.risk.atr_multiplier == 2.0, (
            "ATR_MULTIPLIER_P11 was silently ignored — the A-D-only dict "
            "fell back to the default instead of the per-name env"
        )

    def test_partial_tp_p11_env_lands(self, monkeypatch, p11_db):
        monkeypatch.setenv("PARTIAL_TP_ENABLED_P11", "1")
        _clear_knob_env(monkeypatch, "B")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="P11", db_path=p11_db, dry_run=True,
                       risk_config=RiskConfig(risk_per_trade=0.01))
        assert t.risk.partial_tp_enabled is True

    def test_tp1_ratio_p11_env_lands(self, monkeypatch, p11_db):
        monkeypatch.setenv("TP1_RATIO_P11", "0.6")
        _clear_knob_env(monkeypatch, "B")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="P11", db_path=p11_db, dry_run=True,
                       risk_config=RiskConfig(risk_per_trade=0.01))
        assert t.risk.tp1_ratio == 0.6

    def test_rr_scale_in_p11_env_lands(self, monkeypatch, p11_db):
        monkeypatch.setenv("RR_SCALE_IN_P11", "1.8")
        _clear_knob_env(monkeypatch, "B")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="P11", db_path=p11_db, dry_run=True,
                       risk_config=RiskConfig(risk_per_trade=0.01))
        assert t.risk.rr_scale_in == 1.8

    def test_trailing_activation_p11_env_lands(self, monkeypatch, p11_db):
        monkeypatch.setenv("TRAILING_ACTIVATION_PCT_P11", "0.40")
        _clear_knob_env(monkeypatch, "B")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="P11", db_path=p11_db, dry_run=True,
                       risk_config=RiskConfig(risk_per_trade=0.01))
        assert t.risk.trailing_activation_pct == 0.40

    def test_trailing_trail_p11_env_lands(self, monkeypatch, p11_db):
        monkeypatch.setenv("TRAILING_TRAIL_PCT_P11", "0.20")
        _clear_knob_env(monkeypatch, "B")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="P11", db_path=p11_db, dry_run=True,
                       risk_config=RiskConfig(risk_per_trade=0.01))
        assert t.risk.trailing_trail_pct == 0.20

    def test_global_env_applies_to_p11_when_name_env_unset(self, monkeypatch, p11_db):
        """Global KNOB (no suffix) must reach P-accounts too — same chain as A-D."""
        monkeypatch.setenv("ATR_MULTIPLIER", "1.9")
        monkeypatch.delenv("ATR_MULTIPLIER_P11", raising=False)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="P11", db_path=p11_db, dry_run=True,
                       risk_config=RiskConfig(risk_per_trade=0.01))
        assert t.risk.atr_multiplier == 1.9

    def test_unset_env_keeps_risk_config_value(self, monkeypatch, p11_db):
        """Env unset → caller's risk_config stands (no silent default overwrite)."""
        _clear_knob_env(monkeypatch, "P11")
        _clear_knob_env(monkeypatch, "B")
        from metty.execution.live_trader import LiveTrader
        rc = RiskConfig(risk_per_trade=0.01, atr_multiplier=3.0, tp1_ratio=0.7)
        t = LiveTrader(account="P11", db_path=p11_db, dry_run=True, risk_config=rc)
        assert t.risk.atr_multiplier == 3.0
        assert t.risk.tp1_ratio == 0.7

    def test_account_b_dict_semantics_unchanged(self, monkeypatch):
        """Control: A-D keep the dict chain (env_B → env global → default)."""
        monkeypatch.setenv("ATR_MULTIPLIER_B", "1.2")
        monkeypatch.delenv("ATR_MULTIPLIER", raising=False)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t.risk.atr_multiplier == 1.2

    def test_account_b_default_when_all_unset(self, monkeypatch):
        """Control: B with everything unset → legacy default 2.5."""
        _clear_knob_env(monkeypatch, "B")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t.risk.atr_multiplier == 2.5