"""Causal test: host-cron portfolio_manager must dial the PUBLISHED bridge port.

Bug (found while installing the ISSUE-094 crontab, 2026-10-03):
fetch_account_equity() built the MT5Bridge AccountConfig with
cfg.bridge_internal_port — a constant 8001 — instead of cfg.bridge_port, so the
HANDOFF runbook env MT5_BRIDGE_P*_PORT=5009/5010/5011 was a silent no-op.
Every host crontab run dialed 127.0.0.1:8001 (nothing listens there on the
host; containers publish 8001 as 5009/5010/5011), equity came back None, and
the drawdown/freeze checks ran blind on the baseline fallback.

The causal mechanism under test: the port value actually dialed by the
AccountConfig, given MT5_BRIDGE_P1_PORT in the environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class _CaptureBridge:
    """Fake MT5Bridge that records the config instead of dialing."""

    instances: list = []

    def __init__(self, config):
        self.config = config
        _CaptureBridge.instances.append(self)

    def fetch_account_info_sync(self):
        class _Info:
            equity = 123.45
        return _Info()


@pytest.fixture()
def capture_bridge(monkeypatch):
    _CaptureBridge.instances = []
    import metty.bridge.client as bridge_client
    monkeypatch.setattr(bridge_client, "MT5Bridge", _CaptureBridge)
    yield _CaptureBridge


class TestBridgePortIsEnvDriven:
    def test_env_override_port_is_dialed_not_internal_8001(self, capture_bridge, monkeypatch):
        """MT5_BRIDGE_P1_PORT=5009 (host-published port) must reach the
        AccountConfig — this is the exact runbook crontab scenario."""
        monkeypatch.setenv("ACCOUNTS", "P1")
        monkeypatch.setenv("MT5_BRIDGE_P1_HOST", "127.0.0.1")
        monkeypatch.setenv("MT5_BRIDGE_P1_PORT", "5009")

        from scripts.portfolio_manager import fetch_account_equity
        equity = fetch_account_equity("P1")

        assert equity == 123.45
        assert len(capture_bridge.instances) == 1
        cfg = capture_bridge.instances[0].config
        assert cfg.bridge_host == "127.0.0.1"
        assert cfg.bridge_port == 5009, (
            "host crontab dials the published port (5009), not the "
            f"container-internal 8001 — got {cfg.bridge_port}"
        )

    def test_control_no_env_uses_registry_port_not_8001(self, capture_bridge, monkeypatch):
        """Control: without env overrides the port still comes from the
        registry's bridge_port field (default 5005+index), never the
        fixed internal 8001 — proves the fix routes through the registry."""
        monkeypatch.setenv("ACCOUNTS", "P1")
        monkeypatch.delenv("MT5_BRIDGE_P1_HOST", raising=False)
        monkeypatch.delenv("MT5_BRIDGE_P1_PORT", raising=False)

        from metty.core.account_registry import get_account_config
        from scripts.portfolio_manager import fetch_account_equity
        expected = get_account_config("P1").bridge_port

        fetch_account_equity("P1")
        cfg = capture_bridge.instances[0].config
        assert cfg.bridge_port == expected
        assert cfg.bridge_port != 8001