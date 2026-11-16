"""Kill switch infra contract — docker-compose.vps.yml service wiring.

Mirrors the farm-bet infra lock pattern (tests/test_bet_farm_infra.py):
the compose service is pinned the same way the farm's oracle-engine-farmbet
is pinned, and — because this supervisor lives in the SAME compose file as
the production engines — the pre-existing services are pinned too:

  - the service set is exactly the 13 originals + kill-switch (additive)
  - zero KILL_* env leaks into any pre-existing service
  - every env key the service writes is runtime-safe (no dashes — the
    2026-11-11 live incident dropped dashed keys from PID-1 env)
  - the container env parsed by KillConfig.from_env agrees with the
    service definition (groups, thresholds, wiring — the env-coverage
    guard covers the other direction)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.kill_switch as ks  # noqa: E402
from scripts.kill_switch import KillConfig  # noqa: E402

COMPOSE = ROOT / "docker-compose.vps.yml"

# The 13 services that existed before the kill-switch task (drift lock —
# service set may only grow ADDITIVELY, matching the farm-bet pattern).
PRE_EXISTING_SERVICES = [
    "mt5a", "mt5b", "mt5c", "mt5d", "mt5q",
    "oracle-engine", "oracle-engine-train",
    "mt5p1", "mt5p2", "mt5p3",
    "oracle-engine-p1", "oracle-engine-p2", "oracle-engine-p3",
]


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _svc_env(services: dict, name: str) -> list[str]:
    return list(services[name]["environment"])


def _knob(env: list[str], key: str) -> str | None:
    matches = [e for e in env if e.startswith(key + "=")]
    return matches[0].split("=", 1)[1] if matches else None


class TestKillSwitchService:
    def test_service_exists_with_supervisor_entrypoint(self):
        svc = _compose()["services"]["kill-switch"]
        assert svc["container_name"] == "kill-switch"
        assert "scripts/kill_switch.py" in svc["command"]
        assert svc["restart"] == "unless-stopped"

    def test_dry_run_is_the_compose_default(self):
        """Order 2026-11-16: alert-only until the VPS dry-run period proves
        the design; arming happens in the host .env, never here."""
        env = _svc_env(_compose()["services"], "kill-switch")
        assert _knob(env, "KILL_SWITCH_DRY_RUN") == "${KILL_SWITCH_DRY_RUN:-1}"
        assert "KILL_SWITCH_DRY_RUN=1" not in env  # never hard-armed in repo

    def test_group_defaults_match_the_order(self):
        env = _svc_env(_compose()["services"], "kill-switch")
        assert _knob(env, "KILL_DEMO_ACCOUNTS") == "${KILL_DEMO_ACCOUNTS:-B,C,D}"
        assert _knob(env, "KILL_CENT_ACCOUNTS") == "${KILL_CENT_ACCOUNTS:-P1,P2,P3}"
        assert _knob(env, "KILL_REAL_ACCOUNTS") == "${KILL_REAL_ACCOUNTS:-A}"

    def test_trigger_thresholds_env_tunable(self):
        env = _svc_env(_compose()["services"], "kill-switch")
        assert _knob(env, "KILL_DAILY_LOSS_PCT") == "${KILL_DAILY_LOSS_PCT:-3.0}"
        assert _knob(env, "KILL_DD_PCT") == "${KILL_DD_PCT:-10.0}"
        assert _knob(env, "KILL_ORDER_RATE_PER_HOUR") == "${KILL_ORDER_RATE_PER_HOUR:-20}"
        assert _knob(env, "KILL_PRICE_DEV_PCT") == "${KILL_PRICE_DEV_PCT:-1.0}"
        assert _knob(env, "KILL_CONNECTIVITY_MINUTES") == "${KILL_CONNECTIVITY_MINUTES:-10}"
        assert _knob(env, "KILL_POLL_SECONDS") == "${KILL_POLL_SECONDS:-30}"

    def test_state_and_docker_socket_mounts(self):
        svc = _compose()["services"]["kill-switch"]
        vols = svc["volumes"]
        assert "kill-switch-state:/ks/state" in vols
        assert "/var/run/docker.sock:/var/run/docker.sock" in vols

    def test_engine_dbs_mounted_read_only(self):
        """The supervisor must be physically unable to write engine DBs —
        :ro at the mount level, mode=ro at the sqlite level (belt+braces)."""
        svc = _compose()["services"]["kill-switch"]
        vols = svc["volumes"]
        assert "oracle-data:/ksdata/a:ro" in vols
        assert "oracle-train-data:/ksdata/train:ro" in vols
        assert "${PORTFOLIO_DATA_DIR:-/opt/god-port/data}/p1:/ksdata/p1:ro" in vols
        assert "${PORTFOLIO_DATA_DIR:-/opt/god-port/data}/p2:/ksdata/p2:ro" in vols
        assert "${PORTFOLIO_DATA_DIR:-/opt/god-port/data}/p3:/ksdata/p3:ro" in vols
        assert not [v for v in vols if v.startswith(("oracle-data:", "oracle-train-data:"))
                    and not v.endswith(":ro")]

    def test_per_account_wiring(self):
        env = _svc_env(_compose()["services"], "kill-switch")
        assert _knob(env, "KILL_DB_PATH_A") == "/ksdata/a/oracle.db"
        for acct in ("B", "C", "D"):
            assert _knob(env, f"KILL_DB_PATH_{acct}") == "/ksdata/train/oracle_train.db"
        for n in (1, 2, 3):
            assert _knob(env, f"KILL_DB_PATH_P{n}") == f"/ksdata/p{n}/oracle_p{n}.db"
        assert _knob(env, "KILL_ENGINE_CONTAINER_B") == "oracle-engine-train"
        assert _knob(env, "KILL_ENGINE_CONTAINER_P1") == "oracle-engine-p1"
        # REAL GROUP: no stop wiring exists at all — code refuses one anyway
        assert not [e for e in env if e.startswith("KILL_ENGINE_CONTAINER_A")]

    def test_bridge_wiring_matches_engine_convention(self):
        env = _svc_env(_compose()["services"], "kill-switch")
        for name, host in [("A", "mt5a"), ("B", "mt5b"), ("C", "mt5c"),
                           ("D", "mt5d"), ("P1", "mt5p1"), ("P2", "mt5p2"),
                           ("P3", "mt5p3")]:
            assert _knob(env, f"MT5_BRIDGE_{name}_HOST") == host
            assert _knob(env, f"MT5_BRIDGE_{name}_PORT") == "8001"

    def test_env_keys_never_contain_dashes(self):
        """Same causal lock as the farmbet service (live incident
        2026-11-11): container runtimes DROP dashed env keys from the
        PID-1 environment — docker inspect shows them, the process
        doesn't get them."""
        env = _svc_env(_compose()["services"], "kill-switch")
        for line in env:
            key = line.split("=", 1)[0]
            assert re.fullmatch(r"[A-Za-z0-9_]+", key), \
                f"env key {key!r} contains a runtime-hostile character"

    def test_telegram_reader_contract(self):
        """TG_* first, legacy TELEGRAM_* fallback (PM reader pattern)."""
        env = _svc_env(_compose()["services"], "kill-switch")
        assert _knob(env, "TG_BOT_TOKEN") == "${TG_BOT_TOKEN:-}"
        assert _knob(env, "TG_CHAT_ID") == "${TG_CHAT_ID:-}"


class TestComposeDriftLock:
    def test_service_set_is_additive_only(self):
        services = set(_compose()["services"])
        assert set(PRE_EXISTING_SERVICES) <= services, \
            "pre-existing VPS service disappeared — additive-only violated"
        new = services - set(PRE_EXISTING_SERVICES)
        assert new == {"kill-switch"}, (
            f"unexpected new services in the VPS compose: {sorted(new)}"
        )

    def test_no_kill_env_leaks_into_pre_existing_services(self):
        services = _compose()["services"]
        for name in PRE_EXISTING_SERVICES:
            env = services[name].get("environment") or []
            leaked = [e for e in env if e.startswith("KILL_")]
            assert not leaked, f"{name} leaked kill-switch env: {leaked}"

    def test_real_a_service_untouched(self):
        """The Real-A iron rule, pinned at compose level: ACCOUNTS=A, its
        own DB, and its bridge wiring exactly as before the supervisor."""
        svc = _compose()["services"]["oracle-engine"]
        env = svc["environment"]
        assert _knob(env, "ACCOUNTS") == "A"
        assert _knob(env, "DB_PATH") == "/app/data/oracle.db"
        assert _knob(env, "MT5_BRIDGE_A_HOST") == "mt5a"
        assert svc["volumes"] == ["oracle-data:/app/data"]
        assert svc["restart"] == "unless-stopped"

    def test_new_volume_is_additive(self):
        volumes = _compose()["volumes"]
        assert "kill-switch-state" in volumes
        for pre in ("oracle-data", "oracle-train-data", "mt5-config-a"):
            assert pre in volumes


class TestEnvParserMatchesCompose:
    """The container env must parse into the same config the compose
    declares (the TestEnvParserMatchesYml pattern from the farmbet work —
    checking only the first account is how the dash-key bug hid)."""

    @pytest.fixture
    def config_from_compose_env(self, monkeypatch, tmp_path):
        """Set env from the compose service the way docker compose would
        (${VAR:-default} resolved against a CLEAN host env), then parse."""
        import os

        def resolve(value: str) -> str:
            m = re.match(r"^\$\{(\w+)(?::-([^}]*))?\}$", value)
            if m:
                return os.environ.get(m.group(1)) or (m.group(2) or "")
            return value

        # neutral host env first — only compose lines decide
        for line in _svc_env(_compose()["services"], "kill-switch"):
            monkeypatch.delenv(line.partition("=")[0], raising=False)
        monkeypatch.delenv("KILL_SWITCH_DRY_RUN", raising=False)
        for line in _svc_env(_compose()["services"], "kill-switch"):
            key, _, value = line.partition("=")
            monkeypatch.setenv(key, resolve(value))
        # state path into tmp — not part of the pinned contract
        monkeypatch.setenv("KILL_STATE_PATH", str(tmp_path / "state.json"))
        return KillConfig.from_env()

    def test_groups_match_compose(self, config_from_compose_env):
        config = config_from_compose_env
        by_group = {}
        for t in config.targets:
            by_group.setdefault(t.group, []).append(t.name)
        assert by_group[ks.GROUP_DEMO] == ["B", "C", "D"]
        assert by_group[ks.GROUP_CENT] == ["P1", "P2", "P3"]
        assert by_group[ks.GROUP_REAL] == ["A"]

    def test_wiring_matches_compose_for_every_account(
            self, config_from_compose_env):
        config = config_from_compose_env
        targets = {t.name: t for t in config.targets}
        assert targets["A"].db_path == "/ksdata/a/oracle.db"
        assert targets["B"].db_path == "/ksdata/train/oracle_train.db"
        assert targets["D"].db_path == "/ksdata/train/oracle_train.db"
        assert targets["P2"].db_path == "/ksdata/p2/oracle_p2.db"
        assert targets["A"].engine_container is None  # real = no stop path
        assert targets["B"].engine_container == "oracle-engine-train"
        assert targets["P3"].engine_container == "oracle-engine-p3"
        assert (targets["C"].bridge_host, targets["C"].bridge_port) \
            == ("mt5c", 8001)

    def test_compose_default_env_parses_dry_run(self, config_from_compose_env):
        """${KILL_SWITCH_DRY_RUN:-1} with no host override → dry-run."""
        assert config_from_compose_env.dry_run is True

    def test_compose_thresholds(self, config_from_compose_env):
        config = config_from_compose_env
        assert config.daily_loss_pct == 3.0
        assert config.dd_pct == 10.0
        assert config.order_rate_per_hour == 20
        assert config.price_dev_pct == 1.0
        assert config.connectivity_minutes == 10
        assert config.poll_seconds == 30
