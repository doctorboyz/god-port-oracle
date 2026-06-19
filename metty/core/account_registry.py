"""Centralized account registry — single source of truth for all account config.

Adding a new account (e.g. D) requires ONLY:
1. Add mt5d service to docker-compose.vps.yml
2. Add *_D vars to .env
3. Add D to the ACCOUNTS env var
4. NO code changes needed

The registry reads everything from environment variables and auto-allocates
ports (bridge_port = 5005 + index, vnc_port = 5900 + index).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Default port allocation: first account gets 5005, each subsequent +1
_BASE_BRIDGE_PORT = 5005
_BASE_VNC_PORT = 5900
_BRIDGE_INTERNAL_PORT = 8001  # Inside Docker container (always the same)

# Default drawdown config for new/demo accounts
_DEFAULT_DAILY_LIMIT = 0.10      # 10%
_DEFAULT_WEEKLY_LIMIT = 0.20     # 20%
_DEFAULT_ACCOUNT_LIMIT = 0.50    # 50%
_DEFAULT_COOLDOWN_HOURS = 2


@dataclass(frozen=True)
class AccountConfigInfo:
    """All configuration for a single trading account, derived from env vars.

    This is the SINGLE SOURCE OF TRUTH. All modules should read account
    config from here, not from inline dicts.
    """
    name: str                              # "A", "B", "C", "D"
    account_id: int                        # 1, 2, 3, 4 (DB primary key)
    broker_login: str
    broker_password: str
    broker_server: str
    bridge_host: str                        # mt5a, mt5b, etc. (Docker) or vpsdeluna (macOS)
    bridge_port: int                       # 5005, 5006, etc.
    initial_balance: float
    leverage: int
    signal_group: str                      # volume, ob_os, ma, sentiment
    symbol: str                            # XAUUSD or XAUUSDm
    risk_per_trade: float
    max_positions: int
    atr_multiplier: float
    rr_ratio: float
    min_confidence: float
    buy_min_confidence: float
    initial_equity: float
    m5_max_spread: float
    scalp_max_spread: float
    partial_tp_enabled: bool
    tp1_ratio: float
    rr_scale_in: float
    drawdown_daily_limit_pct: float
    drawdown_weekly_limit_pct: float
    drawdown_account_limit_pct: float
    drawdown_cooldown_hours: int
    # Docker/VPS config
    mt5_container: str                     # mt5a, mt5b, etc.
    vnc_port: int                          # 5900, 5901, etc.
    bridge_internal_port: int = _BRIDGE_INTERNAL_PORT


def get_active_accounts() -> list[str]:
    """Return list of active account letters from ACCOUNTS env var.

    Example: ACCOUNTS=A,B,C,D → ["A", "B", "C", "D"]
    """
    raw = os.environ.get("ACCOUNTS", "A,B,C")
    return [a.strip().upper() for a in raw.split(",") if a.strip()]


def get_account_configs(accounts: Optional[list[str]] = None) -> dict[str, AccountConfigInfo]:
    """Build AccountConfigInfo for each active account from env vars.

    This is the SINGLE SOURCE OF TRUTH. All call sites should use this.
    """
    if accounts is None:
        accounts = get_active_accounts()

    configs = {}
    for i, name in enumerate(accounts):
        configs[name] = _build_account_info(name, i)

    logger.info(
        "[AccountRegistry] Loaded %d accounts: %s",
        len(configs),
        list(configs.keys()),
    )
    return configs


def get_account_config(name: str) -> AccountConfigInfo:
    """Get config for a single account by name (e.g. 'A').

    Raises ValueError if the account is not in the ACCOUNTS env var.
    """
    configs = get_account_configs()
    if name not in configs:
        raise ValueError(
            f"Account '{name}' not found in ACCOUNTS env var. "
            f"Active accounts: {list(configs.keys())}"
        )
    return configs[name]


def get_symbol_map(accounts: Optional[list[str]] = None) -> dict[str, str]:
    """Map account name to trading symbol (XAUUSD vs XAUUSDm).

    Override per account with MT5_SYMBOL_<NAME> env var.
    Account A defaults to XAUUSDm (Standard account), others to XAUUSD (Pro account).
    """
    if accounts is None:
        accounts = get_active_accounts()
    result = {}
    for i, name in enumerate(accounts):
        default_symbol = "XAUUSDm" if name == "A" else "XAUUSD"
        result[name] = os.environ.get(f"MT5_SYMBOL_{name}", default_symbol)
    return result


def get_routing(accounts: Optional[list[str]] = None) -> dict[str, list[str]]:
    """Build signal-group-to-account routing from env vars.

    Default routing: accounts cycle through volume, ob_os, ma groups.
    Sentiment group always goes to all accounts.

    Override with SIGNAL_GROUP_<NAME> env var (e.g. SIGNAL_GROUP_A=volume).
    """
    if accounts is None:
        accounts = get_active_accounts()

    default_groups = ["volume", "ob_os", "ma"]
    routing: dict[str, list[str]] = {}

    for i, name in enumerate(accounts):
        group = os.environ.get(
            f"SIGNAL_GROUP_{name}",
            default_groups[i % len(default_groups)],
        )
        routing.setdefault(group, []).append(name)

    # Sentiment always goes to all accounts
    routing["sentiment"] = list(accounts)

    return routing


def get_account_ids(accounts: Optional[list[str]] = None) -> dict[str, int]:
    """Map account name to DB primary key (1-indexed).

    A=1, B=2, C=3, D=4, etc.
    """
    if accounts is None:
        accounts = get_active_accounts()
    return {name: i + 1 for i, name in enumerate(accounts)}


# ─── Internal helpers ──────────────────────────────────────────────────


def _build_account_info(name: str, index: int) -> AccountConfigInfo:
    """Build AccountConfigInfo for a single account from env vars.

    Auto-allocates ports if not explicitly set:
        bridge_port = 5005 + index
        vnc_port = 5900 + index
        bridge_host = mt5{name.lower()} (Docker) or vpsdeluna (macOS)
    """
    default_bridge_port = _BASE_BRIDGE_PORT + index
    default_bridge_host = f"mt5{name.lower()}"

    # Check if running inside Docker (use mt5X hostnames) or locally (use vpsdeluna)
    # The env var MT5_BRIDGE_<NAME>_HOST overrides everything
    bridge_host = os.environ.get(f"MT5_BRIDGE_{name}_HOST", default_bridge_host)
    bridge_port = int(os.environ.get(
        f"MT5_BRIDGE_{name}_PORT",
        str(default_bridge_port),
    ))

    # Symbol: A uses XAUUSDm (Standard account), others use XAUUSD (Pro account)
    # Can be overridden with MT5_SYMBOL_<NAME> env var
    default_symbol = "XAUUSDm" if name == "A" else "XAUUSD"
    symbol = os.environ.get(f"MT5_SYMBOL_{name}", default_symbol)

    # Signal group: cycle through volume/ob_os/ma, override with env var
    default_groups = ["volume", "ob_os", "ma"]
    signal_group = os.environ.get(
        f"SIGNAL_GROUP_{name}",
        default_groups[index % len(default_groups)],
    )

    # Risk per trade: default 1% for real accounts (A), 2% for demo
    default_risk = 0.01 if name == "A" else 0.02
    risk_per_trade = float(os.environ.get(
        f"RISK_PER_TRADE_{name}",
        os.environ.get("RISK_PER_TRADE", str(default_risk)),
    ))

    # BUY confidence: stricter for real accounts (A=0.50 vs 0.45 for demo)
    # BUY has lower WR than SELL, so real accounts need higher confidence
    default_buy_confidence = 0.50 if name == "A" else 0.45

    # Drawdown config from env vars with sensible defaults
    # Real account (A): strict 20/30/30%, Demo accounts: lenient 10/20/50%
    if name == "A":
        default_daily = 0.20
        default_weekly = 0.30
        default_account = 0.30
        default_cooldown = 4
    else:
        default_daily = _DEFAULT_DAILY_LIMIT
        default_weekly = _DEFAULT_WEEKLY_LIMIT
        default_account = _DEFAULT_ACCOUNT_LIMIT
        default_cooldown = _DEFAULT_COOLDOWN_HOURS

    return AccountConfigInfo(
        name=name,
        account_id=index + 1,
        broker_login=os.environ.get(f"MT5_LOGIN_{name}", ""),
        broker_password=os.environ.get(f"MT5_PASSWORD_{name}", ""),
        broker_server=os.environ.get(f"MT5_SERVER_{name}", "Exness-MT5"),
        bridge_host=bridge_host,
        bridge_port=bridge_port,
        initial_balance=float(os.environ.get(
            f"INITIAL_BALANCE_{name}",
            os.environ.get("INITIAL_BALANCE", "500"),
        )),
        leverage=int(os.environ.get(f"LEVERAGE_{name}", "500")),
        signal_group=signal_group,
        symbol=symbol,
        risk_per_trade=risk_per_trade,
        max_positions=int(os.environ.get(
            f"MAX_POSITIONS_{name}",
            os.environ.get("MAX_POSITIONS_PER_ACCOUNT", "5"),
        )),
        atr_multiplier=float(os.environ.get(
            f"ATR_MULTIPLIER_{name}",
            os.environ.get("ATR_MULTIPLIER", "2.5"),
        )),
        rr_ratio=float(os.environ.get(
            f"RR_RATIO_{name}",
            os.environ.get("RR_RATIO", "2.5"),
        )),
        min_confidence=float(os.environ.get(
            f"MIN_CONFIDENCE_{name}",
            os.environ.get("MIN_CONFIDENCE", "0.45"),
        )),
        buy_min_confidence=float(os.environ.get(
            f"BUY_MIN_CONFIDENCE_{name}",
            os.environ.get("BUY_MIN_CONFIDENCE", str(default_buy_confidence)),
        )),
        initial_equity=float(os.environ.get(
            f"INITIAL_EQUITY_{name}",
            os.environ.get("INITIAL_EQUITY", "500"),
        )),
        m5_max_spread=float(os.environ.get(
            f"M5_MAX_SPREAD_{name}",
            os.environ.get("M5_SCALP_SPREAD_MAX", "30"),
        )),
        scalp_max_spread=float(os.environ.get(
            f"SCALP_MAX_SPREAD_{name}",
            os.environ.get("SCALP_SPREAD_MAX", "30"),
        )),
        partial_tp_enabled=os.environ.get(
            f"PARTIAL_TP_ENABLED_{name}",
            os.environ.get("PARTIAL_TP_ENABLED", "0"),
        ) == "1",
        tp1_ratio=float(os.environ.get(
            f"TP1_RATIO_{name}",
            os.environ.get("TP1_RATIO", "0.5"),
        )),
        rr_scale_in=float(os.environ.get(
            f"RR_SCALE_IN_{name}",
            os.environ.get("RR_SCALE_IN", "2.5"),
        )),
        drawdown_daily_limit_pct=float(os.environ.get(
            f"DRAWDOWN_DAILY_LIMIT_{name}",
            str(default_daily),
        )),
        drawdown_weekly_limit_pct=float(os.environ.get(
            f"DRAWDOWN_WEEKLY_LIMIT_{name}",
            str(default_weekly),
        )),
        drawdown_account_limit_pct=float(os.environ.get(
            f"DRAWDOWN_ACCOUNT_LIMIT_{name}",
            str(default_account),
        )),
        drawdown_cooldown_hours=int(os.environ.get(
            f"DRAWDOWN_COOLDOWN_HOURS_{name}",
            str(default_cooldown),
        )),
        mt5_container=f"mt5{name.lower()}",
        vnc_port=_BASE_VNC_PORT + index,
    )