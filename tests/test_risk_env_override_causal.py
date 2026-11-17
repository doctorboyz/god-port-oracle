"""Causal proof test: RISK_PER_TRADE_{account} env must win over risk_config.

Hypothesis
----------
LiveTrader.__init__ resolves the per-account risk (RISK_PER_TRADE_{account}
env → account registry → ACCOUNT_RISK) but applies it ONLY through
`self.risk = risk_config or RiskConfig(risk_per_trade=_risk_per_trade)`
(metty/execution/live_trader.py ~line 215). oracle_runner.py — the runner
behind every engine container — ALWAYS passes
risk_config=RiskConfig(risk_per_trade=float(os.environ.get("RISK_PER_TRADE", "0.02")))
built from the GENERIC env key with a 2% default, so the `or` short-circuits
and the per-account value is silently discarded → runtime risk = 2%.
Same bug class as RR_RATIO_* (2026-09-23) and ATR_MULTIPLIER_* (2026-07-13):
both of those got env-wins treatment; risk_per_trade itself was missed.

Observed (2026-11-17, P22 cent stage 1 DRY_RUN on the VPS, commit d2464ac):
- P2 (equity 2000 USC) mock SELL 3862.69 SL 3871.35 shipped lots 0.04 —
  exactly 2000×0.02/866 = 0.046 → 0.04. The intended 0.5% gives
  2000×0.005/866 = 0.0116 → 0.01 (RISK_PER_TRADE_P2=0.005 in compose).
- P3 (equity 1000 USC) shipped 0.02 — exactly the 2% math; 0.5% gives 0.01.
- Container probe: _calculate_lots(2000, …, sl 8.66, risk 0.005) → 0.01 —
  the formula is right; the runtime object held risk=2%.

Why nobody saw it before (the blind spot, recorded in the retro):
- farm ($100 equity) and demo (~$200) always sat under the 0.01 min-lot
  floor, so 2% and 0.5% produced identical lots 0.01 — two months of farm
  stats ran 2% UNDER THE FLOOR with zero effect on any trade's lots.
- cent P2/P3 (USC 2000/1000) are the first accounts whose 0.5% risk budget
  clears the floor, exposing the mismatch.
- verify_deploy.py passed because it constructed LiveTrader WITHOUT
  risk_config (the env branch fires there) — not the way oracle_runner
  builds the object at runtime. Fixed in verify_deploy.py the same day.

Fix semantics (mirrors RR_RATIO_* / ATR_MULTIPLIER_* env-wins):
- RISK_PER_TRADE_{account} set → overrides risk.risk_per_trade, ALWAYS
- env unset → the passed risk_config value stands (legacy callers unchanged)

Causal proof
------------
RED (pre-fix):  risk_config passed + RISK_PER_TRADE_B=0.005 → risk 0.02.
GREEN (post-fix): same construction → risk 0.005.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

VPS_COMPOSE = ROOT / "docker-compose.vps.yml"
FARM_COMPOSE = ROOT / "docker-compose.paper-farm.yml"

_ALL_ACCOUNTS = ["B", "C", "D", "P1", "P2", "P3"] + [f"P{i}" for i in range(11, 25)]


def _clear_env(monkeypatch):
    for n in _ALL_ACCOUNTS:
        monkeypatch.delenv(f"RISK_PER_TRADE_{n}", raising=False)
    monkeypatch.delenv("RISK_PER_TRADE", raising=False)


def _runner_style_risk_config():
    """The EXACT construction oracle_runner.py:253 performs every loop start
    (generic env key, 2% default). Tests must use this, not a bare RiskConfig,
    or they reproduce the verify_deploy blind spot that let the bug ship."""
    import os

    from metty.execution.live_trader import RiskConfig
    return RiskConfig(risk_per_trade=float(os.environ.get("RISK_PER_TRADE", "0.02")))


@pytest.fixture()
def p_db(tmp_path):
    """Accounts table seeded with every P name the composes carry — P-accounts
    resolve account_id from this table during LiveTrader.__init__."""
    from metty.core.db import init_db, insert_account
    db = tmp_path / "oracle_p.db"
    init_db(db)
    for acct in [f"P{i}" for i in range(1, 4)] + [f"P{i}" for i in range(11, 25)] \
            + [f"P{i}" for i in range(31, 36)]:
        insert_account(
            name=acct, balance=100.0, leverage=500,
            bridge_host=f"mt5{acct.lower()}", bridge_port=8001,
            signal_group="volume", db_path=db,
        )
    return db


class TestRiskEnvOverrideCausal:
    """Causal proof: per-account risk env must reach risk even when
    oracle_runner passes a risk_config built from the generic key."""

    def test_env_wins_over_risk_config(self, monkeypatch):
        """THE bug: runner passes risk_config (2% default) → per-account env
        must still apply. RED pre-fix: risk stays 0.02."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("RISK_PER_TRADE_B", "0.005")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(
            account="B", dry_run=True,
            risk_config=_runner_style_risk_config(),
        )
        assert t.risk.risk_per_trade == 0.005, (
            "RISK_PER_TRADE_B env must win over the passed risk_config — "
            f"got {t.risk.risk_per_trade} (the 2026-11-17 sizing bug)"
        )

    def test_env_wins_without_risk_config(self, monkeypatch):
        """No risk_config + env set → env applies (legacy path, env-first)."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("RISK_PER_TRADE_C", "0.005")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="C", dry_run=True)
        assert t.risk.risk_per_trade == 0.005

    def test_env_unset_keeps_risk_config_value(self, monkeypatch):
        """env unset + risk_config passed → risk_config value stands
        (legacy callers like sweep/one-shot scripts unchanged)."""
        _clear_env(monkeypatch)
        from metty.execution.live_trader import LiveTrader, RiskConfig
        t = LiveTrader(
            account="B", dry_run=True,
            risk_config=RiskConfig(risk_per_trade=0.005),
        )
        assert t.risk.risk_per_trade == 0.005

    def test_env_unset_no_risk_config_keeps_default(self, monkeypatch):
        """env unset + no risk_config → the resolved chain stands (registry
        default for demo accounts = 2%). Farm/demo containers have no generic
        RISK_PER_TRADE either, so this is their real pre-fix behavior."""
        _clear_env(monkeypatch)
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(account="B", dry_run=True)
        assert t.risk.risk_per_trade == 0.02

    @pytest.mark.parametrize("acct", ["P1", "P2", "P3"])
    def test_cent_production_scenario_sizing(self, monkeypatch, p_db, acct):
        """The exact 2026-11-17 VPS scenario, through the REAL method the live
        loop calls: runner-style risk_config (2%) + compose per-account env
        (0.005) → _calculate_lots at the production numbers must give the
        0.5% lots, not the 2% lots that shipped."""
        _clear_env(monkeypatch)
        monkeypatch.setenv(f"RISK_PER_TRADE_{acct}", "0.005")
        from metty.execution.live_trader import LiveTrader
        t = LiveTrader(
            account=acct, db_path=p_db, dry_run=True,
            risk_config=_runner_style_risk_config(),
        )
        # production numbers: P2 mock SELL 3862.69 SL 3871.35 (distance 8.66)
        lots = t._calculate_lots(2000.0, 3862.69, 3871.35, 2.0)
        assert lots == 0.01, (
            f"{acct}: 2000 USC @ SL 8.66 with risk 0.005 must floor to 0.01, "
            f"got {lots} — the runtime object is still holding the 2% default"
        )

    def test_bug_math_documented(self, monkeypatch):
        """Documents the causal direction of the observed bug: the same
        _calculate_lots with risk=2% reproduces the exact wrong lots the VPS
        shipped (P2 0.04 / P3 0.02). If this ever fails, the production
        evidence no longer matches the sizing math — STOP and re-read."""
        _clear_env(monkeypatch)
        from metty.execution.live_trader import LiveTrader, RiskConfig
        t = LiveTrader(
            account="B", dry_run=True,
            risk_config=RiskConfig(risk_per_trade=0.02),
        )
        assert t._calculate_lots(2000.0, 3862.69, 3871.35, 2.0) == 0.04
        assert t._calculate_lots(1000.0, 3862.69, 3871.35, 2.0) == 0.02

    @pytest.mark.parametrize("risk", ["0.005", "0.02"])
    def test_farm_equity_under_floor_unchanged(self, monkeypatch, risk):
        """FARM-SAFE proof: at $100 equity and $6 SL, risk 0.5% and 2% BOTH
        floor to the 0.01 broker minimum — the fix changes nothing for the
        farm/demo accounts (their trades sat under the floor all along,
        which is exactly why the bug stayed invisible for two months)."""
        _clear_env(monkeypatch)
        from metty.execution.live_trader import LiveTrader, RiskConfig
        t = LiveTrader(
            account="B", dry_run=True,
            risk_config=RiskConfig(risk_per_trade=float(risk)),
        )
        lots = t._calculate_lots(100.0, 2700.0, 2694.0, 2.0)
        assert lots == 0.01, (
            f"farm $100 @ SL $6 must be the 0.01 min-lot at risk {risk} — "
            "if this moves, the fix changed farm behavior (it must not)"
        )


# ═══ Compose intent: every account's compose risk reaches the object ════


def _env_value(raw: str):
    """compose value → the in-container default (`${VAR:-d}` → d)."""
    m = re.match(r"^\$\{[^:}]+(?::-([^}]*))?\}$", raw)
    if m:
        return m.group(1)  # None when `${VAR}` has no default
    return raw


def _compose_risk_env(compose_path: Path) -> dict:
    """Every RISK_PER_TRADE_{acct} a compose file sets, service-agnostic."""
    doc = yaml.safe_load(compose_path.read_text())
    found: dict[str, str] = {}
    for svc in doc.get("services", {}).values():
        for e in svc.get("environment") or []:
            if "=" not in e:
                continue
            key, raw = e.split("=", 1)
            m = re.match(r"^RISK_PER_TRADE_([A-Z0-9]+)$", key)
            if m:
                found[m.group(1)] = _env_value(raw)
    return found


class TestComposeRiskIntent:
    """farm P11–P24 (+P31–P35) + demo B/C/D + cent P1–P3: the per-account
    risk each compose pins must reach the REAL object even under
    runner-style construction (risk_config from the generic 2% default).
    The farm believed it ran 0.5% from day one — this locks that belief
    into the runtime path that was silently broken."""

    @pytest.fixture(scope="class")
    def compose_risk(self):
        merged = {**_compose_risk_env(VPS_COMPOSE), **_compose_risk_env(FARM_COMPOSE)}
        return merged

    def test_expected_accounts_covered(self, compose_risk):
        expected = (
            {"B", "C", "D", "P1", "P2", "P3"}
            | {f"P{i}" for i in range(11, 25)}
            | {f"P{i}" for i in range(31, 36)}
        )
        assert set(compose_risk) == expected, (
            f"compose per-account risk set changed: {set(compose_risk) ^ expected}"
        )

    def test_all_intended_risk_is_half_percent(self, compose_risk):
        """Current intent per the order: every compose account pins 0.005.
        (Demo D's host-.env 0.01 override on the VPS is a pending decision —
        verify_deploy NOTE-2 — and lives outside the compose file.)"""
        for acct, value in compose_risk.items():
            assert value == "0.005", (
                f"{acct}: compose pins RISK_PER_TRADE_{acct}={value!r} — "
                "expected 0.005; update this intent lock deliberately"
            )

    @pytest.mark.parametrize("acct,db_needed", [
        *(("B", False), ("C", False), ("D", False)),
        *(("P1", True), ("P2", True), ("P3", True)),
        *((f"P{i}", True) for i in range(11, 25)),
        *((f"P{i}", True) for i in range(31, 36)),
    ])
    def test_compose_risk_lands_on_object(self, monkeypatch, p_db, acct,
                                          db_needed):
        _clear_env(monkeypatch)
        from metty.execution.live_trader import LiveTrader
        monkeypatch.setenv(f"RISK_PER_TRADE_{acct}", "0.005")
        t = LiveTrader(
            account=acct, db_path=(p_db if db_needed else None), dry_run=True,
            risk_config=_runner_style_risk_config(),
        )
        assert t.risk.risk_per_trade == 0.005, (
            f"{acct}: compose risk 0.005 did not reach the object under "
            f"runner-style construction (got {t.risk.risk_per_trade}) — "
            "the engine would size at the wrong risk again"
        )
