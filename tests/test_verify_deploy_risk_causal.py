"""Causal proof test: verify_deploy must inspect the RUNNER-shaped object.

The 2026-11-17 sizing bug (risk 2% instead of 0.5% on the P22 cent engines)
shipped WITH verify_deploy green — because verify_account constructed
LiveTrader WITHOUT a risk_config, while oracle_runner ALWAYS passes
RiskConfig(risk_per_trade=os.environ.get("RISK_PER_TRADE", "0.02")) built
from the GENERIC key. Pre-live_trader-fix, only the bare construction honored
RISK_PER_TRADE_{account}, so the script verified an object the runtime never
built: env echo said 0.005, verify's object said 0.005, the trading loop's
object said 0.02 (lots 0.04/0.02 on the VPS).

Contract locked here (ISSUE-089 follow-up):
1. verify_account must construct the trader WITH the runner-style
   risk_config — otherwise it can only ever see the env branch.
2. With RISK_PER_TRADE_{account} set (compose env): the verified object
   carries 0.005 and preset p22-cent PASSES on risk_per_trade.
3. With the per-account env MISSING: the verified object carries the 2%
   generic default and preset p22-cent FAILS on risk_per_trade — the
   script must be able to catch exactly what shipped to production.

Causal proof: contract (1) is RED until verify_deploy mirrors the runner.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.verify_deploy as vd  # noqa: E402


def _clear_env(monkeypatch):
    monkeypatch.delenv("RISK_PER_TRADE", raising=False)
    for n in ("P1", "P2", "P3"):
        monkeypatch.delenv(f"RISK_PER_TRADE_{n}", raising=False)


@pytest.fixture()
def p_db(tmp_path, monkeypatch):
    """Seeded P-engine DB, wired through DB_PATH the way the compose does —
    verify_account resolves P-account ids from THIS file."""
    from metty.core.db import init_db, insert_account
    db = tmp_path / "oracle_p2.db"
    init_db(db)
    insert_account(
        name="P2", balance=2000.0, leverage=2000,
        bridge_host="mt5p2", bridge_port=8001,
        signal_group="volume", db_path=db,
    )
    monkeypatch.setenv("DB_PATH", str(db))
    return db


class TestVerifyDeployRunnerMirror:
    """verify_account must inspect the object the runner actually builds."""

    def test_constructs_trader_with_runner_style_risk_config(self):
        """Contract (1), causal RED pre-fix: the source must pass the same
        generic-key risk_config oracle_runner.py:253 passes — a bare
        LiveTrader(...) can never see the runtime branch that mattered."""
        import inspect
        src = inspect.getsource(vd.verify_account)
        assert "risk_config" in src, (
            "verify_account constructs LiveTrader WITHOUT risk_config — it "
            "verifies an object the runtime never builds (the blind spot "
            "that let the 2026-11-17 2% sizing bug ship green)"
        )
        assert 'os.environ.get("RISK_PER_TRADE", "0.02")' in src, (
            "the risk_config must mirror oracle_runner.py exactly "
            '(generic key, 2% default) — anything else verifies a '
            "different object than the loop"
        )

    def test_p22_cent_passes_when_env_correct(self, monkeypatch, p_db):
        """Contract (2): compose env present → verified object risk 0.005 →
        preset p22-cent has no risk_per_trade failure."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("RISK_PER_TRADE_P2", "0.005")
        expect = vd._PRESETS["p22-cent"]["P2"]
        rows, failures = vd.verify_account("P2", expect)
        shown = dict(rows).get("risk_per_trade")
        assert shown == "0.005", f"object risk {shown!r} ≠ 0.005"
        assert not any("risk_per_trade" in f for f in failures), failures

    def test_p22_cent_fails_when_runtime_risk_wrong(self, monkeypatch, p_db):
        """Contract (3), the production scenario inverted: per-account env
        MISSING → the runner-shaped object holds the 2% generic default →
        preset p22-cent must FAIL on risk_per_trade. This is the exact
        condition that shipped to the VPS green on 2026-11-17."""
        _clear_env(monkeypatch)
        expect = vd._PRESETS["p22-cent"]["P2"]
        rows, failures = vd.verify_account("P2", expect)
        shown = dict(rows).get("risk_per_trade")
        assert shown == "0.02", (
            f"env-missing runner object should hold the 2% default, got {shown!r}"
        )
        assert any("risk_per_trade" in f for f in failures), (
            "verify_deploy did NOT fail a 2% runtime risk against the 0.005 "
            "expectation — the p22-cent preset cannot catch the sizing bug"
        )
