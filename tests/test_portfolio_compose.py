"""Sanity tests for the P1-P3 portfolio additions in docker-compose.vps.yml.

Guards the deploy constraints Hermes set (Q2/Q5):
  - 3 separate engine containers + 3 separate MT5 containers exist
  - port allocation does not collide with legacy services
  - Real-A (oracle-engine) env stays untouched — its config is the contract
    ห้าม recreate / ห้ามแตะใน step นี้
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

COMPOSE = Path(__file__).parent.parent / "docker-compose.vps.yml"


@pytest.fixture(scope="module")
def doc():
    return yaml.safe_load(COMPOSE.read_text())


class TestNewServices:
    def test_six_new_services_defined(self, doc):
        for name in ("mt5p1", "mt5p2", "mt5p3",
                     "oracle-engine-p1", "oracle-engine-p2", "oracle-engine-p3"):
            assert name in doc["services"], f"missing service {name}"

    def test_each_engine_is_separate_container(self, doc):
        """Q2: isolation per account — one engine container per P-account."""
        for n in (1, 2, 3):
            env = doc["services"][f"oracle-engine-p{n}"]["environment"]
            accounts = next(e for e in env if e.startswith("ACCOUNTS="))
            assert accounts == f"ACCOUNTS=P{n}"
            db = next(e for e in env if e.startswith("DB_PATH="))
            assert db == f"DB_PATH=/app/data/oracle_p{n}.db"

    def test_mt5_volumes_isolated(self, doc):
        vols = [doc["services"][f"mt5p{n}"]["volumes"][0].split(":")[0] for n in (1, 2, 3)]
        assert vols == ["mt5-config-p1", "mt5-config-p2", "mt5-config-p3"]


class TestPortAllocation:
    def test_no_port_collisions(self, doc):
        seen: dict[str, str] = {}
        for svc, cfg in doc["services"].items():
            for binding in cfg.get("ports", []):
                host_port = binding.split(":")[0]
                assert host_port not in seen, (
                    f"port {host_port} used by both {seen[host_port]} and {svc}"
                )
                seen[host_port] = svc

    def test_p_bridges_use_5009_5011(self, doc):
        for n, port in ((1, "5009"), (2, "5010"), (3, "5011")):
            ports = doc["services"][f"mt5p{n}"]["ports"]
            assert f"{port}:8001" in ports


class TestRealAUntouched:
    def test_oracle_engine_env_has_no_p_account_leak(self, doc):
        """Real-A container must not gain any P-account env — its config is
        frozen until คุณหมอ decides ISSUE-003 separately."""
        import re
        p_env = re.compile(r"_P[123](=|:|\$)")
        for svc in ("oracle-engine", "oracle-engine-train"):
            for e in doc["services"][svc]["environment"]:
                assert not p_env.search(e), f"{svc} leaked portfolio env: {e}"

    def test_oracle_engine_depends_only_on_mt5a(self, doc):
        deps = doc["services"]["oracle-engine"]["depends_on"]
        assert list(deps) == ["mt5a"]


class TestVariantEnv:
    # 2026-11-16 supersede (คุณหมอสั่งผ่าน Hermes): P1-P3 switched from the
    # retired September paper-round variants to the P22 champion (mirror of
    # demo C / mr-bet-p22-C). Criteria are PRESERVED — only the pinned
    # values changed, matching the new compose contract.

    def test_entry_hours_match_variant_matrix(self, doc):
        # P22 won with full occupancy — hour gates OPEN on all three
        # (mirror demo C: ENTRY_HOURS_C= empty).
        for n in (1, 2, 3):
            env = doc["services"][f"oracle-engine-p{n}"]["environment"]
            assert any(e == f"ENTRY_HOURS_P{n}=" for e in env), \
                f"oracle-engine-p{n}: ENTRY_HOURS_P{n} must be empty (P22: no hour gate)"

    def test_blocked_hours_negative_ev_veto_every_engine(self, doc):
        # Mirror demo C: no blocked-hours veto either.
        for n in (1, 2, 3):
            env = doc["services"][f"oracle-engine-p{n}"]["environment"]
            assert any(e == f"BLOCKED_HOURS_P{n}=" for e in env), \
                f"oracle-engine-p{n}: BLOCKED_HOURS_P{n} must be empty (P22: no hour gate)"

    def test_single_position_per_account(self, doc):
        # Criterion unchanged: exactly one position per P-account. Now
        # HARD-CODED (=1, not ${...:-1}) so a stale host .env value can't
        # raise it — real cent money.
        for n in (1, 2, 3):
            env = doc["services"][f"oracle-engine-p{n}"]["environment"]
            assert any(e == f"MAX_POSITIONS_P{n}=1" for e in env), \
                f"oracle-engine-p{n}: MAX_POSITIONS_P{n} must be hard-coded 1"
            assert any(e == f"MAX_POSITIONS_CAP_P{n}=1" for e in env), \
                f"oracle-engine-p{n}: MAX_POSITIONS_CAP_P{n} must be hard-coded 1"

    def test_paper_round_is_demo_type(self, doc):
        # Supersede: accounts are now REAL Exness Standard Cent — the old
        # "paper round = demo" contract no longer applies. cent_real per
        # account_registry: "Portfolio cent accounts set ACCOUNT_TYPE_Pn=
        # cent_real when live".
        for n in (1, 2, 3):
            env = doc["services"][f"oracle-engine-p{n}"]["environment"]
            assert any(e == f"ACCOUNT_TYPE_P{n}=cent_real" for e in env), \
                f"oracle-engine-p{n}: ACCOUNT_TYPE_P{n} must be cent_real"

    def test_no_credentials_hardcoded(self, doc):
        """ห้ามใส่ credential ใน repo — every login/password must come from .env."""
        for svc, cfg in doc["services"].items():
            for e in cfg["environment"]:
                if "PASSWORD" in e and "mt5password" not in e:
                    assert "${" in e, f"{svc} hardcodes a credential: {e}"

    def test_engine_data_bind_mounts_for_host_cron(self, doc):
        """Host crontab (portfolio_manager.py) must reach the same DB files
        the engines write — bind mounts, one dir per account."""
        for n in (1, 2, 3):
            vols = doc["services"][f"oracle-engine-p{n}"]["volumes"]
            assert any(v.endswith(":/app/data") and f"/p{n}" in v for v in vols)