"""Causal proof test: RR_RATIO_* / MIN_CONFIDENCE_* env must win over risk_config.

Hypothesis
----------
LiveTrader.__init__ applies per-account RR/conf only inside `if not risk_config:`
(metty/execution/live_trader.py ~line 228). But oracle_runner.py — the runner for
both oracle-engine AND oracle-engine-train — ALWAYS passes
risk_config=RiskConfig(risk_per_trade=...), so the branch never fires and
RR_RATIO_{acct} / MIN_CONFIDENCE_{acct} env is silently skipped.

Observed (2026-09-21 mr-bet deploy, post-mortem 2026-09-23): the 2 mr-bet-B
trades shipped with rr_ratio=2.5 (RiskConfig default) instead of the
RR_RATIO_B=0.8 variant value — and MIN_CONFIDENCE_B=0.55 was not enforced
either (RiskConfig default 0.45 applied).

Fix semantics (mirrors the ATR env-wins fix of 2026-07-13):
- RR_RATIO_{acct} (fallback RR_RATIO) set  → override risk.risk_reward_ratio
- MIN_CONFIDENCE_{acct} (fallback MIN_CONFIDENCE) set → override risk.min_confidence
- env unset → risk_config value stands (legacy callers unchanged)

Causal proof
------------
RED (pre-fix):  risk_config passed + RR_RATIO_B=0.8 / MIN_CONFIDENCE_B=0.55
                → risk stays 2.5/0.45 → assertions FAIL.
GREEN (post-fix): same construction → risk is 0.8/0.55.

Real-A guard: oracle-engine sets RR_RATIO_A=2.5 / MIN_CONFIDENCE_A=0.45 in
compose — identical to RiskConfig defaults, so env-wins changes nothing for A.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _clear_env(monkeypatch):
    for n in [
        "RR_RATIO_B", "RR_RATIO_C", "RR_RATIO_D", "RR_RATIO_A", "RR_RATIO",
        "MIN_CONFIDENCE_B", "MIN_CONFIDENCE_C", "MIN_CONFIDENCE_D",
        "MIN_CONFIDENCE_A", "MIN_CONFIDENCE",
    ]:
        monkeypatch.delenv(n, raising=False)


class TestRRConfEnvOverrideCausal:
    """Causal proof: env RR/conf must reach risk even when risk_config is passed."""

    def test_rr_env_wins_over_risk_config(self, monkeypatch):
        """The bug: oracle_runner passes risk_config → RR_RATIO_B must still apply."""
        monkeypatch.setenv("RR_RATIO_B", "0.8")
        monkeypatch.setenv("MIN_CONFIDENCE_B", "0.55")
        from metty.execution.live_trader import LiveTrader, RiskConfig
        t = LiveTrader(
            account="B", dry_run=True,
            risk_config=RiskConfig(risk_per_trade=0.005),
        )
        assert t.risk.risk_reward_ratio == 0.8, (
            "RR_RATIO_B env must win over the passed risk_config "
            f"(got {t.risk.risk_reward_ratio})"
        )
        assert t.risk.min_confidence == 0.55, (
            "MIN_CONFIDENCE_B env must win over the passed risk_config "
            f"(got {t.risk.min_confidence})"
        )

    def test_rr_env_wins_without_risk_config(self, monkeypatch):
        """No risk_config + env set → env applies (legacy path, still env-first)."""
        monkeypatch.setenv("RR_RATIO_C", "1.0")
        monkeypatch.setenv("MIN_CONFIDENCE_C", "0.55")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="C", dry_run=True)
        assert t.risk.risk_reward_ratio == 1.0
        assert t.risk.min_confidence == 0.55

    def test_env_unset_keeps_risk_config_values(self, monkeypatch):
        """env unset + risk_config passed → risk_config values stand (legacy)."""
        _clear_env(monkeypatch)
        from metty.execution.live_trader import LiveTrader, RiskConfig
        rc = RiskConfig(risk_per_trade=0.005)
        rc.risk_reward_ratio = 3.0
        rc.min_confidence = 0.50
        t = LiveTrader(account="B", dry_run=True, risk_config=rc)
        assert t.risk.risk_reward_ratio == 3.0
        assert t.risk.min_confidence == 0.50

    def test_env_unset_no_risk_config_keeps_defaults(self, monkeypatch):
        """env unset + no risk_config → RiskConfig defaults (2.5/0.45)."""
        _clear_env(monkeypatch)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t.risk.risk_reward_ratio == 2.5
        assert t.risk.min_confidence == 0.45

    def test_unsuffixed_fallback_env(self, monkeypatch):
        """Plain RR_RATIO / MIN_CONFIDENCE (no suffix) also apply."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("RR_RATIO", "1.2")
        monkeypatch.setenv("MIN_CONFIDENCE", "0.60")
        from metty.execution.live_trader import LiveTrader, RiskConfig
        t = LiveTrader(
            account="D", dry_run=True,
            risk_config=RiskConfig(risk_per_trade=0.005),
        )
        assert t.risk.risk_reward_ratio == 1.2
        assert t.risk.min_confidence == 0.60

    def test_suffixed_beats_unsuffixed(self, monkeypatch):
        """RR_RATIO_D wins over plain RR_RATIO when both set."""
        monkeypatch.setenv("RR_RATIO", "2.0")
        monkeypatch.setenv("RR_RATIO_D", "1.2")
        from metty.execution.live_trader import LiveTrader, RiskConfig
        t = LiveTrader(
            account="D", dry_run=True,
            risk_config=RiskConfig(risk_per_trade=0.005),
        )
        assert t.risk.risk_reward_ratio == 1.2