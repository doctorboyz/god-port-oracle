"""Causal proof: RR_RATIO_* / MIN_CONFIDENCE_* / M5_MAX_SPREAD_* env must win
over the passed risk_config in M5ScalpTrader (retro งาน 4 follow-up,
คุณหมอผ่าน Hermes 17/11).

Hypothesis
----------
M5ScalpTrader.__init__ applies the per-account RR/conf/spread env only
inside `if not risk_config:` (metty/execution/m5_scalp_trader.py ~line 170).
But the runner (scripts/oracle_runner.py run_m5_scalp_trader) ALWAYS passes
risk_config=M5ScalpRiskConfig(risk_per_trade=..., max_spread_points=...), so
the branch never fires on the live path and RR_RATIO_{acct} /
MIN_CONFIDENCE_{acct} / M5_MAX_SPREAD_{acct} env is silently skipped — the
same bug class as live_trader RR/conf (2026-09-23) and live_trader
risk_per_trade (2026-11-17, commit f0a640d).

Concrete live impact (docker-compose.vps.yml oracle-engine, the ONLY service
with M5_SCALP_ENABLED=1 — Real-A): compose deliberately sets
RR_RATIO_A=2.5 / MIN_CONFIDENCE_A=0.45 / M5_MAX_SPREAD_A=45, but the live
m5-scalp has been trading on M5ScalpRiskConfig defaults (2.0 / 0.50) plus the
runner's M5_SCALP_SPREAD_MAX=30 instead — user intent silently ignored.

Fix semantics (mirrors live_trader env-wins):
- RR_RATIO_{acct} (fallback RR_RATIO) set            → override risk.risk_reward_ratio
- MIN_CONFIDENCE_{acct} (fallback MIN_CONFIDENCE) set → override risk.min_confidence
- M5_MAX_SPREAD_{acct} (fallback M5_MAX_SPREAD) set   → override risk.max_spread_points
- env unset → risk_config value stands (legacy callers unchanged)

Causal proof
------------
RED (pre-fix):  risk_config passed + RR_RATIO_B=0.8 / MIN_CONFIDENCE_B=0.55 /
                M5_MAX_SPREAD_B=25 → risk stays 2.0/0.50/30 → assertions FAIL.
GREEN (post-fix): same construction → risk is 0.8/0.55/25.

Real-A note: after the fix Real-A's live m5-scalp honors its compose values
(RR 2.5, conf 0.45, spread 45) — previously the defaults above applied.
This behavioral delta is deliberate (env = explicit user intent) and is
recorded in the handoff.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_KNOBS = [
    "RR_RATIO", "MIN_CONFIDENCE", "M5_MAX_SPREAD",
] + [
    f"{base}_{acct}" for base in ("RR_RATIO", "MIN_CONFIDENCE", "M5_MAX_SPREAD")
    for acct in ("A", "B", "C", "D")
]


def _clear_env(monkeypatch):
    for n in _KNOBS:
        monkeypatch.delenv(n, raising=False)


class TestM5ScalpEnvWinsCausal:
    """Causal proof: env RR/conf/spread must reach risk even when the runner
    passes a risk_config (which it always does on the live path)."""

    def test_env_wins_over_passed_risk_config(self, monkeypatch):
        """The bug: runner passes risk_config → RR_RATIO_B etc. must still apply."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("RR_RATIO_B", "0.8")
        monkeypatch.setenv("MIN_CONFIDENCE_B", "0.55")
        monkeypatch.setenv("M5_MAX_SPREAD_B", "25")
        from metty.execution.m5_scalp_trader import M5ScalpRiskConfig, M5ScalpTrader
        t = M5ScalpTrader(
            account="B", dry_run=True,
            risk_config=M5ScalpRiskConfig(),
        )
        assert t.risk.risk_reward_ratio == 0.8, (
            "RR_RATIO_B env must win over the passed risk_config "
            f"(got {t.risk.risk_reward_ratio})"
        )
        assert t.risk.min_confidence == 0.55, (
            "MIN_CONFIDENCE_B env must win over the passed risk_config "
            f"(got {t.risk.min_confidence})"
        )
        assert t.risk.max_spread_points == 25, (
            "M5_MAX_SPREAD_B env must win over the passed risk_config "
            f"(got {t.risk.max_spread_points})"
        )

    def test_env_wins_without_risk_config(self, monkeypatch):
        """No risk_config + env set → env applies (legacy path, still env-first)."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("RR_RATIO_C", "1.0")
        monkeypatch.setenv("MIN_CONFIDENCE_C", "0.55")
        monkeypatch.setenv("M5_MAX_SPREAD_C", "20")
        from metty.execution.m5_scalp_trader import M5ScalpTrader
        t = M5ScalpTrader(account="C", dry_run=True)
        assert t.risk.risk_reward_ratio == 1.0
        assert t.risk.min_confidence == 0.55
        assert t.risk.max_spread_points == 20

    def test_env_unset_keeps_risk_config_values(self, monkeypatch):
        """env unset + risk_config passed → risk_config values stand (legacy)."""
        _clear_env(monkeypatch)
        from metty.execution.m5_scalp_trader import M5ScalpRiskConfig, M5ScalpTrader
        risk = M5ScalpRiskConfig(
            risk_reward_ratio=3.0, min_confidence=0.65, max_spread_points=18,
        )
        t = M5ScalpTrader(account="D", dry_run=True, risk_config=risk)
        assert t.risk.risk_reward_ratio == 3.0
        assert t.risk.min_confidence == 0.65
        assert t.risk.max_spread_points == 18

    def test_global_env_fallback_applies(self, monkeypatch):
        """Per-account key unset but the GLOBAL key set → global env applies
        (the runner's risk_config must not shadow explicit global intent)."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("RR_RATIO", "1.20")
        monkeypatch.setenv("MIN_CONFIDENCE", "0.55")
        monkeypatch.setenv("M5_MAX_SPREAD", "28")
        from metty.execution.m5_scalp_trader import M5ScalpRiskConfig, M5ScalpTrader
        t = M5ScalpTrader(account="B", dry_run=True,
                          risk_config=M5ScalpRiskConfig())
        assert t.risk.risk_reward_ratio == 1.20
        assert t.risk.min_confidence == 0.55
        assert t.risk.max_spread_points == 28

    def test_real_a_compose_intent_reaches_risk(self, monkeypatch):
        """The production delta lock: Real-A's compose values (RR 2.5 /
        conf 0.45 / spread 45) must reach the live m5-scalp risk object —
        the exact intent that was silently ignored before the fix."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("RR_RATIO_A", "2.5")
        monkeypatch.setenv("MIN_CONFIDENCE_A", "0.45")
        monkeypatch.setenv("M5_MAX_SPREAD_A", "45")
        from metty.execution.m5_scalp_trader import M5ScalpRiskConfig, M5ScalpTrader
        # the runner's exact construction (oracle_runner.run_m5_scalp_trader):
        # per-account env is unset at the runner level, M5_SCALP_SPREAD_MAX
        # default 30 lands in risk_config — the per-account env must win.
        monkeypatch.delenv("M5_SCALP_SPREAD_MAX", raising=False)
        monkeypatch.delenv("M5_SCALP_RISK_PER_TRADE", raising=False)
        risk = M5ScalpRiskConfig(
            risk_per_trade=0.015, max_spread_points=30,
        )
        t = M5ScalpTrader(account="A", dry_run=True, risk_config=risk)
        assert t.risk.risk_reward_ratio == 2.5
        assert t.risk.min_confidence == 0.45
        assert t.risk.max_spread_points == 45
