"""Causal tests for portfolio signal-group wiring (P-accounts).

Bug symptom (2026-09-18, engine-p1 logs): every P-engine cycle failed with
"Unknown account: P1" → no candles → no trades.

Root cause: SIGNAL_GROUP_Pn=portfolio (set by oracle seeding and compose) but
SignalGroup enum had no "portfolio" member, so get_bridge_config("P1") raised
ValueError("'portfolio' is not a valid SignalGroup") — caught and misreported
as "Unknown account". A second hazard: M5Scalp._get_account_config silently
fell back to account "A", which on a P-engine pointed at Real-A's bridge.

These tests fail before the fix and pass after:
  - test_bridge_config_accepts_portfolio_group  (causal, enum)
  - test_routing_maps_portfolio_group           (manager group_map)
  - test_scalp_config_no_fallback_to_real_a     (safety, fallback removal)
"""

import os

import pytest

_P_ENV_KEYS = [
    "ACCOUNTS",
    "SIGNAL_GROUP_P1",
    "MT5_LOGIN_P1",
    "MT5_PASSWORD_P1",
    "MT5_SERVER_P1",
    "MT5_BRIDGE_P1_HOST",
    "MT5_BRIDGE_P1_PORT",
    "ACCOUNT_TYPE_P1",
]


@pytest.fixture(autouse=True)
def clean_env():
    """Remove P-account env vars before each test, restore after."""
    original = {}
    for key in _P_ENV_KEYS:
        if key in os.environ:
            original[key] = os.environ.pop(key, None)
    yield
    for key in _P_ENV_KEYS:
        if key in os.environ and key not in original:
            del os.environ[key]
    os.environ.update({k: v for k, v in original.items() if v is not None})


def _set_p1_env():
    os.environ["ACCOUNTS"] = "P1"
    os.environ["SIGNAL_GROUP_P1"] = "portfolio"
    os.environ["MT5_LOGIN_P1"] = "184100001"
    os.environ["MT5_PASSWORD_P1"] = "pw"
    os.environ["MT5_SERVER_P1"] = "Exness-MT5Trial7"


class TestSignalGroupPortfolio:
    """The causal mechanism: SignalGroup must accept 'portfolio'."""

    def test_enum_accepts_portfolio(self):
        from metty.core.models import SignalGroup

        assert SignalGroup("portfolio") is SignalGroup.PORTFOLIO

    def test_bridge_config_accepts_portfolio_group(self):
        """get_bridge_config("P1") must not raise — this is the exact call
        that failed in the engine logs before the enum fix."""
        from metty.core.account_registry import get_bridge_config
        from metty.core.models import SignalGroup

        _set_p1_env()
        config = get_bridge_config("P1")
        assert config.name == "P1"
        assert config.signal_group is SignalGroup.PORTFOLIO


class TestBridgeManagerRouting:
    """Manager._build_routing must not silently fold 'portfolio' into VOLUME."""

    def test_routing_maps_portfolio_group(self):
        from metty.bridge.manager import MultiAccountManager
        from metty.core.models import SignalGroup

        _set_p1_env()
        routing = MultiAccountManager._build_routing()
        assert routing.get(SignalGroup.PORTFOLIO) == ["P1"]


class TestScalpAccountConfigSafety:
    """M5Scalp must never silently fall back to account A (Real-A bridge)."""

    @staticmethod
    def _make_scalp(account: str):
        from metty.execution.m5_scalp_trader import M5ScalpTrader

        scalp = M5ScalpTrader.__new__(M5ScalpTrader)
        scalp.account = account
        return scalp

    def test_scalp_config_no_fallback_to_real_a(self):
        """Unknown account must raise loud, not return account A's config.

        Before the fix this returned get_bridge_config("A") — in a mixed
        ACCOUNTS env a P-trader would trade on the real-money A bridge.
        """
        _set_p1_env()
        scalp = self._make_scalp("P9")  # P9 not in ACCOUNTS=P1
        with pytest.raises(ValueError, match="P9"):
            scalp._get_account_config()

    def test_scalp_config_resolves_known_portfolio_account(self):
        _set_p1_env()
        scalp = self._make_scalp("P1")
        config = scalp._get_account_config()
        assert config.name == "P1"