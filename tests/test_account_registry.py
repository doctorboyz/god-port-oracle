"""Tests for metty.core.account_registry — dynamic multi-account configuration."""

import os
import pytest

# Ensure clean environment for each test
_ENV_KEYS = [
    "ACCOUNTS",
    "ACCOUNT_TYPE_A", "ACCOUNT_TYPE_B", "ACCOUNT_TYPE_C", "ACCOUNT_TYPE_D",
    "MT5_LOGIN_A", "MT5_PASSWORD_A", "MT5_SERVER_A",
    "MT5_BRIDGE_A_HOST", "MT5_BRIDGE_A_PORT",
    "MT5_SYMBOL_A", "INITIAL_EQUITY_A", "INITIAL_BALANCE_A",
    "RISK_PER_TRADE_A", "BUY_MIN_CONFIDENCE_A",
    "DRAWDOWN_DAILY_LIMIT_A", "DRAWDOWN_WEEKLY_LIMIT_A",
    "DRAWDOWN_ACCOUNT_LIMIT_A", "DRAWDOWN_COOLDOWN_HOURS_A",
    "MT5_LOGIN_B", "MT5_SYMBOL_B",
    "MT5_LOGIN_C", "MT5_SYMBOL_C",
    "MT5_LOGIN_D", "MT5_SYMBOL_D",
    "SIGNAL_GROUP_A", "SIGNAL_GROUP_B", "SIGNAL_GROUP_C",
]


@pytest.fixture(autouse=True)
def clean_env():
    """Remove account-related env vars before each test."""
    original = {}
    for key in _ENV_KEYS:
        if key in os.environ:
            original[key] = os.environ.pop(key, None)
    yield
    # Restore
    os.environ.update({k: v for k, v in original.items() if v is not None})
    # Remove any that were set during test
    for key in _ENV_KEYS:
        if key not in original and key in os.environ:
            del os.environ[key]


class TestGetActiveAccounts:
    """Tests for get_active_accounts()."""

    def test_default_accounts(self):
        from metty.core.account_registry import get_active_accounts
        accounts = get_active_accounts()
        assert accounts == ["A", "B", "C"]

    def test_custom_accounts(self):
        from metty.core.account_registry import get_active_accounts
        os.environ["ACCOUNTS"] = "A,B,C,D"
        accounts = get_active_accounts()
        assert accounts == ["A", "B", "C", "D"]

    def test_whitespace_handling(self):
        from metty.core.account_registry import get_active_accounts
        os.environ["ACCOUNTS"] = " A , B , C "
        accounts = get_active_accounts()
        assert accounts == ["A", "B", "C"]

    def test_single_account(self):
        from metty.core.account_registry import get_active_accounts
        os.environ["ACCOUNTS"] = "A"
        accounts = get_active_accounts()
        assert accounts == ["A"]

    def test_lowercase_uppercased(self):
        from metty.core.account_registry import get_active_accounts
        os.environ["ACCOUNTS"] = "a,b,c"
        accounts = get_active_accounts()
        assert accounts == ["A", "B", "C"]


class TestGetAccountConfigs:
    """Tests for get_account_configs()."""

    def test_default_three_accounts(self):
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        assert set(configs.keys()) == {"A", "B", "C"}

    def test_port_allocation(self):
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        assert configs["A"].bridge_port == 5005
        assert configs["B"].bridge_port == 5006
        assert configs["C"].bridge_port == 5007

    def test_dynamic_port_allocation(self):
        from metty.core.account_registry import get_account_configs
        os.environ["ACCOUNTS"] = "A,B,C,D"
        configs = get_account_configs()
        assert configs["D"].bridge_port == 5008

    def test_symbol_defaults(self):
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        # A uses XAUUSDm (Standard account), others XAUUSD (Pro)
        assert configs["A"].symbol == "XAUUSDm"
        assert configs["B"].symbol == "XAUUSD"
        assert configs["C"].symbol == "XAUUSD"

    def test_env_override_symbol(self):
        from metty.core.account_registry import get_account_configs
        os.environ["MT5_SYMBOL_A"] = "XAUUSD"
        configs = get_account_configs()
        assert configs["A"].symbol == "XAUUSD"

    def test_drawdown_defaults_real_account(self):
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        a = configs["A"]
        assert a.drawdown_daily_limit_pct == 0.20
        assert a.drawdown_weekly_limit_pct == 0.30
        assert a.drawdown_account_limit_pct == 0.30
        assert a.drawdown_cooldown_hours == 4

    def test_drawdown_defaults_demo_account(self):
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        b = configs["B"]
        assert b.drawdown_daily_limit_pct == 0.10
        assert b.drawdown_weekly_limit_pct == 0.20
        assert b.drawdown_account_limit_pct == 0.50
        assert b.drawdown_cooldown_hours == 2

    def test_drawdown_env_override(self):
        from metty.core.account_registry import get_account_configs
        os.environ["DRAWDOWN_DAILY_LIMIT_A"] = "0.15"
        configs = get_account_configs()
        assert configs["A"].drawdown_daily_limit_pct == 0.15

    def test_buy_min_confidence_real_account(self):
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        # A (real) has stricter BUY filter at 0.50
        assert configs["A"].buy_min_confidence == 0.50

    def test_buy_min_confidence_demo_account(self):
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        assert configs["B"].buy_min_confidence == 0.45

    def test_buy_min_confidence_env_override(self):
        from metty.core.account_registry import get_account_configs
        os.environ["BUY_MIN_CONFIDENCE_A"] = "0.55"
        configs = get_account_configs()
        assert configs["A"].buy_min_confidence == 0.55

    def test_risk_per_trade_defaults(self):
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        assert configs["A"].risk_per_trade == 0.01  # Real: 1%
        assert configs["B"].risk_per_trade == 0.02  # Demo: 2%

    def test_container_names(self):
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        assert configs["A"].mt5_container == "mt5a"
        assert configs["B"].mt5_container == "mt5b"

    def test_vnc_port_allocation(self):
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        assert configs["A"].vnc_port == 5900
        assert configs["B"].vnc_port == 5901
        assert configs["C"].vnc_port == 5902


class TestGetAccountConfig:
    """Tests for get_account_config(name)."""

    def test_existing_account(self):
        from metty.core.account_registry import get_account_config
        config = get_account_config("A")
        assert config.name == "A"
        assert config.bridge_port == 5005

    def test_nonexistent_account_raises(self):
        from metty.core.account_registry import get_account_config
        with pytest.raises(ValueError, match="Account 'Z' not found"):
            get_account_config("Z")

    def test_dynamic_account(self):
        from metty.core.account_registry import get_account_config
        os.environ["ACCOUNTS"] = "A,B,C,D"
        config = get_account_config("D")
        assert config.name == "D"
        assert config.bridge_port == 5008


class TestGetSymbolMap:
    """Tests for get_symbol_map()."""

    def test_default_symbols(self):
        from metty.core.account_registry import get_symbol_map
        symbols = get_symbol_map()
        assert symbols["A"] == "XAUUSDm"
        assert symbols["B"] == "XAUUSD"
        assert symbols["C"] == "XAUUSD"

    def test_env_override(self):
        from metty.core.account_registry import get_symbol_map
        os.environ["MT5_SYMBOL_A"] = "XAUUSD"
        symbols = get_symbol_map()
        assert symbols["A"] == "XAUUSD"

    def test_dynamic_account_symbols(self):
        from metty.core.account_registry import get_symbol_map
        os.environ["ACCOUNTS"] = "A,B,C,D"
        symbols = get_symbol_map()
        # D defaults to XAUUSD (not Standard account)
        assert symbols["D"] == "XAUUSD"


class TestGetRouting:
    """Tests for get_routing()."""

    def test_default_routing(self):
        from metty.core.account_registry import get_routing
        routing = get_routing()
        assert "volume" in routing
        assert "ob_os" in routing
        assert "ma" in routing
        assert "sentiment" in routing
        assert "A" in routing["volume"]
        assert "B" in routing["ob_os"]
        assert "C" in routing["ma"]
        assert len(routing["sentiment"]) == 3

    def test_env_override_signal_group(self):
        from metty.core.account_registry import get_routing
        os.environ["SIGNAL_GROUP_A"] = "ob_os"
        routing = get_routing()
        assert "A" in routing["ob_os"]

    def test_dynamic_account_routing(self):
        from metty.core.account_registry import get_routing
        os.environ["ACCOUNTS"] = "A,B,C,D"
        routing = get_routing()
        # D should cycle to "volume" group (index 3 % 3 = 0)
        assert "D" in routing["volume"]
        assert len(routing["sentiment"]) == 4


class TestGetAccountIds:
    """Tests for get_account_ids()."""

    def test_default_ids(self):
        from metty.core.account_registry import get_account_ids
        ids = get_account_ids()
        assert ids == {"A": 1, "B": 2, "C": 3}

    def test_dynamic_ids(self):
        from metty.core.account_registry import get_account_ids
        os.environ["ACCOUNTS"] = "A,B,C,D"
        ids = get_account_ids()
        assert ids == {"A": 1, "B": 2, "C": 3, "D": 4}


class TestAccountName:
    """Tests for AccountName dynamic string class."""

    def test_constants(self):
        from metty.core.models import AccountName
        assert AccountName.A == "A"
        assert AccountName.B == "B"
        assert AccountName.C == "C"

    def test_dynamic_creation(self):
        from metty.core.models import AccountName
        d = AccountName("D")
        assert d == "D"
        assert isinstance(d, str)

    def test_bracket_access(self):
        from metty.core.models import AccountName
        assert AccountName["A"] == "A"
        assert AccountName["D"] == "D"

    def test_string_comparison(self):
        from metty.core.models import AccountName
        assert AccountName.A == "A"
        assert AccountName("D") == "D"
        assert AccountName.A != "B"

    def test_dict_key(self):
        from metty.core.models import AccountName
        d = {AccountName.A: "hello"}
        assert d[AccountName.A] == "hello"
        assert d["A"] == "hello"

    def test_values(self):
        from metty.core.models import AccountName
        assert AccountName.values() == ["A", "B", "C"]

    def test_account_config_with_dynamic_name(self):
        from metty.core.models import AccountConfig
        cfg = AccountConfig(name="D", bridge_host="mt5d", bridge_port=5008)
        assert cfg.name == "D"
        assert cfg.bridge_port == 5008


class TestDrawdownConfig:
    """Tests for drawdown config from registry (backward compat)."""

    def test_real_account_strict(self):
        from broky.risk.drawdown_protection import get_drawdown_config
        config = get_drawdown_config("A")
        assert config["daily_limit_pct"] == 0.20
        assert config["weekly_limit_pct"] == 0.30
        assert config["account_limit_pct"] == 0.30
        assert config["cooldown_hours"] == 4

    def test_demo_account_lenient(self):
        from broky.risk.drawdown_protection import get_drawdown_config
        config = get_drawdown_config("B")
        assert config["daily_limit_pct"] == 0.10
        assert config["weekly_limit_pct"] == 0.20
        assert config["account_limit_pct"] == 0.50
        assert config["cooldown_hours"] == 2

    def test_unknown_account_defaults(self):
        from broky.risk.drawdown_protection import get_drawdown_config
        # Without ACCOUNTS env, D is unknown → fallback defaults
        config = get_drawdown_config("D")
        assert config["daily_limit_pct"] == 0.10
        assert config["account_limit_pct"] == 0.50

    def test_dynamic_account_config(self):
        from broky.risk.drawdown_protection import get_drawdown_config
        os.environ["ACCOUNTS"] = "A,B,C,D"
        os.environ["DRAWDOWN_DAILY_LIMIT_D"] = "0.15"
        config = get_drawdown_config("D")
        assert config["daily_limit_pct"] == 0.15

    def test_backward_compat_dict(self):
        from broky.risk.drawdown_protection import ACCOUNT_DRAWDOWN_CONFIGS
        configs = ACCOUNT_DRAWDOWN_CONFIGS()
        assert "A" in configs
        assert configs["A"]["daily_limit_pct"] == 0.20

    def test_buy_min_confidence_real(self):
        from broky.risk.drawdown_protection import get_buy_min_confidence
        assert get_buy_min_confidence("A") == 0.50

    def test_buy_min_confidence_demo(self):
        from broky.risk.drawdown_protection import get_buy_min_confidence
        assert get_buy_min_confidence("B") == 0.45

    def test_buy_min_confidence_backward_compat(self):
        from broky.risk.drawdown_protection import BUY_MIN_CONFIDENCE
        conf = BUY_MIN_CONFIDENCE()
        assert conf["A"] == 0.50
        assert conf["B"] == 0.45


class TestDrawdownProtectorIntegration:
    """Tests that DrawdownProtector works with registry configs."""

    def test_create_protector_from_registry(self):
        from broky.risk.drawdown_protection import DrawdownProtector, get_drawdown_config
        config = get_drawdown_config("A")
        protector = DrawdownProtector(
            initial_equity=100.0,
            **config,
        )
        can_trade, reason = protector.check(100.0)
        assert can_trade is True
        assert reason == "OK"

    def test_dynamic_account_protector(self):
        from broky.risk.drawdown_protection import DrawdownProtector, get_drawdown_config
        os.environ["ACCOUNTS"] = "A,B,C,D"
        config = get_drawdown_config("D")
        protector = DrawdownProtector(
            initial_equity=500.0,
            **config,
        )
        can_trade, reason = protector.check(500.0)
        assert can_trade is True


class TestAccountType:
    """Tests for account_type and display_name features."""

    def test_default_type_a_is_real(self):
        """Account A defaults to 'real' type."""
        os.environ["ACCOUNTS"] = "A,B,C"
        from metty.core.account_registry import get_account_config
        config = get_account_config("A")
        assert config.account_type == "real"
        assert config.display_name == "Real-A"

    def test_default_type_others_are_demo(self):
        """Accounts B, C, D default to 'demo' type."""
        os.environ["ACCOUNTS"] = "A,B,C,D"
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        assert configs["B"].account_type == "demo"
        assert configs["B"].display_name == "Demo-B"
        assert configs["C"].account_type == "demo"
        assert configs["C"].display_name == "Demo-C"
        assert configs["D"].account_type == "demo"
        assert configs["D"].display_name == "Demo-D"

    def test_env_override_type(self):
        """ACCOUNT_TYPE_<NAME> env var overrides default."""
        os.environ["ACCOUNTS"] = "A,B"
        os.environ["ACCOUNT_TYPE_A"] = "demo"
        os.environ["ACCOUNT_TYPE_B"] = "real"
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        assert configs["A"].account_type == "demo"
        assert configs["A"].display_name == "Demo-A"
        assert configs["B"].account_type == "real"
        assert configs["B"].display_name == "Real-B"

    def test_get_display_name_standalone(self):
        """get_display_name() works without full config."""
        from metty.core.account_registry import get_display_name
        assert get_display_name("A") == "Real-A"
        assert get_display_name("B") == "Demo-B"
        assert get_display_name("C") == "Demo-C"
        assert get_display_name("D") == "Demo-D"

    def test_type_affects_symbol(self):
        """Real accounts default to XAUUSDm, demo to XAUUSD."""
        os.environ["ACCOUNTS"] = "A,B"
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        assert configs["A"].symbol == "XAUUSDm"  # real = Standard
        assert configs["B"].symbol == "XAUUSD"    # demo = Pro

    def test_type_affects_risk(self):
        """Real accounts default to 1% risk, demo to 2%."""
        os.environ["ACCOUNTS"] = "A,B"
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        assert configs["A"].risk_per_trade == 0.01  # real
        assert configs["B"].risk_per_trade == 0.02  # demo

    def test_type_affects_buy_confidence(self):
        """Real accounts default to higher BUY min confidence."""
        os.environ["ACCOUNTS"] = "A,B"
        from metty.core.account_registry import get_account_configs
        configs = get_account_configs()
        assert configs["A"].buy_min_confidence == 0.50  # real
        assert configs["B"].buy_min_confidence == 0.45  # demo