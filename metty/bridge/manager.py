"""Multi-Account Manager — manages MT5 connections for ML data collection.

Routes signals to accounts based on group-to-account mapping.
Fetches market data from the primary account (all see the same market).
Manages position tracking and account health across all connections.

Account configuration is now driven by environment variables via
metty.core.account_registry — adding a new account requires only
.env changes, no code changes here.
"""

import asyncio
import logging
from typing import Optional

import pandas as pd

from metty.bridge.client import MT5Bridge
from metty.core.account_registry import (
    AccountConfigInfo,
    get_account_configs,
    get_routing,
)
from metty.core.models import AccountConfig, AccountInfo, OrderResult, SignalGroup

logger = logging.getLogger(__name__)


class MultiAccountManager:
    """Manages MT5 accounts with different balance/leverage combinations.

    Each account connects to a separate MT5 Docker container via RPyC bridge.
    Signals are routed to accounts based on their originating group.
    Market data is fetched from Account A (all accounts see the same market).

    Account config is loaded from environment variables via account_registry.
    Override by passing account_configs/routing to __init__.
    """

    def __init__(
        self,
        account_configs: Optional[dict[str, AccountConfig]] = None,
        routing: Optional[dict[SignalGroup, list[str]]] = None,
    ):
        self.bridges: dict[str, MT5Bridge] = {}
        self._routing = routing or self._build_routing()
        self._primary: Optional[str] = None

        if account_configs:
            for name, config in account_configs.items():
                self.bridges[name] = MT5Bridge(config)
                if self._primary is None:
                    self._primary = name
        else:
            # Auto-load from environment
            for name, info in get_account_configs().items():
                config = self._info_to_config(info)
                self.bridges[name] = MT5Bridge(config)
                if self._primary is None:
                    self._primary = name

        # Fallback primary
        if self._primary is None:
            self._primary = "A"

    @staticmethod
    def _build_routing() -> dict[SignalGroup, list[str]]:
        """Build routing from registry, converting to SignalGroup enum."""
        raw_routing = get_routing()
        result: dict[SignalGroup, list[str]] = {}
        group_map = {
            "volume": SignalGroup.VOLUME,
            "ob_os": SignalGroup.OB_OS,
            "ma": SignalGroup.MA,
            "sentiment": SignalGroup.SENTIMENT,
        }
        for group_str, accounts in raw_routing.items():
            sg = group_map.get(group_str, SignalGroup.VOLUME)
            result[sg] = accounts
        return result

    @staticmethod
    def _info_to_config(info: AccountConfigInfo) -> AccountConfig:
        """Convert AccountConfigInfo from registry to AccountConfig for MT5Bridge."""
        return AccountConfig(
            name=info.name,
            broker_login=info.broker_login,
            broker_password=info.broker_password,
            broker_server=info.broker_server,
            bridge_host=info.bridge_host,
            bridge_port=info.bridge_port,
            initial_balance=info.initial_balance,
            leverage=info.leverage,
            signal_group=SignalGroup(info.signal_group),
            is_active=True,
        )

    def add_account(self, config: AccountConfig) -> None:
        """Add an account connection."""
        self.bridges[config.name] = MT5Bridge(config)

    async def connect_all(self) -> dict[str, bool]:
        """Connect to all MT5 bridge servers.

        Returns a dict of account name -> connection success.
        """
        results = {}
        for name, bridge in self.bridges.items():
            success = await bridge.connect()
            results[name] = success
            if success:
                logger.info(f"Account {name}: Connected to {bridge.host}:{bridge.port}")
            else:
                logger.error(f"Account {name}: Failed to connect to {bridge.host}:{bridge.port}")
        return results

    async def disconnect_all(self) -> None:
        """Disconnect from all MT5 bridge servers."""
        for name, bridge in self.bridges.items():
            await bridge.disconnect()
        logger.info("All MT5 connections closed")

    async def fetch_candles(
        self,
        symbol: str = "XAUUSD",
        timeframes: Optional[list[str]] = None,
        count: int = 500,
    ) -> dict[str, pd.DataFrame]:
        """Fetch latest candles from the primary account.

        All accounts see the same market, so we only need one connection
        for data. Returns a dict of timeframe -> DataFrame.
        """
        if timeframes is None:
            timeframes = ["M5", "M15", "H1", "H4", "D1"]

        primary = self.bridges.get(self._primary)
        if not primary:
            logger.error("Primary account bridge not available")
            return {}

        result = {}
        for tf in timeframes:
            df = await primary.get_candles(symbol, tf, count)
            if not df.empty:
                result[tf] = df
            else:
                logger.warning(f"No data for {symbol} {tf}")

        return result

    async def send_order(
        self,
        symbol: str,
        direction: str,
        lots: float,
        account_name: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderResult:
        """Send an order to a specific account."""
        bridge = self.bridges.get(account_name)
        if not bridge:
            return OrderResult(
                success=False,
                error=f"No bridge for account {account_name}",
                timestamp=__import__("datetime").datetime.now(),
            )
        return await bridge.send_order(symbol, direction, lots, stop_loss, take_profit)

    async def send_to_group(
        self,
        symbol: str,
        direction: str,
        lots: float,
        group: SignalGroup,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> list[tuple[str, OrderResult]]:
        """Send an order to all accounts mapped to a signal group."""
        target_accounts = self._routing.get(group, [self._primary or "A"])
        results = []
        for account_name in target_accounts:
            result = await self.send_order(
                symbol, direction, lots, account_name, stop_loss, take_profit
            )
            results.append((account_name, result))
        return results

    async def check_positions(self, symbol: str = "XAUUSD") -> dict[str, list[dict]]:
        """Check all open positions across all accounts."""
        all_positions = {}
        for name, bridge in self.bridges.items():
            positions = await bridge.get_positions(symbol)
            all_positions[name] = positions
        return all_positions

    async def close_position(self, account_name: str, ticket: int) -> bool:
        """Close a position on a specific account."""
        bridge = self.bridges.get(account_name)
        if not bridge:
            logger.error(f"No bridge for account {account_name}")
            return False
        return await bridge.close_position(ticket)

    async def get_all_account_info(self) -> dict[str, Optional[AccountInfo]]:
        """Get account info from all accounts."""
        info = {}
        for name, bridge in self.bridges.items():
            info[name] = await bridge.get_account_info()
        return info

    async def health_check_all(self) -> dict[str, bool]:
        """Check health of all bridge connections."""
        health = {}
        for name, bridge in self.bridges.items():
            health[name] = await bridge.health_check()
        return health

    async def reconnect_failed(self) -> dict[str, bool]:
        """Attempt to reconnect any failed connections."""
        results = {}
        for name, bridge in self.bridges.items():
            if not bridge._connected:
                logger.info(f"Attempting to reconnect account {name}...")
                success = await bridge.connect()
                results[name] = success
            else:
                results[name] = True
        return results